#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

APP_VERSION = "0.2.0"
API_VERSION = "v1"


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    refresh: int
    timeout: int
    innovad: str
    datadir: str | None
    conf: str | None
    frontend: Path


class Cache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: dict[str, Any] | None = None
        self.updated = 0.0

    def get(self) -> dict[str, Any] | None:
        with self.lock:
            return self.data

    def set(self, data: dict[str, Any]) -> None:
        with self.lock:
            self.data = data
            self.updated = time.monotonic()

    def age(self) -> float:
        with self.lock:
            return float("inf") if self.data is None else time.monotonic() - self.updated


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
    except (OSError, subprocess.TimeoutExpired) as exc:
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

    code, output, _ = run(
        ["ps", "-p", str(pid), "-o", "etimes="],
        config.timeout,
    )
    uptime_seconds = int(output) if code == 0 and output.isdigit() else None

    code, output, _ = run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        config.timeout,
    )
    started_at = None
    if code == 0 and output:
        try:
            parsed = dt.datetime.strptime(output, "%a %b %d %H:%M:%S %Y")
            started_at = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
        except ValueError:
            pass

    # etimes is the authoritative uptime. Derive a start timestamp if lstart
    # is unavailable, but never derive uptime from filesystem metadata.
    if started_at is None and uptime_seconds is not None:
        started_at = dt.datetime.now().astimezone() - dt.timedelta(seconds=uptime_seconds)

    return started_at, uptime_seconds


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


def collect(config: Config) -> dict[str, Any]:
    now = dt.datetime.now().astimezone()
    errors: list[str] = []
    info: dict[str, Any] = {}
    peers: list[dict[str, Any]] = []

    code, output, error = rpc(config, ["getinfo"])
    if code == 0:
        try:
            value = json.loads(output)
            if isinstance(value, dict):
                info = value
            else:
                errors.append("getinfo returned invalid JSON")
        except json.JSONDecodeError:
            errors.append("getinfo returned invalid JSON")
    else:
        errors.append(error or output or "getinfo failed")

    code, output, error = rpc(config, ["getpeerinfo"])
    if code == 0 and output:
        try:
            value = json.loads(output)
            if isinstance(value, list):
                peers = [peer for peer in value if isinstance(peer, dict)]
            else:
                errors.append("getpeerinfo returned invalid JSON")
        except json.JSONDecodeError:
            errors.append("getpeerinfo returned invalid JSON")
    elif error or output:
        errors.append(error or output)

    pid = process_pid(config)
    started_at, uptime_seconds = process_times(pid, config)
    height = as_int(info.get("blocks"))

    inbound = sum(1 for peer in peers if peer.get("inbound") is True) if peers else None
    outbound = sum(1 for peer in peers if peer.get("inbound") is False) if peers else None
    pings = [as_float(peer.get("pingtime")) for peer in peers]
    pings = [ping for ping in pings if ping is not None]

    return {
        "api": {"name": "innova-node-dashboard", "version": API_VERSION, "schema": 1},
        "dashboard": {"version": APP_VERSION},
        "generated_at": now.isoformat(),
        "node": {
            "online": height is not None,
            "network": "mainnet",
            "version": info.get("version"),
            "build_commit": info.get("buildcommit"),
            "build_dirty": info.get("builddirty"),
            "protocol_version": info.get("protocolversion"),
            "pid": pid,
            "started_at": started_at.isoformat() if started_at else None,
            "uptime_seconds": uptime_seconds,
        },
        "chain": {
            "height": height,
            "initial_block_download": info.get("initialblockdownload"),
            "difficulty": info.get("difficulty"),
            "money_supply": info.get("moneysupply"),
        },
        "network": {
            "connections": as_int(info.get("connections"))
            if info.get("connections") is not None
            else (len(peers) if peers else None),
            "inbound": inbound,
            "outbound": outbound,
            "average_ping_ms": round(sum(pings) / len(pings) * 1000, 2) if pings else None,
            "bytes_received": info.get("datareceived"),
            "bytes_sent": info.get("datasent"),
        },
        "system": {"cpu_percent": None, "memory_bytes": None, "disk": None},
        "features": {
            "peers": bool(peers),
            "traffic": info.get("datareceived") is not None or info.get("datasent") is not None,
            "system_metrics": False,
        },
        "errors": errors,
    }


