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

APP_VERSION = "0.4.1"
API_VERSION = "v1"
HEIGHT_RE = re.compile(rb"SetBestChain:.*?\bheight=(\d+)\b")

MAX_SAMPLES = 360
SAMPLE_INTERVAL = 10
STALE_HEALTHY_S = 60
STALE_SLOW_S = 180
EMA_ALPHA = 0.15
MIN_PEERS_FOR_ETA = 3

# A cached IBD=false is evidence of synchronisation only while the chain RPC that
# produced it is itself fresh; past this age the dashboard degrades instead of
# asserting "synced". The effective bound is derived per run from the poll
# interval (see chain_state_max_age) so a slow poller is not punished.
CHAIN_STATE_MIN_MAX_AGE_S = 120
# A peer height becomes "confirmed" only with at least this much support, so a
# single unconfirmed announcement can never create a lag verdict.
CONFIRMED_TIP_MIN_SUPPORT = 2
CONFIRMED_TIP_TOLERANCE = 1
# A confirmed lag smaller than this is ordinary tip churn on a live chain, not a
# synchronisation deficit; the raw number is still reported to the UI.
CONFIRMED_LAG_BLOCKS = 2

STOP_CONFIRM_MISSES = 2
DAEMON_RUNNING = "running"
DAEMON_STOPPED = "stopped"
DAEMON_UNKNOWN = "unknown"

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
    # Per-source RPC budgets: a slow getinfo must never gate the fast probes.
    timeout_fast: int = 6
    timeout_slow: int = 45


@dataclass(frozen=True)
class DaemonState:
    """Confirmed daemon lifecycle state as of the current snapshot.

    - running:  a live process with a matching PID was observed
    - stopped:  a previously running daemon was confirmed absent
    - unknown:  no running process has ever been observed this run
    """

    state: str = DAEMON_UNKNOWN
    pid: int | None = None


class TimedValue:
    """Cached value with explicit availability and freshness lifecycle.

    ``value`` starts as ``None`` meaning "never fetched" (UNAVAILABLE). A
    successful fetch stores the real result (which may be a valid empty list)
    and a failure preserves the last successful value while marking it stale.
    """

    def __init__(self) -> None:
        self.value: Any = None
        self.updated_wall: dt.datetime | None = None
        self.last_attempt = 0.0
        self.last_attempt_wall: dt.datetime | None = None
        self.failures = 0
        self.warning: str | None = None

    def due(self, interval: int, max_backoff: int) -> bool:
        delay = min(max_backoff, interval * (2 ** min(self.failures, 6)))
        return self.last_attempt == 0.0 or time.monotonic() - self.last_attempt >= delay

    def success(self, value: Any) -> None:
        self.value = value
        self.updated_wall = dt.datetime.now().astimezone()
        self.last_attempt = time.monotonic()
        self.last_attempt_wall = dt.datetime.now().astimezone()
        self.failures = 0
        self.warning = None

    def failure(self, warning: str) -> None:
        self.last_attempt = time.monotonic()
        self.last_attempt_wall = dt.datetime.now().astimezone()
        self.failures += 1
        self.warning = warning

    def invalidate(self, warning: str) -> None:
        """Drop the cached value (e.g. after the daemon restarted)."""
        self.value = None
        self.updated_wall = None
        self.failures = 0
        self.warning = warning
        self.last_attempt = 0.0
        self.last_attempt_wall = dt.datetime.now().astimezone()

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


def chain_state_max_age(info_interval: int) -> int:
    """Oldest chain-RPC state that may still be used as sync evidence."""
    return max(CHAIN_STATE_MIN_MAX_AGE_S, int(info_interval) * 2)


