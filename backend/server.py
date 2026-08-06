#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

APP_VERSION = "0.3.1"
API_VERSION = "v1"
HEIGHT_RE = re.compile(rb"SetBestChain:.*?\bheight=(\d+)\b")

MAX_SAMPLES = 360
SAMPLE_INTERVAL = 10
STALE_HEALTHY_S = 60
STALE_SLOW_S = 180
EMA_ALPHA = 0.15
MIN_PEERS_FOR_ETA = 3

BYTE_UNITS = {"": 1, "B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    refresh: int
    timeout: int
    info_interval: int
    peer_interval: int
    max_backoff: int
    log_tail_bytes: int
    innovad: str
    datadir: str | None
    conf: str | None
    debug_log: Path | None
    frontend: Path


class TimedValue:
    def __init__(self) -> None:
        self.value: Any = None
        self.updated_wall: dt.datetime | None = None
        self.last_attempt = 0.0
        self.failures = 0
        self.warning: str | None = None

    def due(self, interval: int, max_backoff: int) -> bool:
        delay = min(max_backoff, interval * (2 ** min(self.failures, 6)))
        return self.last_attempt == 0.0 or time.monotonic() - self.last_attempt >= delay

    def success(self, value: Any) -> None:
        self.value = value
        self.updated_wall = dt.datetime.now().astimezone()
        self.last_attempt = time.monotonic()
        self.failures = 0
        self.warning = None

    def failure(self, warning: str) -> None:
        self.last_attempt = time.monotonic()
        self.failures += 1
        self.warning = warning

    def age_seconds(self) -> int | None:
        if self.updated_wall is None:
            return None
        return max(0, int((dt.datetime.now().astimezone() - self.updated_wall).total_seconds()))


class IBDSampler:
    def __init__(self, max_samples: int = MAX_SAMPLES) -> None:
        self.max_samples = max_samples
        self.samples: collections.deque = collections.deque(maxlen=max_samples)

    def record(self, height: int | None, connections: int | None,
               bytes_received: int | None, bytes_sent: int | None,
               ibd: bool | None, estimated_tip: int | None,
               blocks_inflight: int | None, ask_queue: int | None,
               active_download_peers: int | None) -> None:
        if height is None:
            return
        now = time.time()
        if self.samples and self.samples[-1]["height"] == height:
            s = self.samples[-1]
            s["time"] = now
            s["connections"] = connections or s["connections"]
            s["bytes_received"] = bytes_received or s["bytes_received"]
            s["bytes_sent"] = bytes_sent or s["bytes_sent"]
            return
        self.samples.append({
            "time": now,
            "height": height,
            "connections": connections,
            "bytes_received": bytes_received,
            "bytes_sent": bytes_sent,
            "ibd": ibd,
            "estimated_tip": estimated_tip,
            "blocks_inflight": blocks_inflight,
            "ask_queue": ask_queue,
            "active_download_peers": active_download_peers,
        })

    def window_average(self, seconds: float) -> float | None:
        if not self.samples:
            return None
        now = time.time()
        cutoff = now - seconds
        heights = [s["height"] for s in self.samples if s["time"] >= cutoff]
        if len(heights) < 2:
            return None
        blocks = heights[-1] - heights[0]
        span = self.samples[-1]["time"] - max(self.samples[0]["time"], cutoff)
        if span <= 0 or blocks < 0:
            return None
        return blocks / (span / 60.0)

    def session_average(self, start_height: int) -> float | None:
        if not self.samples or start_height is None:
            return None
        current = self.samples[-1]["height"]
        first = self.samples[0]
        blocks = current - start_height
        elapsed = time.time() - first["time"]
        if blocks <= 0 or elapsed <= 0:
            return None
        return blocks / (elapsed / 60.0)

    def current_rate(self) -> float | None:
        if len(self.samples) < 2:
            return None
        s1 = self.samples[-2]
        s2 = self.samples[-1]
        dt_sec = s2["time"] - s1["time"]
        if dt_sec <= 0:
            return None
        dh = s2["height"] - s1["height"]
        if dh < 0:
            return None
        return dh / (dt_sec / 60.0)

    def last_advance_time(self) -> float | None:
        if not self.samples:
            return None
        return self.samples[-1]["time"]

    def to_history(self, limit: int = 60) -> list[dict]:
        out = []
        for s in list(self.samples)[-limit:]:
            out.append({
                "time": int(s["time"]),
                "height": s["height"],
                "connections": s.get("connections"),
                "bytes_received": s.get("bytes_received"),
                "bytes_sent": s.get("bytes_sent"),
            })
        return out


class IBDTracker:
    def __init__(self) -> None:
        self.session_start_height: int | None = None
        self.session_observed_height: int | None = None
        self.last_height: int | None = None
        self.last_height_change: float = 0.0
        self._last_advance_time: float | None = None
        self.ema_rate: float | None = None
        self.peaks: dict[str, int] = {
            "connections": 0,
            "blocks_inflight": 0,
            "ask_queue": 0,
        }

    def update(self, height: int | None, now: float | None = None) -> None:
        if height is None:
            return
        now = now or time.time()
        if self.session_start_height is None:
            self.session_start_height = height
            self.session_observed_height = height
            self.last_height = height
            self.last_height_change = now
            return
        if height != self.last_height:
            if height > self.last_height:
                self._last_advance_time = now
            self.last_height = height
            self.last_height_change = now
            if self.session_observed_height is not None and height > self.session_observed_height:
                self.session_observed_height = height

    def last_advance_time(self) -> float | None:
        return self._last_advance_time

    def sync_state(self, ibd_rpc: bool | None, rpc_failures: int) -> str:
        if ibd_rpc is None and rpc_failures > 0:
            return "rpc_unavailable"
        if ibd_rpc is False:
            return "synced"
        if ibd_rpc is not True and rpc_failures == 0 and ibd_rpc is None:
            return "unknown"
        now = time.time()
        elapsed = now - self.last_height_change if self.last_height_change else 999999
        if elapsed < STALE_HEALTHY_S:
            return "healthy"
        if elapsed < STALE_SLOW_S:
            return "slow"
        return "stalled"

    def update_ema(self, new_rate: float | None) -> None:
        if new_rate is None:
            return
        if self.ema_rate is None:
            self.ema_rate = new_rate
        else:
            self.ema_rate = EMA_ALPHA * new_rate + (1 - EMA_ALPHA) * self.ema_rate

    def update_peaks(self, connections: int | None, inflight: int | None, askqueue: int | None) -> None:
        if connections is not None:
            self.peaks["connections"] = max(self.peaks["connections"], connections)
        if inflight is not None:
            self.peaks["blocks_inflight"] = max(self.peaks["blocks_inflight"], inflight)
        if askqueue is not None:
            self.peaks["ask_queue"] = max(self.peaks["ask_queue"], askqueue)


def compute_eta(ema_rate: float | None, current_height: int | None,
                target_height: int | None) -> float | None:
    if ema_rate is None or current_height is None or target_height is None:
        return None
    remaining = target_height - current_height
    if remaining <= 0 or ema_rate <= 0:
        return None
    return remaining / ema_rate * 60.0


def estimate_network_height(peers: list[dict], current_height: int | None) -> int | None:
    if not peers or current_height is None:
        return None
    heights = []
    for peer in peers:
        h = as_int(peer.get("startingheight"))
        if h is None or h <= 0:
            continue
        if current_height > 0 and h > current_height * 2.5:
            continue
        if current_height > 0 and h < current_height * 0.5:
            continue
        heights.append(h)
    if not heights:
        return None
    heights.sort()
    if len(heights) == 1:
        return heights[0]
    q75 = heights[int(len(heights) * 0.75)]
    return q75


def aggregate_peers(peers: list[dict]) -> dict[str, Any]:
    active_download = 0
    total_inflight = 0
    total_askqueue = 0
    details = []
    for peer in peers:
        inflight = as_int(peer.get("blocksinflight")) or 0
        askqueue = as_int(peer.get("askqueuesize")) or 0
        total_inflight += inflight
        total_askqueue += askqueue
        is_active = inflight > 0
        if is_active:
            active_download += 1
        bytes_sent = parse_bytes(peer.get("bytessent"))
        if bytes_sent is None:
            bytes_sent = parse_bytes(peer.get("bytessend"))
        details.append({
            "addr": peer.get("addr", ""),
            "version": peer.get("subver"),
            "protocol_version": as_int(peer.get("version")),
            "starting_height": as_int(peer.get("startingheight")),
            "best_known_height": as_int(peer.get("bestknownheight")) or as_int(peer.get("chainheight")),
            "bytes_received": parse_bytes(peer.get("bytesrecv")),
            "bytes_sent": bytes_sent,
            "blocks_inflight": inflight,
            "ask_queue": askqueue,
            "pingtime": as_float(peer.get("pingtime")),
            "lastsend": as_int(peer.get("lastsend")),
            "lastrecv": as_int(peer.get("lastrecv")),
            "connection_age": as_int(peer.get("conntime")),
        })
    return {
        "active_download_peers": active_download,
        "total_blocks_inflight": total_inflight,
        "total_ask_queue": total_askqueue,
        "details": details,
    }


def host_metrics() -> dict[str, Any]:
    cpu_count = os.cpu_count()
    total_ram_bytes = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        total_ram_bytes = int(parts[1]) * 1024
                    break
    except (OSError, ValueError):
        pass
    return {
        "cpu_count": cpu_count,
        "total_ram_bytes": total_ram_bytes,
    }


class Collector:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.info = TimedValue()
        self.peers = TimedValue()
        self.blockchain = TimedValue()
        self.ibd_tracker = IBDTracker()
        self.sampler = IBDSampler()
        self.traffic = TrafficTracker()
        self.metrics = host_metrics()
        self._last_sample_time = 0.0

    def rpc_json(self, method: str) -> tuple[Any | None, str | None]:
        code, output, error = rpc(self.config, [method])
        if code != 0:
            return None, error or output or f"{method} failed"
        try:
            return json.loads(output), None
        except json.JSONDecodeError:
            return None, f"{method} returned invalid JSON"

    def refresh_rpc(self) -> None:
        if self.info.due(self.config.info_interval, self.config.max_backoff):
            value, error = self.rpc_json("getinfo")
            if error is None and isinstance(value, dict):
                self.info.success(value)
            else:
                self.info.failure(f"RPC busy: getinfo unavailable ({error or 'invalid response'})")

        if self.peers.due(self.config.peer_interval, self.config.max_backoff):
            value, error = self.rpc_json("getpeerinfo")
            if error is None and isinstance(value, list):
                self.peers.success([peer for peer in value if isinstance(peer, dict)])
            else:
                self.peers.failure(f"RPC busy: getpeerinfo unavailable ({error or 'invalid response'})")

        if self.blockchain.due(self.config.info_interval, self.config.max_backoff):
            value, error = self.rpc_json("getblockchaininfo")
            if error is None and isinstance(value, dict):
                self.blockchain.success(value)
            else:
                self.blockchain.failure(f"RPC busy: getblockchaininfo unavailable ({error or 'invalid response'})")

    def collect(self) -> dict[str, Any]:
        with self.lock:
            self.refresh_rpc()
            snapshot = build_snapshot(self.config, self.info, self.peers, self.blockchain,
                                     self.ibd_tracker, self.sampler, self.metrics, self.traffic)
            now_mono = time.time()
            if now_mono - self._last_sample_time >= SAMPLE_INTERVAL:
                chain = snapshot.get("chain", {})
                net = snapshot.get("network", {})
                ibd_sec = snapshot.get("ibd", {})
                peer_agg = ibd_sec.get("peer_aggregation", {})
                self.sampler.record(
                    height=chain.get("height"),
                    connections=net.get("connections"),
                    bytes_received=net.get("bytes_received"),
                    bytes_sent=net.get("bytes_sent"),
                    ibd=chain.get("initial_block_download"),
                    estimated_tip=ibd_sec.get("estimated_tip"),
                    blocks_inflight=peer_agg.get("total_blocks_inflight"),
                    ask_queue=peer_agg.get("total_ask_queue"),
                    active_download_peers=peer_agg.get("active_download_peers"),
                )
                self._last_sample_time = now_mono
            return snapshot


def run(command: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout.strip(), completed.stderr.strip()
    except subprocess.TimeoutExpired as exc:
        return 1, "", f"timed out after {exc.timeout:g} seconds"
    except OSError as exc:
        return 1, "", str(exc)


def locate_innovad(explicit: str | None) -> str:
    candidates = [
        explicit,
        os.getenv("INNOVAD_PATH"),
        shutil.which("innovad"),
        str(Path.home() / "innova" / "src" / "innovad"),
        "/usr/local/bin/innovad",
        "/usr/bin/innovad",
    ]
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    return explicit or "innovad"


def rpc(config: Config, args: list[str]) -> tuple[int, str, str]:
    command = [config.innovad]
    if config.datadir:
        command.append(f"-datadir={config.datadir}")
    if config.conf:
        command.append(f"-conf={config.conf}")
    return run(command + args, config.timeout)


def process_pid(config: Config) -> int | None:
    commands = (
        ["systemctl", "show", "innovad.service", "--property=MainPID", "--value"],
        ["pidof", "-s", "innovad"],
        ["pgrep", "-xo", "innovad"],
    )
    for command in commands:
        code, output, _ = run(command, config.timeout)
        candidate = output.split()[0] if output else ""
        if code == 0 and candidate.isdigit() and int(candidate) > 0:
            return int(candidate)
    return None


def process_times(pid: int | None, config: Config) -> tuple[dt.datetime | None, int | None]:
    if pid is None:
        return None, None
    code, output, _ = run(["ps", "-p", str(pid), "-o", "etimes="], config.timeout)
    uptime_seconds = int(output) if code == 0 and output.isdigit() else None
    code, output, _ = run(["ps", "-p", str(pid), "-o", "lstart="], config.timeout)
    started_at = None
    if code == 0 and output:
        try:
            parsed = dt.datetime.strptime(output, "%a %b %d %H:%M:%S %Y")
            started_at = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
        except ValueError:
            pass
    if started_at is None and uptime_seconds is not None:
        started_at = dt.datetime.now().astimezone() - dt.timedelta(seconds=uptime_seconds)
    return started_at, uptime_seconds


def read_log_height(path: Path | None, tail_bytes: int) -> tuple[int | None, dt.datetime | None]:
    if path is None:
        return None, None
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - tail_bytes), os.SEEK_SET)
            chunk = handle.read()
        matches = list(HEIGHT_RE.finditer(chunk))
        if not matches:
            return None, None
        return int(matches[-1].group(1)), dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except (OSError, ValueError):
        return None, None


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_bytes(value: Any) -> int | None:
    """Parse a byte counter that may be an int or a human string ("537.48 MB")."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)?\s*$", value, re.IGNORECASE)
        if not match:
            return None
        number = float(match.group(1))
        unit = (match.group(2) or "").upper().replace("I", "")
        factor = BYTE_UNITS.get(unit)
        if factor is None:
            return None
        return int(round(number * factor))
    return None


class TrafficTracker:
    """Ring buffer of cumulative traffic samples used to derive throughput."""

    def __init__(self, max_samples: int = 120) -> None:
        self.max_samples = max_samples
        self.samples: collections.deque = collections.deque(maxlen=max_samples)

    def record(self, received: int | None, sent: int | None) -> None:
        if received is None or sent is None:
            return
        now = time.time()
        if self.samples and (received < self.samples[-1][1] or sent < self.samples[-1][2]):
            self.samples.clear()
        if self.samples and self.samples[-1][1] == received and self.samples[-1][2] == sent:
            self.samples[-1] = (now, received, sent)
            return
        self.samples.append((now, received, sent))

    def rates(self) -> tuple[float | None, float | None]:
        if len(self.samples) < 2:
            return None, None
        (t0, r0, s0), (t1, r1, s1) = self.samples[-2], self.samples[-1]
        span = t1 - t0
        if span <= 0:
            return None, None
        return (r1 - r0) / span, (s1 - s0) / span


def iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def build_snapshot(config: Config, info_state: TimedValue, peers_state: TimedValue,
                   blockchain_state: TimedValue, ibd_tracker: IBDTracker,
                   sampler: IBDSampler, metrics: dict[str, Any],
                   traffic: TrafficTracker | None = None) -> dict[str, Any]:
    now = dt.datetime.now().astimezone()
    info = info_state.value if isinstance(info_state.value, dict) else {}
    peers = peers_state.value if isinstance(peers_state.value, list) else []
    blockchain = blockchain_state.value if isinstance(blockchain_state.value, dict) else {}

    log_height, log_updated = read_log_height(config.debug_log, config.log_tail_bytes)
    rpc_height = as_int(info.get("blocks"))
    height = log_height if log_height is not None else rpc_height
    height_source = "debug_log" if log_height is not None else ("rpc" if rpc_height is not None else None)

    ibd_rpc = blockchain.get("initialblockdownload") if isinstance(blockchain.get("initialblockdownload"), bool) else info.get("initialblockdownload")

    pid = process_pid(config)
    started_at, uptime_seconds = process_times(pid, config)
    inbound = sum(1 for peer in peers if peer.get("inbound") is True) if peers else None
    outbound = sum(1 for peer in peers if peer.get("inbound") is False) if peers else None
    pings = [as_float(peer.get("pingtime")) for peer in peers]
    pings = [ping for ping in pings if ping is not None]

    rpc_ok = info_state.failures == 0 or peers_state.failures == 0
    rpc_failures = max(info_state.failures, peers_state.failures)

    ibd_tracker.update(height)
    current_rate = sampler.current_rate()
    ibd_tracker.update_ema(current_rate)

    peer_agg = aggregate_peers(peers)
    estimated_tip = estimate_network_height(peers, height)

    traffic_received = parse_bytes(info.get("datareceived"))
    traffic_sent = parse_bytes(info.get("datasent"))
    traffic_source = "getinfo"
    if traffic_received is None and peers:
        traffic_received = sum(p["bytes_received"] or 0 for p in peer_agg["details"])
        traffic_source = "peers"
    if traffic_sent is None and peers:
        traffic_sent = sum(p["bytes_sent"] or 0 for p in peer_agg["details"])
        traffic_source = "peers"
    if traffic_received is None and traffic_sent is None:
        traffic_source = None
    if traffic is not None:
        traffic.record(traffic_received, traffic_sent)
    rx_rate, tx_rate = traffic.rates() if traffic is not None else (None, None)

    ibd_tracker.update_peaks(
        as_int(info.get("connections")) or (len(peers) if peers else None),
        peer_agg["total_blocks_inflight"],
        peer_agg["total_ask_queue"],
    )

    sync_state = ibd_tracker.sync_state(ibd_rpc, rpc_failures)

    session_start = ibd_tracker.session_start_height
    session_type = "unknown"
    if session_start is not None:
        if session_start < 1000:
            session_type = "cold_ibd"
        else:
            session_type = "resume_ibd"
    if sync_state == "synced":
        session_type = "synced"

    session_rate = sampler.session_average(session_start) if session_start is not None else None
    rate_5min = sampler.window_average(300)
    eta_seconds = compute_eta(ibd_tracker.ema_rate, height, estimated_tip)

    last_advance = ibd_tracker.last_advance_time()
    last_advance_ago = None
    if last_advance:
        last_advance_ago = max(0, int(time.time() - last_advance))

    notices = [notice for notice in (info_state.warning, peers_state.warning, blockchain_state.warning) if notice]

    return {
        "api": {"name": "innova-node-dashboard", "version": API_VERSION, "schema": 4},
        "dashboard": {"version": APP_VERSION},
        "generated_at": now.isoformat(),
        "node": {
            "online": pid is not None or height is not None,
            "network": "mainnet",
            "version": info.get("version"),
            "build_commit": info.get("buildcommit"),
            "build_dirty": info.get("builddirty"),
            "protocol_version": info.get("protocolversion"),
            "started_at": iso(started_at),
            "uptime_seconds": uptime_seconds,
        },
        "chain": {
            "height": height,
            "height_source": height_source,
            "height_updated_at": iso(log_updated if height_source == "debug_log" else info_state.updated_wall),
            "initial_block_download": ibd_rpc,
            "difficulty": info.get("difficulty"),
            "money_supply": info.get("moneysupply"),
            "verification_progress": as_float(blockchain.get("verificationprogress")),
        },
        "network": {
            "connections": as_int(info.get("connections")) if info.get("connections") is not None else (len(peers) if peers else None),
            "inbound": inbound,
            "outbound": outbound,
            "average_ping_ms": round(sum(pings) / len(pings) * 1000, 2) if pings else None,
            "bytes_received": traffic_received,
            "bytes_sent": traffic_sent,
            "traffic": {
                "received": traffic_received,
                "sent": traffic_sent,
                "rx_rate_bps": round(rx_rate, 2) if rx_rate is not None else None,
                "tx_rate_bps": round(tx_rate, 2) if tx_rate is not None else None,
                "source": traffic_source,
            },
        },
        "ibd": {
            "session_type": session_type,
            "session_start_height": session_start,
            "estimated_tip": estimated_tip,
            "current_rate_bpm": round(current_rate, 2) if current_rate is not None else None,
            "rate_5min_bpm": round(rate_5min, 2) if rate_5min is not None else None,
            "rate_ema_bpm": round(ibd_tracker.ema_rate, 2) if ibd_tracker.ema_rate is not None else None,
            "session_rate_bpm": round(session_rate, 2) if session_rate is not None else None,
            "eta_seconds": round(eta_seconds) if eta_seconds is not None and eta_seconds < 1e9 else None,
            "last_height_advance_ago_seconds": last_advance_ago,
            "sync_state": sync_state,
            "peer_aggregation": {
                "active_download_peers": peer_agg["active_download_peers"],
                "total_blocks_inflight": peer_agg["total_blocks_inflight"],
                "total_ask_queue": peer_agg["total_ask_queue"],
                "peer_count": len(peers),
            },
            "peers": peer_agg["details"],
        },
        "host": metrics,
        "history": sampler.to_history(60),
        "freshness": {
            "getinfo_age_seconds": info_state.age_seconds(),
            "getpeerinfo_age_seconds": peers_state.age_seconds(),
            "getblockchaininfo_age_seconds": blockchain_state.age_seconds(),
            "getinfo_failures": info_state.failures,
            "getpeerinfo_failures": peers_state.failures,
            "getblockchaininfo_failures": blockchain_state.failures,
        },
        "features": {
            "peers": bool(peers),
            "traffic": traffic_received is not None or traffic_sent is not None,
            "debug_log_height": log_height is not None,
            "ibd_benchmark": True,
            "host_metrics": bool(metrics.get("total_ram_bytes")),
        },
        "notices": notices,
        "errors": [],
    }


class Server(ThreadingHTTPServer):
    config: Config
    collector: Collector
    snapshot_lock: threading.Lock
    snapshot_data: dict[str, Any] | None
    snapshot_updated: float


class Handler(BaseHTTPRequestHandler):
    server_version = f"InnovaDashboard/{APP_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    @property
    def app(self) -> Server:
        return self.server  # type: ignore[return-value]

    def send_data(self, payload: bytes, content_type: str, status: int = HTTPStatus.OK, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def snapshot(self) -> dict[str, Any]:
        with self.app.snapshot_lock:
            if self.app.snapshot_data is None or time.monotonic() - self.app.snapshot_updated > self.app.config.refresh:
                self.app.snapshot_data = self.app.collector.collect()
                self.app.snapshot_updated = time.monotonic()
            return self.app.snapshot_data

    def static(self, relative: str) -> None:
        base = self.app.config.frontend.resolve()
        path = (base / relative).resolve()
        if base not in path.parents and path != base:
            self.send_data(b"forbidden\n", "text/plain", HTTPStatus.FORBIDDEN)
            return
        if not path.is_file():
            self.send_data(b"not found\n", "text/plain", HTTPStatus.NOT_FOUND)
            return
        types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}
        cache = "no-cache" if path.name == "index.html" else "public, max-age=3600"
        self.send_data(path.read_bytes(), types.get(path.suffix, "application/octet-stream"), cache=cache)

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self.static("index.html")
        elif route == "/api/v1/status":
            payload = json.dumps(self.snapshot(), ensure_ascii=False, separators=(",", ":")).encode()
            self.send_data(payload, "application/json; charset=utf-8")
        elif route == "/health":
            snapshot = self.snapshot()
            ok = bool(snapshot.get("node", {}).get("online"))
            payload = json.dumps({"ok": ok, "generated_at": snapshot.get("generated_at")}).encode()
            self.send_data(payload, "application/json", HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE)
        elif route.startswith("/assets/"):
            self.static(route.lstrip("/"))
        else:
            self.send_data(b"not found\n", "text/plain", HTTPStatus.NOT_FOUND)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Innova node dashboard")
    parser.add_argument("--host", default=os.getenv("INNOVA_DASHBOARD_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=env_int("INNOVA_DASHBOARD_PORT", 8787))
    parser.add_argument("--refresh", type=int, default=env_int("INNOVA_DASHBOARD_REFRESH", 5))
    parser.add_argument("--rpc-timeout", type=int, default=env_int("INNOVA_DASHBOARD_RPC_TIMEOUT", 8))
    parser.add_argument("--info-interval", type=int, default=env_int("INNOVA_DASHBOARD_INFO_INTERVAL", 60))
    parser.add_argument("--peer-interval", type=int, default=env_int("INNOVA_DASHBOARD_PEER_INTERVAL", 120))
    parser.add_argument("--max-backoff", type=int, default=env_int("INNOVA_DASHBOARD_MAX_BACKOFF", 900))
    parser.add_argument("--log-tail-bytes", type=int, default=env_int("INNOVA_DASHBOARD_LOG_TAIL_BYTES", 262144))
    parser.add_argument("--innovad", default=os.getenv("INNOVAD_PATH"))
    parser.add_argument("--datadir", default=os.getenv("INNOVA_DATADIR"))
    parser.add_argument("--conf", default=os.getenv("INNOVA_CONF"))
    parser.add_argument("--debug-log", default=os.getenv("INNOVA_DEBUG_LOG"))
    parser.add_argument("--frontend-dir", default=os.getenv("INNOVA_DASHBOARD_FRONTEND"))
    args = parser.parse_args()

    backend = Path(__file__).resolve().parent
    datadir = str(Path(args.datadir).expanduser()) if args.datadir else None
    debug_log = Path(args.debug_log).expanduser() if args.debug_log else (Path(datadir) / "debug.log" if datadir else None)
    config = Config(
        args.host, args.port, max(1, args.refresh), max(1, args.rpc_timeout),
        max(5, args.info_interval), max(5, args.peer_interval), max(30, args.max_backoff),
        max(4096, args.log_tail_bytes), locate_innovad(args.innovad), datadir, args.conf,
        debug_log, Path(args.frontend_dir).expanduser() if args.frontend_dir else backend.parent / "frontend",
    )

    server = Server((config.host, config.port), Handler)
    server.config = config
    server.collector = Collector(config)
    server.snapshot_lock = threading.Lock()
    server.snapshot_data = None
    server.snapshot_updated = 0.0
    print(f"Innova Node Dashboard {APP_VERSION} - http://{config.host}:{config.port}")
    print(f"innovad: {config.innovad}")
    print(f"debug.log: {config.debug_log}")
    print(f"RPC intervals: getinfo={config.info_interval}s getpeerinfo={config.peer_interval}s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