class Server(ThreadingHTTPServer):
    config: Config
    cache: Cache


class Handler(BaseHTTPRequestHandler):
    server_version = f"InnovaDashboard/{APP_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    @property
    def app(self) -> Server:
        return self.server  # type: ignore[return-value]

    def send_data(
        self,
        payload: bytes,
        content_type: str,
        status: int = HTTPStatus.OK,
        cache: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
            "base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(payload)

    def snapshot(self) -> dict[str, Any]:
        if self.app.cache.age() > self.app.config.refresh:
            self.app.cache.set(collect(self.app.config))
        snapshot = self.app.cache.get()
        if snapshot is None:
            snapshot = collect(self.app.config)
            self.app.cache.set(snapshot)
        return snapshot

    def static(self, relative: str) -> None:
        base = self.app.config.frontend.resolve()
        path = (base / relative).resolve()
        if base not in path.parents and path != base:
            self.send_data(b"forbidden\n", "text/plain", HTTPStatus.FORBIDDEN)
            return
        if not path.is_file():
            self.send_data(b"not found\n", "text/plain", HTTPStatus.NOT_FOUND)
            return
        types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }
        cache = "no-cache" if path.name == "index.html" else "public, max-age=3600"
        self.send_data(path.read_bytes(), types.get(path.suffix, "application/octet-stream"), cache=cache)

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self.static("index.html")
            return
        if route == "/api/v1/status":
            payload = json.dumps(self.snapshot(), ensure_ascii=False, separators=(",", ":")).encode()
            self.send_data(payload, "application/json; charset=utf-8")
            return
        if route == "/health":
            snapshot = self.snapshot()
            ok = bool(snapshot.get("node", {}).get("online"))
            payload = json.dumps({"ok": ok, "generated_at": snapshot.get("generated_at")}).encode()
            self.send_data(payload, "application/json", HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if route.startswith("/assets/"):
            self.static(route.lstrip("/"))
            return
        self.send_data(b"not found\n", "text/plain", HTTPStatus.NOT_FOUND)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Innova node dashboard")
    parser.add_argument("--host", default=os.getenv("INNOVA_DASHBOARD_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("INNOVA_DASHBOARD_PORT", "8787")))
    parser.add_argument("--refresh", type=int, default=int(os.getenv("INNOVA_DASHBOARD_REFRESH", "5")))
    parser.add_argument("--rpc-timeout", type=int, default=int(os.getenv("INNOVA_DASHBOARD_RPC_TIMEOUT", "8")))
    parser.add_argument("--innovad", default=os.getenv("INNOVAD_PATH"))
    parser.add_argument("--datadir", default=os.getenv("INNOVA_DATADIR"))
    parser.add_argument("--conf", default=os.getenv("INNOVA_CONF"))
    parser.add_argument("--frontend-dir", default=os.getenv("INNOVA_DASHBOARD_FRONTEND"))
    args = parser.parse_args()

    backend = Path(__file__).resolve().parent
    config = Config(
        args.host,
        args.port,
        max(1, args.refresh),
        max(1, args.rpc_timeout),
        locate_innovad(args.innovad),
        args.datadir,
        args.conf,
        Path(args.frontend_dir).expanduser() if args.frontend_dir else backend.parent / "frontend",
    )

    server = Server((config.host, config.port), Handler)
    server.config = config
    server.cache = Cache()
    print(f"Innova Node Dashboard {APP_VERSION} - http://{config.host}:{config.port}")
    print(f"innovad: {config.innovad}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