def confirmed_tip_from_peers(peers: list[dict] | None, *,
                             min_support: int = CONFIRMED_TIP_MIN_SUPPORT,
                             tolerance: int = CONFIRMED_TIP_TOLERANCE) -> dict[str, Any]:
    """Highest peer height that a *quorum* of peers actually confirms.

    Peers gossip ``bestknownheight``; a single peer announcing a height far above
    everybody else is not evidence that this node is behind (it may be stale, on
    another network, or simply wrong). A height is treated as confirmed only when
    at least ``min_support`` peers announce it or something higher within
    ``tolerance`` blocks, so one unconfirmed announcement can never produce a
    "behind" verdict on its own.
    """
    heights: list[int] = []
    for peer in peers or []:
        if not isinstance(peer, dict):
            continue
        value = as_int(peer.get("bestknownheight"))
        if value is not None and value > 0:
            heights.append(value)
    if not heights:
        return {"confirmed_tip": None, "support": 0, "max_peer_tip": None, "announcements": 0}
    required = min(max(int(min_support), 1), len(heights))
    confirmed: int | None = None
    support = 0
    for candidate in sorted(set(heights), reverse=True):
        backers = sum(1 for h in heights if h >= candidate - tolerance)
        if backers >= required:
            confirmed = candidate
            support = backers
            break
    return {
        "confirmed_tip": confirmed,
        "support": support,
        "max_peer_tip": max(heights),
        "announcements": len(heights),
    }


def resolve_sync_phase(*, ibd_rpc: bool | None, chain_fresh: bool,
                       chain_failures: int = 0, rpc_failures: int = 0,
                       height: int | None = None, height_stale: bool = False,
                       behind_confirmed: int = 0, running: bool = True,
                       require_height: bool = False,
                       lag_threshold: int = CONFIRMED_LAG_BLOCKS) -> str:
    """Canonical four-state sync verdict.

    ``ibd_rpc`` is only ever trusted while the chain RPC that produced it is
    fresh: a cached ``initialblockdownload=false`` is *not* proof of
    synchronisation. Freshness of the peer view is deliberately independent —
    a stale chain verdict may never be presented as "synced" just because the
    peer panel is live.
    """
    failures = chain_failures if chain_failures else rpc_failures
    if not running:
        return "unknown"
    if ibd_rpc is None:
        return "rpc_unavailable" if failures > 0 else "unknown"
    if not chain_fresh:
        return "degraded"
    if require_height and (height is None or height_stale):
        return "degraded"
    if ibd_rpc is True:
        return "syncing"
    if behind_confirmed and behind_confirmed >= max(1, int(lag_threshold)):
        return "syncing"
    return "synced"


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
            if self.last_height is not None and height > self.last_height:
                self._last_advance_time = now
            self.last_height = height
            self.last_height_change = now
            if self.session_observed_height is not None and height > self.session_observed_height:
                self.session_observed_height = height

    def last_advance_time(self) -> float | None:
        return self._last_advance_time

    def sync_state(self, ibd_rpc: bool | None, rpc_failures: int, *,
                   chain_age_seconds: int | None = None,
                   chain_failures: int = 0,
                   max_chain_age_seconds: int | None = None,
                   height: int | None = None,
                   height_stale: bool = False,
                   behind_confirmed: int = 0,
                   running: bool = True) -> str:
        """Legacy sync label, hardened against stale chain state.

        Without freshness inputs the historical contract is preserved (a False
        IBD is reported as ``synced``): callers that know the age must pass
        ``chain_age_seconds``/``max_chain_age_seconds``, and then a stale
        ``IBD=false`` degrades instead of claiming synchronisation. The canonical
        four-state verdict lives in :func:`resolve_sync_phase`.
        """
        chain_fresh = True
        if chain_age_seconds is not None and max_chain_age_seconds is not None:
            chain_fresh = chain_age_seconds <= max_chain_age_seconds
        phase = resolve_sync_phase(
            ibd_rpc=ibd_rpc, chain_fresh=chain_fresh, chain_failures=chain_failures,
            rpc_failures=rpc_failures, height=height, height_stale=height_stale,
            behind_confirmed=behind_confirmed, running=running,
            require_height=height is not None or height_stale,
        )
        if phase != "syncing":
            return phase
        # IBD is (freshly) in progress: keep the historical activity detail.
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


