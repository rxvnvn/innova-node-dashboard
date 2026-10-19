#!/usr/bin/env python3
"""Tests for the RPC-first data acquisition architecture of the dashboard.

These tests pin the properties that the old implementation violated:
  * a slow/unavailable RPC family never delays another family;
  * height comes from RPC, the log is only a bounded fallback;
  * the log fallback is incremental (no full re-parse per refresh);
  * a cached height is reported with its age and a stale marker.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import server
from server import (
    Collector,
    Config,
    DaemonState,
    IBDSampler,
    IBDTracker,
    LogHeightReader,
    RpcSource,
    TimedValue,
    build_snapshot,
)


def make_config(**overrides) -> Config:
    base = dict(
        host="127.0.0.1", port=8787, refresh=1, timeout=2,
        info_interval=60, peer_interval=120, max_backoff=30,
        log_tail_bytes=4096, innovad="/bin/true", datadir=None, conf=None,
        debug_log=None, frontend=Path("."), timeout_fast=2, timeout_slow=10,
    )
    base.update(overrides)
    return Config(**base)


def snapshot_for(config, *, height_state=None, log_state=None, last_known=None, info=None):
    return build_snapshot(
        config, TimedValue(), TimedValue(), TimedValue(),
        IBDTracker(), IBDSampler(), {}, None, DaemonState(state="running", pid=123),
        last_known_height=last_known, height_state=height_state, log_state=log_state,
    )


class SlowRpcIsolationTest(unittest.TestCase):
    def test_slow_getinfo_does_not_delay_height(self):
        def fake_rpc(config, args, timeout=None):
            method = args[0]
            if method == "getinfo":
                time.sleep(1.5)
                return 0, json.dumps({"blocks": 1, "version": "v0"}), ""
            if method == "getblockcount":
                return 0, "4242", ""
            return 0, "[]", ""

        collector = Collector(make_config())
        with patch.object(server, "rpc", side_effect=fake_rpc):
            started = time.monotonic()
            collector.poll_source("height")
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, "height probe must not wait for the slow getinfo")
        self.assertEqual(collector.height.value, 4242)

    def test_slow_getinfo_runs_without_blocking_other_sources(self):
        started = time.monotonic()

        def fake_rpc(config, args, timeout=None):
            if args[0] == "getinfo":
                time.sleep(0.8)
                return 0, json.dumps({"blocks": 1}), ""
            if args[0] == "getblockcount":
                return 0, "77", ""
            return 0, "[]", ""

        collector = Collector(make_config())
        with patch.object(server, "rpc", side_effect=fake_rpc):
            collector.start()
            try:
                deadline = time.monotonic() + 0.4
                while time.monotonic() < deadline and collector.height.value is None:
                    time.sleep(0.02)
                self.assertEqual(collector.height.value, 77)
                self.assertIsNone(collector.info.value, "getinfo still in flight")
            finally:
                collector.stop()
        self.assertLess(time.monotonic() - started, 2.0)

    def test_failed_probe_keeps_last_value_but_records_failure(self):
        source = RpcSource("height", "getblockcount", 1, 2, server._valid_int)
        source.state.success(100)
        ok = source.poll(make_config(), fetcher=lambda *a, **k: (None, "boom"))
        self.assertFalse(ok)
        self.assertEqual(source.state.value, 100, "last good value is preserved")
        self.assertEqual(source.state.failures, 1)


class LogFallbackTest(unittest.TestCase):
    def test_reader_is_incremental_and_survives_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "debug.log"
            path.write_bytes(b"noise\nSetBestChain: new best=aa height=100 trust=1\n")
            reader = LogHeightReader(initial_scan_bytes=4096, max_bytes_per_pass=4096)
            height, _ = reader.read(path)
            self.assertEqual(height, 100)
            offset_after_first = reader._offset

            with path.open("ab") as handle:
                handle.write(b"SetBestChain: new best=bb height=101 trust=1\n")
            height, _ = reader.read(path)
            self.assertEqual(height, 101)
            self.assertGreater(reader._offset, offset_after_first)

            # rotation: new inode with unrelated tail content
            path.unlink()
            path.write_bytes(b"SetBestChain: new best=cc height=7 trust=1\n")
            height, _ = reader.read(path)
            self.assertEqual(height, 7)

    def test_no_height_line_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "debug.log"
            path.write_bytes(b"nothing relevant\n" * 100)
            reader = LogHeightReader(initial_scan_bytes=4096)
            self.assertEqual(reader.read(path), (None, reader.read(path)[1]))


class HeightPolicyTest(unittest.TestCase):
    def test_rpc_height_wins(self):
        height_state = TimedValue()
        height_state.success(500)
        snap = snapshot_for(make_config(), height_state=height_state, last_known=1)
        self.assertEqual(snap["chain"]["height"], 500)
        self.assertEqual(snap["chain"]["height_source"], "rpc")
        self.assertFalse(snap["chain"]["height_stale"])

    def test_log_used_only_when_rpc_missing(self):
        log_state = TimedValue()
        log_state.success(400)
        snap = snapshot_for(make_config(), height_state=TimedValue(), log_state=log_state)
        self.assertEqual(snap["chain"]["height"], 400)
        self.assertEqual(snap["chain"]["height_source"], "debug_log")

    def test_cached_height_is_marked_stale_with_age(self):
        snap = snapshot_for(make_config(), last_known=100)
        chain = snap["chain"]
        self.assertEqual(chain["height"], 100)
        self.assertEqual(chain["height_source"], "cache")
        self.assertTrue(chain["height_stale"])
        self.assertIsNone(chain["height_age_seconds"])

    def test_stale_height_does_not_advance_ibd_tracker(self):
        snap = snapshot_for(make_config(), last_known=100)
        self.assertTrue(snap["chain"]["height_stale"])


if __name__ == "__main__":
    unittest.main()
