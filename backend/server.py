#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

APP_VERSION = "0.2.1"
API_VERSION = "v1"
HEIGHT_RE = re.compile(rb"SetBestChain:.*?\bheight=(\d+)\b")


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


class Collector:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.info = TimedValue()
        self.peers = TimedValue()

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

    def collect(self) -> dict[str, Any]:
        with self.lock:
            self.refresh_rpc()
            return build_snapshot(self.config, self.info, self.peers)


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


def iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def build_snapshot(config: Config, info_state: TimedValue, peers_state: TimedValue) -> dict[str, Any]:
    now = dt.datetime.now().astimezone()
    info = info_state.value if isinstance(info_state.value, dict) else {}
    peers = peers_state.value if isinstance(peers_state.value, list) else []
    log_height, log_updated = read_log_height(config.debug_log, config.log_tail_bytes)
    rpc_height = as_int(info.get("blocks"))
    height = log_height if log_height is not None else rpc_height
    height_source = "debug_log" if log_height is not None else ("rpc" if rpc_height is not None else None)

    pid = process_pid(config)
    started_at, uptime_seconds = process_times(pid, config)
    inbound = sum(1 for peer in peers if peer.get("inbound") is True) if peers else None
    outbound = sum(1 for peer in peers if peer.get("inbound") is False) if peers else None
    pings = [as_float(peer.get("pingtime")) for peer in peers]
    pings = [ping for ping in pings if ping is not None]

    notices = [notice for notice in (info_state.warning, peers_state.warning) if notice]
    return {
        "api": {"name": "innova-node-dashboard", "version": API_VERSION, "schema": 2},
        "dashboard": {"version": APP_VERSION},
        "generated_at": now.isoformat(),
        "node": {
            "online": pid is not None or height is not None,
            "network": "mainnet",
            "version": info.get("version"),
            "build_commit": info.get("buildcommit"),
            "build_dirty": info.get("builddirty"),
            "protocol_version": info.get("protocolversion"),
            "pid": pid,
            "started_at": iso(started_at),
            "uptime_seconds": uptime_seconds,
        },
        "chain": {
            "height": height,
            "height_source": height_source,
            "height_updated_at": iso(log_updated if height_source == "debug_log" else info_state.updated_wall),
            "initial_block_download": info.get("initialblockdownload"),
            "difficulty": info.get("difficulty"),
            "money_supply": info.get("moneysupply"),
        },
        "network": {
            "connections": as_int(info.get("connections")) if info.get("connections") is not None else (len(peers) if peers else None),
            "inbound": inbound,
            "outbound": outbound,
            "average_ping_ms": round(sum(pings) / len(pings) * 1000, 2) if pings else None,
            "bytes_received": info.get("datareceived"),
            "bytes_sent": info.get("datasent"),
        },
        "freshness": {
            "getinfo_age_seconds": info_state.age_seconds(),
            "getpeerinfo_age_seconds": peers_state.age_seconds(),
            "getinfo_failures": info_state.failures,
            "getpeerinfo_failures": peers_state.failures,
        },
        "features": {
            "peers": bool(peers),
            "traffic": info.get("datareceived") is not None or info.get("datasent") is not None,
            "debug_log_height": log_height is not None,
            "system_metrics": False,
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