def peer_snapshot_state(state: TimedValue, interval: int) -> dict[str, Any]:
    """Describe the peer snapshot lifecycle.

    availability / freshness / value are independent axes:

    - UNAVAILABLE: ``value`` is None (never successfully fetched this run)
    - AVAILABLE:   ``value`` is a list, including a valid empty list
    - FRESH:       last success within the polling interval and no failures
                   since it
    - STALE:       a previous success exists but the latest refresh failed,
                   timed out, or aged beyond the polling interval

    A successful ``[]`` is AVAILABLE with ``count == 0``; it is never inferred
    from missing data.
    """
    available = isinstance(state.value, list)
    if not available:
        return {
            "available": False,
            "fresh": False,
            "stale": False,
            "count": None,
            "items": None,
            "age_seconds": None,
            "last_success_at": None,
            "last_attempt_at": iso(state.last_attempt_wall),
            "last_error": state.warning,
        }
    age = state.age_seconds()
    fresh = state.failures == 0 and age is not None and age <= interval
    return {
        "available": True,
        "fresh": fresh,
        "stale": not fresh,
        "count": len(state.value),
        "items": aggregate_peers(state.value)["details"],
        "age_seconds": age,
        "last_success_at": iso(state.updated_wall),
        "last_attempt_at": iso(state.last_attempt_wall),
        "last_error": state.warning,
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
        # Independent metric families: one poller each, one timeout each.
        self.sources = {
            "height": RpcSource("height", "getblockcount", config.refresh, config.timeout_fast, _valid_int),
            "info": RpcSource("info", "getinfo", config.info_interval, config.timeout_slow, lambda v: isinstance(v, dict)),
            "peers": RpcSource("peers", "getpeerinfo", config.peer_interval, max(config.timeout_fast, 10), lambda v: isinstance(v, list)),
            "blockchain": RpcSource("blockchain", "getblockchaininfo", config.info_interval, max(config.timeout_fast, 10), lambda v: isinstance(v, dict)),
        }
        self.log_height = TimedValue()
        self.log_reader = LogHeightReader(initial_scan_bytes=config.log_tail_bytes)
        self._stop = threading.Event()
        self._started = False
        self._threads: list[threading.Thread] = []
        self.ibd_tracker = IBDTracker()
        self.sampler = IBDSampler()
        self.traffic = TrafficTracker()
        self.metrics = host_metrics()
        self._last_sample_time = 0.0
        self._observed_pid: int | None = None
        self._daemon_state: str = DAEMON_UNKNOWN
        self._stop_misses = 0
        self._last_known_height: int | None = None

    @property
    def info(self) -> TimedValue:
        return self.sources["info"].state

    @property
    def peers(self) -> TimedValue:
        return self.sources["peers"].state

    @property
    def blockchain(self) -> TimedValue:
        return self.sources["blockchain"].state

    @property
    def height(self) -> TimedValue:
        return self.sources["height"].state

    def rpc_json(self, method: str, timeout: int | None = None) -> tuple[Any | None, str | None]:
        """Single seam for RPC access (patchable; tests use it directly)."""
        return rpc_json(self.config, method, timeout)

    def invalidate_daemon_caches(self, warning: str) -> None:
        """Drop daemon-specific cached state after a restart/stop transition."""
        for state in (self.info, self.peers, self.blockchain, self.height, self.log_height):
            state.invalidate(warning)
        self.traffic.samples.clear()
        self._last_sample_time = 0.0

    def update_daemon_state(self, pid: int | None) -> DaemonState:
        """Advance the daemon lifecycle state machine.

        - a found PID confirms RUNNING; a PID change or reappearance after a
          confirmed stop invalidates daemon-specific caches so old data from a
          previous instance is never presented as current
        - STOPPED requires ``STOP_CONFIRM_MISSES`` consecutive PID-detection
          misses while previously RUNNING, so one transient lookup failure
          never triggers a false shutdown
        - UNKNOWN while no running process has ever been observed this run
        """
        if pid is not None:
            restarted = self._observed_pid is not None and pid != self._observed_pid
            if restarted or self._daemon_state == DAEMON_STOPPED:
                self.invalidate_daemon_caches("innovad process restarted (PID changed)")
            self._observed_pid = pid
            self._daemon_state = DAEMON_RUNNING
            self._stop_misses = 0
        else:
            self._stop_misses += 1
            if self._daemon_state == DAEMON_RUNNING and self._stop_misses >= STOP_CONFIRM_MISSES:
                self._daemon_state = DAEMON_STOPPED
                self._observed_pid = None
                self._stop_misses = 0
        effective_pid = self._observed_pid if self._daemon_state == DAEMON_RUNNING else None
        return DaemonState(state=self._daemon_state, pid=effective_pid)

    def _refresh_log_height(self) -> None:
        value, _updated = self.log_reader.read(self.config.debug_log)
        if value is None:
            self.log_height.failure("debug.log: no height line in the incremental window")
        else:
            self.log_height.success(value)

    def poll_source(self, name: str) -> None:
        source = self.sources[name]
        if not source.state.due(source.interval, self.config.max_backoff):
            return
        source.poll(self.config, fetcher=self.rpc_json)
        if name == "height" and as_int(source.state.value) is None:
            # RPC height unavailable: bounded incremental log fallback only.
            self._refresh_log_height()

    def _poll_loop(self, name: str) -> None:
        while not self._stop.is_set():
            try:
                self.poll_source(name)
            except Exception as exc:  # a poller must never die silently
                self.sources[name].state.failure(f"poller error: {exc}")
            self._stop.wait(1.0)

    def start(self) -> None:
        self._started = True
        for name in list(self.sources):
            thread = threading.Thread(target=self._poll_loop, args=(name,), name=f"rpc-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()

    def collect(self) -> dict[str, Any]:
        with self.lock:
            pid = process_pid(self.config)
            daemon = self.update_daemon_state(pid)
            # Only an unstarted collector (unit tests, tooling) polls inline.
            # A served collector is filled by its own threads, so a request
            # never blocks on a slow RPC: collect() stays a pure cache read
            # even while getinfo is taking tens of seconds.
            for name, source in self.sources.items():
                if not self._started and source.state.last_attempt == 0.0:
                    try:
                        self.poll_source(name)
                    except Exception as exc:
                        source.state.failure(f"poll error: {exc}")
            snapshot = build_snapshot(self.config, self.info, self.peers, self.blockchain,
                                      self.ibd_tracker, self.sampler, self.metrics, self.traffic,
                                      daemon=daemon, last_known_height=self._last_known_height,
                                      height_state=self.height, log_state=self.log_height)
            live_height = snapshot.get("chain", {}).get("height")
            if live_height is not None:
                self._last_known_height = live_height
            now_mono = time.time()
            if daemon.state == DAEMON_RUNNING and now_mono - self._last_sample_time >= SAMPLE_INTERVAL:
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


def rpc(config: Config, args: list[str], timeout: int | None = None) -> tuple[int, str, str]:
    command = [config.innovad]
    if config.datadir:
        command.append(f"-datadir={config.datadir}")
    if config.conf:
        command.append(f"-conf={config.conf}")
    return run(command + args, config.timeout if timeout is None else timeout)


def rpc_json(config: Config, method: str, timeout: int | None = None) -> tuple[Any | None, str | None]:
    """One RPC call with its own budget. Never raises: returns (value, error)."""
    code, output, error = rpc(config, [method], timeout)
    if code != 0:
        return None, error or output or f"{method} failed"
    try:
        return json.loads(output), None
    except json.JSONDecodeError:
        return None, f"{method} returned invalid JSON"


def process_pid(config: Config) -> int | None:
    # An RPC call is a short-lived `innovad <method>` child, so pidof/pgrep can
    # return that child instead of the daemon.  The daemon would then look
    # "restarted" on the next refresh and every cache would be dropped
    # (observed: repeated "PID changed" notices and an empty snapshot).  The
    # daemon is always the longest-lived innovad process.
    for command in (
        ["systemctl", "show", "innovad.service", "--property=MainPID", "--value"],
        ["systemctl", "show", "innova-live.service", "--property=MainPID", "--value"],
    ):
        code, output, _ = run(command, config.timeout)
        candidate = output.split()[0] if output else ""
        if code == 0 and candidate.isdigit() and int(candidate) > 0:
            return int(candidate)

    code, output, _ = run(["ps", "-eo", "pid=,etimes=,comm="], config.timeout)
    if code != 0:
        return None
    best_pid: int | None = None
    best_age = -1
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        if Path(parts[2]).name != "innovad":
            continue
        age = int(parts[1])
        if age > best_age:
            best_pid, best_age = int(parts[0]), age
    return best_pid


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


class RpcSource:
    """One RPC metric family with its own cadence and its own timeout budget.

    Every source owns a TimedValue, so a slow or failing probe can neither
    delay nor invalidate any other metric.
    """

    def __init__(self, name: str, method: str, interval: int, timeout: int, validator) -> None:
        self.name = name
        self.method = method
        self.interval = interval
        self.timeout = timeout
        self.validator = validator
        self.state = TimedValue()

    def poll(self, config: Config, fetcher=None) -> bool:
        if fetcher is None:
            value, error = rpc_json(config, self.method, self.timeout)
        else:
            value, error = fetcher(self.method)
        if error is None and self.validator(value):
            self.state.success(value)
            return True
        self.state.failure(f"RPC busy: {self.method} unavailable ({error or 'invalid response'})")
        return False


class LogHeightReader:
    """Incremental, bounded fallback reader for debug.log height.

    The first read scans at most ``initial_scan_bytes`` backwards. Every later
    read consumes only the bytes appended since the previous read, capped by
    ``max_bytes_per_pass``; the file is never re-parsed in full, so a
    multi-hundred-megabyte log cannot stall the refresh loop. A rotation or
    truncation (new inode, smaller size) re-seeds the window.
    """

    def __init__(self, initial_scan_bytes: int = 8 * 1024 * 1024,
                 max_bytes_per_pass: int = 1024 * 1024) -> None:
        self.initial_scan_bytes = max(4096, initial_scan_bytes)
        self.max_bytes_per_pass = max(4096, max_bytes_per_pass)
        self._key: tuple[int, int] | None = None
        self._offset = 0

    def read(self, path: Path | None) -> tuple[int | None, dt.datetime | None]:
        if path is None:
            return None, None
        try:
            stat = path.stat()
        except OSError:
            return None, None
        key = (stat.st_dev, stat.st_ino)
        if key != self._key or stat.st_size < self._offset:
            self._key = key
            self._offset = max(0, stat.st_size - self.initial_scan_bytes)
        try:
            with path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read(self.max_bytes_per_pass)
                self._offset = handle.tell()
        except OSError:
            return None, None
        matches = list(HEIGHT_RE.finditer(chunk))
        height = int(matches[-1].group(1)) if matches else None
        return height, dt.datetime.fromtimestamp(stat.st_mtime).astimezone()


def read_log_height(path: Path | None, tail_bytes: int) -> tuple[int | None, dt.datetime | None]:
    """Stateless bounded scan (compatibility helper for tests and one-shot use)."""
    return LogHeightReader(initial_scan_bytes=max(4096, tail_bytes)).read(path)


def _valid_int(value: Any) -> bool:
    return isinstance(value, bool) is False and as_int(value) is not None


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


def human_version(value: Any) -> Any:
    """Human-facing release version.

    The daemon reports a four-component ``CLIENT_VERSION_*`` string (e.g.
    ``v5.0.3.0``). The fourth component is the internal BUILD number and is not
    part of the normal release label when it is zero, so ``v5.0.3.0`` is shown
    as ``v5.0.3``. Only an exactly-four-component string whose last component is
    ``0`` is shortened: arbitrary trailing components are never removed, and a
    non-zero build component is preserved verbatim. Non-matching values are
    returned unchanged.
    """
    if not isinstance(value, str):
        return value
    match = re.match(r"^(v?)(\d+)\.(\d+)\.(\d+)\.(\d+)$", value.strip())
    if match and match.group(5) == "0":
        return f"{match.group(1)}{match.group(2)}.{match.group(3)}.{match.group(4)}"
    return value


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
                   traffic: TrafficTracker | None = None,
                   daemon: DaemonState | None = None,
                   last_known_height: int | None = None,
                   height_state: TimedValue | None = None,
                   log_state: TimedValue | None = None) -> dict[str, Any]:
    now = dt.datetime.now().astimezone()
    info = info_state.value if isinstance(info_state.value, dict) else {}
    peers = peers_state.value if isinstance(peers_state.value, list) else None
    blockchain = blockchain_state.value if isinstance(blockchain_state.value, dict) else {}

    daemon = daemon or DaemonState()
    running = daemon.state == DAEMON_RUNNING
    online = True if running else (False if daemon.state == DAEMON_STOPPED else None)

    # Height policy: RPC is authoritative; the (bounded) log is a fallback;
    # a cached value is only ever surfaced with its age and a stale marker.
    rpc_height = as_int(height_state.value) if height_state is not None else None
    if rpc_height is None:
        rpc_height = as_int(info.get("blocks"))
    log_height = as_int(log_state.value) if log_state is not None else None
    log_age = log_state.age_seconds() if log_state is not None else None
    height: int | None = None
    height_source: str | None = None
    height_ts: dt.datetime | None = None
    if rpc_height is not None:
        height = rpc_height
        height_source = "rpc"
        height_ts = height_state.updated_wall if height_state is not None else info_state.updated_wall
    elif log_height is not None and (log_age is None or log_age <= max(60, config.refresh * 6)):
        height = log_height
        height_source = "debug_log"
        height_ts = log_state.updated_wall if log_state is not None else None
    elif last_known_height is not None:
        height = last_known_height
        height_source = "cache"
        height_ts = None
    height_age = None
    if height is not None and height_ts is not None:
        height_age = max(0, int((now - height_ts).total_seconds()))
    height_stale = bool(height_source == "cache" or
                        (height_age is not None and height_age > max(60, config.refresh * 3)))
    if not running:
        height = None
        height_source = None
        height_age = None
        height_stale = False

    ibd_rpc = blockchain.get("initialblockdownload") if isinstance(blockchain.get("initialblockdownload"), bool) else info.get("initialblockdownload")

    # Independent freshness axes: the chain verdict may only be as trustworthy as
    # the chain RPC that produced it, and the peer view is a *separate* source.
    chain_age_seconds = blockchain_state.age_seconds()
    chain_max_age = chain_state_max_age(config.info_interval)
    chain_fresh = chain_age_seconds is not None and chain_age_seconds <= chain_max_age
    chain_failures = blockchain_state.failures
    peers_age_seconds = peers_state.age_seconds()
    tip_info = confirmed_tip_from_peers(peers) if peers is not None else {
        "confirmed_tip": None, "support": 0, "max_peer_tip": None, "announcements": 0,
    }
    confirmed_tip = tip_info["confirmed_tip"]
    behind_confirmed = 0
    if confirmed_tip is not None and height is not None and confirmed_tip > height:
        behind_confirmed = confirmed_tip - height

    pid = daemon.pid if running else None
    started_at, uptime_seconds = process_times(pid, config)
    inbound = sum(1 for peer in peers if peer.get("inbound") is True) if peers is not None else None
    outbound = sum(1 for peer in peers if peer.get("inbound") is False) if peers is not None else None
    pings = [as_float(peer.get("pingtime")) for peer in peers] if peers is not None else None
    pings = [ping for ping in pings if ping is not None] if pings else None

    rpc_ok = info_state.failures == 0 or peers_state.failures == 0
    rpc_failures = max(info_state.failures, peers_state.failures)

    if not height_stale:
        ibd_tracker.update(height)
    current_rate = sampler.current_rate()
    ibd_tracker.update_ema(current_rate)

    if peers is None:
        peer_agg = {
            "active_download_peers": None,
            "total_blocks_inflight": None,
            "total_ask_queue": None,
            "details": None,
        }
    else:
        peer_agg = aggregate_peers(peers)
    peer_snap = peer_snapshot_state(peers_state, config.peer_interval)
    if not running and peer_snap.get("available"):
        peer_snap = dict(peer_snap, fresh=False, stale=True)
    estimated_tip = estimate_network_height(peers, height) if peers is not None else None

    traffic_received = parse_bytes(info.get("datareceived"))
    traffic_sent = parse_bytes(info.get("datasent"))
    traffic_source = "getinfo"
    if traffic_received is None and peers:
        traffic_received = sum(p["bytes_received"] or 0 for p in (peer_agg["details"] or []))
        traffic_source = "peers"
    if traffic_sent is None and peers:
        traffic_sent = sum(p["bytes_sent"] or 0 for p in (peer_agg["details"] or []))
        traffic_source = "peers"
    if traffic_received is None and traffic_sent is None:
        traffic_source = None
    if not running:
        traffic_received = None
        traffic_sent = None
        traffic_source = None
        rx_rate = None
        tx_rate = None
    else:
        if traffic is not None:
            traffic.record(traffic_received, traffic_sent)
        rx_rate, tx_rate = traffic.rates() if traffic is not None else (None, None)

    ibd_tracker.update_peaks(
        as_int(info.get("connections")) or (len(peers) if isinstance(peers, list) and peers else None),
        peer_agg["total_blocks_inflight"],
        peer_agg["total_ask_queue"],
    )

    sync_phase = resolve_sync_phase(
        ibd_rpc=ibd_rpc, chain_fresh=chain_fresh, chain_failures=chain_failures,
        rpc_failures=rpc_failures, height=height, height_stale=height_stale,
        behind_confirmed=behind_confirmed, running=running, require_height=True,
    )
    sync_state = ibd_tracker.sync_state(
        ibd_rpc, rpc_failures, chain_age_seconds=chain_age_seconds,
        chain_failures=chain_failures, max_chain_age_seconds=chain_max_age,
        height=height, height_stale=height_stale,
        behind_confirmed=behind_confirmed, running=running,
    )

    if sync_phase == "synced":
        sync_reason = "fresh_chain_state_at_confirmed_tip"
    elif sync_phase == "syncing":
        sync_reason = "ibd_in_progress" if ibd_rpc is True else "behind_confirmed_tip"
    elif sync_phase == "degraded":
        sync_reason = "chain_state_stale" if not chain_fresh else "height_unavailable"
    elif sync_phase == "rpc_unavailable":
        sync_reason = "chain_rpc_unavailable"
    else:
        sync_reason = "no_chain_state"

    session_start = ibd_tracker.session_start_height
    session_type = "unknown"
    if session_start is not None:
        if session_start < 1000:
            session_type = "cold_ibd"
        else:
            session_type = "resume_ibd"
    if sync_phase == "synced":
        session_type = "synced"

    session_rate = sampler.session_average(session_start) if session_start is not None else None
    rate_5min = sampler.window_average(300)
    eta_seconds = compute_eta(ibd_tracker.ema_rate, height, estimated_tip)

    last_advance = ibd_tracker.last_advance_time()
    last_advance_ago = None
    if last_advance:
        last_advance_ago = max(0, int(time.time() - last_advance))

    notices = [notice for notice in (info_state.warning, peers_state.warning, blockchain_state.warning) if notice]
    if running and sync_phase == "degraded":
        age_label = f"{chain_age_seconds}s" if chain_age_seconds is not None else "unknown"
        notices.append(f"Chain state is {age_label} old (limit {chain_max_age}s); sync status is not asserted")
    elif running and sync_phase == "rpc_unavailable" and not blockchain_state.warning:
        notices.append("RPC unavailable: chain state unknown")
    if running and behind_confirmed >= CONFIRMED_LAG_BLOCKS:
        notices.append(f"Behind the confirmed peer tip by {behind_confirmed} blocks")

    return {
        "api": {"name": "innova-node-dashboard", "version": API_VERSION, "schema": 7},
        "dashboard": {"version": APP_VERSION},
        "generated_at": now.isoformat(),
        "node": {
            "online": online,
            "network": "mainnet",
            "version": human_version(info.get("version")),
            "build_commit": info.get("buildcommit"),
            "build_dirty": info.get("builddirty"),
            "protocol_version": info.get("protocolversion"),
            "started_at": iso(started_at),
            "uptime_seconds": uptime_seconds,
        },
        "chain": {
            "height": height,
            "height_source": height_source,
            "height_updated_at": iso(height_ts),
            "height_age_seconds": height_age,
            "height_stale": height_stale,
            "last_known_height": last_known_height,
            "initial_block_download": ibd_rpc,
            # Age of the chain-RPC state this verdict rests on: the value above is
            # only meaningful while ``state_fresh`` is true.
            "state_updated_at": iso(blockchain_state.updated_wall),
            "state_age_seconds": chain_age_seconds,
            "state_fresh": chain_fresh,
            "state_max_age_seconds": chain_max_age,
            "confirmed_tip": confirmed_tip,
            "confirmed_tip_support": tip_info["support"],
            "peer_announcements": tip_info["announcements"],
            "unconfirmed_peer_tip": tip_info["max_peer_tip"],
            "behind_confirmed_blocks": behind_confirmed,
            "difficulty": info.get("difficulty"),
            "money_supply": info.get("moneysupply"),
            "verification_progress": as_float(blockchain.get("verificationprogress")),
        },
        "network": {
            # getinfo owns the authoritative connection count; the peer list is
            # only a fallback when getinfo has no value yet. Both are fetched by
            # independent pollers, so neither waits on the other.
            "connections": (as_int(info.get("connections")) if info.get("connections") is not None else (len(peers) if isinstance(peers, list) and peers else None)) if running else None,
            "inbound": inbound if running else None,
            "outbound": outbound if running else None,
            "average_ping_ms": round(sum(pings) / len(pings) * 1000, 2) if pings and running else None,
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
        "peers": peer_snap,
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
            "sync_phase": sync_phase,
            "sync_reason": sync_reason,
            "sync_basis": {
                "chain_rpc_age_seconds": chain_age_seconds,
                "chain_rpc_fresh": chain_fresh,
                "chain_rpc_max_age_seconds": chain_max_age,
                "chain_rpc_failures": chain_failures,
                "peers_age_seconds": peers_age_seconds,
                "peers_fresh": peer_snap.get("fresh"),
                "height_source": height_source,
                "height_age_seconds": height_age,
                "height_stale": height_stale,
                "behind_confirmed_blocks": behind_confirmed,
            },
            "peer_aggregation": {
                "active_download_peers": peer_agg["active_download_peers"],
                "total_blocks_inflight": peer_agg["total_blocks_inflight"],
                "total_ask_queue": peer_agg["total_ask_queue"],
                "peer_count": len(peers) if isinstance(peers, list) else None,
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
            "peers": peers is not None,
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

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

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
    parser.add_argument("--rpc-timeout-fast", type=int,
                        default=env_int("INNOVA_DASHBOARD_RPC_TIMEOUT_FAST", 6))
    parser.add_argument("--rpc-timeout-slow", type=int,
                        default=env_int("INNOVA_DASHBOARD_RPC_TIMEOUT_SLOW",
                                        env_int("INNOVA_DASHBOARD_RPC_TIMEOUT", 45)))
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
        timeout_fast=max(2, args.rpc_timeout_fast),
        timeout_slow=max(args.rpc_timeout_fast, args.rpc_timeout_slow),
    )

    server = Server((config.host, config.port), Handler)
    server.config = config
    server.collector = Collector(config)
    # One bounded fast probe so the first snapshot already has a height, then
    # every metric refreshes on its own thread.
    server.collector.poll_source("height")
    server.collector.start()
    server.snapshot_lock = threading.Lock()
    server.snapshot_data = None
    server.snapshot_updated = 0.0
    print(f"Innova Node Dashboard {APP_VERSION} - http://{config.host}:{config.port}")
    print(f"innovad: {config.innovad}")
    print(f"debug.log: {config.debug_log}")
    print(f"RPC intervals: height={config.refresh}s getinfo={config.info_interval}s "
          f"getpeerinfo={config.peer_interval}s")
    print(f"RPC timeouts: fast={config.timeout_fast}s slow={config.timeout_slow}s "
          f"log_tail={config.log_tail_bytes}B")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.collector.stop()
        server.server_close()


if __name__ == "__main__":
    main()
