#!/usr/bin/env python3
"""Regression tests for peer snapshot availability/freshness semantics."""
from __future__ import annotations

import datetime as dt
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from server import (
    IBDTracker,
    IBDSampler,
    Collector,
    Config,
    TimedValue,
    build_snapshot,
    peer_snapshot_state,
)


def _make_config():
    return Config(
        host="127.0.0.1", port=8787, refresh=5, timeout=5,
        info_interval=60, peer_interval=120, max_backoff=30,
        log_tail_bytes=262144, innovad="innovad", datadir=None,
        conf=None, debug_log=None, frontend=Path("/dev/null"),
    )


def _snap(info=None, peers=None, bc=None):
    config = _make_config()
    info_state = TimedValue()
    if info is not None:
        info_state.success(info)
    peers_state = peers if isinstance(peers, TimedValue) else TimedValue()
    blockchain = TimedValue()
    if bc is not None:
        blockchain.success(bc)
    tracker = IBDTracker()
    sampler = IBDSampler()
    with patch("server.process_times", return_value=(None, None)), \
         patch("server.process_pid", return_value=None), \
         patch("server.read_log_height", return_value=(None, None)):
        return build_snapshot(config, info_state, peers_state, blockchain,
                              tracker, sampler, metrics={"cpu_count": 2})


def _peer_timed(value, failures=0, age_seconds=0):
    state = TimedValue()
    state.success(value)
    if failures:
        for _ in range(failures):
            state.failure("RPC busy: getpeerinfo unavailable (test)")
    state.updated_wall = dt.datetime.now().astimezone() - dt.timedelta(seconds=age_seconds)
    return state


class TestPeerSnapshotState(unittest.TestCase):
    def test_never_fetched_is_unavailable(self):
        state = peer_snapshot_state(TimedValue(), 120)
        self.assertFalse(state["available"])
        self.assertFalse(state["fresh"])
        self.assertFalse(state["stale"])
        self.assertIsNone(state["count"])
        self.assertIsNone(state["items"])
        self.assertIsNone(state["age_seconds"])
        self.assertIsNone(state["last_success_at"])
        self.assertIsNone(state["last_error"])

    def test_failed_never_fetched_is_unavailable(self):
        state = TimedValue()
        state.failure("RPC busy")
        snap = peer_snapshot_state(state, 120)
        self.assertFalse(snap["available"])
        self.assertEqual(snap["last_error"], "RPC busy")

    def test_empty_list_is_available_zero(self):
        state = TimedValue()
        state.success([])
        snap = peer_snapshot_state(state, 120)
        self.assertTrue(snap["available"])
        self.assertTrue(snap["fresh"])
        self.assertEqual(snap["count"], 0)
        self.assertEqual(snap["items"], [])

    def test_fresh_success(self):
        snap = peer_snapshot_state(_peer_timed([{"addr": "a"}], age_seconds=10), 120)
        self.assertTrue(snap["available"])
        self.assertTrue(snap["fresh"])
        self.assertFalse(snap["stale"])
        self.assertEqual(snap["count"], 1)
        self.assertEqual(snap["age_seconds"], 10)

    def test_stale_after_failure(self):
        snap = peer_snapshot_state(_peer_timed([{"addr": "a"}], failures=1), 120)
        self.assertTrue(snap["available"])
        self.assertFalse(snap["fresh"])
        self.assertTrue(snap["stale"])
        self.assertEqual(snap["count"], 1)
        self.assertIsNotNone(snap["last_error"])

    def test_stale_after_aging(self):
        snap = peer_snapshot_state(_peer_timed([{"addr": "a"}], age_seconds=300), 120)
        self.assertTrue(snap["available"])
        self.assertFalse(snap["fresh"])
        self.assertTrue(snap["stale"])
        self.assertEqual(snap["age_seconds"], 300)

    def test_fresh_again_after_backoff_recovery(self):
        state = _peer_timed([{"addr": "a"}], failures=2, age_seconds=300)
        self.assertTrue(peer_snapshot_state(state, 120)["stale"])
        state.success([{"addr": "a"}, {"addr": "b"}])
        snap = peer_snapshot_state(state, 120)
        self.assertTrue(snap["available"])
        self.assertTrue(snap["fresh"])
        self.assertEqual(snap["count"], 2)


class TestBuildSnapshotPeerSemantics(unittest.TestCase):
    def test_no_peer_success_yet_connections_still_known(self):
        snap = _snap(info={"blocks": 1000, "connections": 2},
                     bc={"initialblockdownload": True})
        self.assertEqual(snap["network"]["connections"], 2)
        peers = snap["peers"]
        self.assertFalse(peers["available"])
        self.assertIsNone(peers["count"])
        self.assertIsNone(peers["items"])
        self.assertIsNone(snap["network"]["inbound"])
        self.assertIsNone(snap["network"]["outbound"])
        self.assertIsNone(snap["network"]["average_ping_ms"])
        agg = snap["ibd"]["peer_aggregation"]
        self.assertIsNone(agg["peer_count"])
        self.assertIsNone(agg["active_download_peers"])
        self.assertIsNone(agg["total_blocks_inflight"])
        self.assertIsNone(agg["total_ask_queue"])
        self.assertIsNone(snap["ibd"]["peers"])
        self.assertFalse(snap["features"]["peers"])

    def test_empty_peer_result_is_available_zero(self):
        snap = _snap(info={"blocks": 1000, "connections": 2},
                     peers=_peer_timed([]),
                     bc={"initialblockdownload": True})
        peers = snap["peers"]
        self.assertTrue(peers["available"])
        self.assertEqual(peers["count"], 0)
        self.assertEqual(peers["items"], [])
        self.assertEqual(snap["ibd"]["peer_aggregation"]["peer_count"], 0)

    def test_three_peers_derive_values(self):
        peers = [
            {"inbound": True, "pingtime": 0.05, "blocksinflight": 2, "askqueuesize": 3, "addr": "a"},
            {"inbound": False, "pingtime": 0.10, "blocksinflight": 0, "askqueuesize": 1, "addr": "b"},
            {"inbound": False, "pingtime": 0.08, "blocksinflight": 1, "askqueuesize": 0, "addr": "c"},
        ]
        snap = _snap(info={"blocks": 1000, "connections": 2},
                     peers=_peer_timed(peers),
                     bc={"initialblockdownload": True})
        peers_snap = snap["peers"]
        self.assertTrue(peers_snap["available"])
        self.assertTrue(peers_snap["fresh"])
        self.assertEqual(peers_snap["count"], 3)
        self.assertEqual(len(peers_snap["items"]), 3)
        agg = snap["ibd"]["peer_aggregation"]
        self.assertEqual(agg["peer_count"], 3)
        self.assertEqual(agg["active_download_peers"], 2)
        self.assertEqual(agg["total_blocks_inflight"], 3)
        self.assertEqual(agg["total_ask_queue"], 4)
        self.assertEqual(snap["network"]["inbound"], 1)
        self.assertEqual(snap["network"]["outbound"], 2)
        self.assertIsNotNone(snap["network"]["average_ping_ms"])

    def test_success_then_timeout_preserves_snapshot(self):
        state = _peer_timed([
            {"inbound": False, "blocksinflight": 1, "addr": "a"},
            {"inbound": False, "blocksinflight": 0, "addr": "b"},
            {"inbound": True, "blocksinflight": 0, "addr": "c"},
        ])
        state.failure("RPC busy: getpeerinfo unavailable (timed out after 8 seconds)")
        snap = _snap(info={"blocks": 1000, "connections": 2}, peers=state,
                     bc={"initialblockdownload": True})
        peers = snap["peers"]
        self.assertTrue(peers["available"])
        self.assertFalse(peers["fresh"])
        self.assertTrue(peers["stale"])
        self.assertEqual(peers["count"], 3)
        self.assertEqual(len(peers["items"]), 3)
        self.assertEqual(snap["ibd"]["peer_aggregation"]["active_download_peers"], 1)
        self.assertIsNotNone(peers["last_error"])

    def test_startup_timeout_no_false_zeros(self):
        state = TimedValue()
        state.failure("RPC busy: getpeerinfo unavailable (timed out after 8 seconds)")
        snap = _snap(info={"blocks": 1000, "connections": 2}, peers=state,
                     bc={"initialblockdownload": True})
        peers = snap["peers"]
        self.assertFalse(peers["available"])
        self.assertIsNone(peers["count"])
        self.assertIsNone(snap["network"]["inbound"])
        self.assertIsNone(snap["network"]["outbound"])
        self.assertIsNone(snap["ibd"]["peer_aggregation"]["peer_count"])

    def test_backoff_window_keeps_semantics(self):
        state = _peer_timed([{"addr": "a", "inbound": False}], failures=4, age_seconds=600)
        snap = _snap(info={"blocks": 1000, "connections": 2}, peers=state,
                     bc={"initialblockdownload": True})
        self.assertTrue(snap["peers"]["stale"])
        self.assertEqual(snap["peers"]["count"], 1)
        self.assertEqual(snap["peers"]["age_seconds"], 600)

    def test_recovery_after_backoff_returns_to_fresh(self):
        state = _peer_timed([{"addr": "a"}], failures=2, age_seconds=600)
        state.success([{"addr": "a"}, {"addr": "b"}, {"addr": "c"}])
        snap = _snap(info={"blocks": 1000, "connections": 2}, peers=state,
                     bc={"initialblockdownload": True})
        peers = snap["peers"]
        self.assertTrue(peers["available"])
        self.assertTrue(peers["fresh"])
        self.assertFalse(peers["stale"])
        self.assertEqual(peers["count"], 3)

    def test_connections_remain_independent_of_peer_data(self):
        snap = _snap(info={"blocks": 1000, "connections": 7}, bc={"initialblockdownload": True})
        self.assertEqual(snap["network"]["connections"], 7)
        self.assertFalse(snap["peers"]["available"])


class TestTimedValueInvalidate(unittest.TestCase):
    def test_invalidate_drops_value_and_reschedules(self):
        state = _peer_timed([{"addr": "a"}])
        state.invalidate("innovad process restarted (PID changed)")
        self.assertIsNone(state.value)
        self.assertIsNone(state.updated_wall)
        self.assertEqual(state.failures, 0)
        self.assertTrue(state.due(120, 30))

    def test_invalidate_warning_surfaces_in_snapshot(self):
        state = TimedValue()
        state.success([{"addr": "a"}])
        state.invalidate("innovad process restarted (PID changed)")
        snap = _snap(info={"blocks": 1000, "connections": 2}, peers=state,
                     bc={"initialblockdownload": True})
        self.assertFalse(snap["peers"]["available"])
        self.assertEqual(snap["peers"]["last_error"], "innovad process restarted (PID changed)")
        self.assertIn("innovad process restarted", " ".join(snap["notices"]))


class TestCollectorDaemonRestart(unittest.TestCase):
    def test_pid_change_invalidates_peer_cache(self):
        collector = Collector(_make_config())
        collector.peers.success([{"addr": "a"}])
        collector._observed_pid = 100
        collector.check_daemon_restart(200)
        self.assertIsNone(collector.peers.value)
        self.assertEqual(collector._observed_pid, 200)

    def test_stable_pid_keeps_peer_cache(self):
        collector = Collector(_make_config())
        collector.peers.success([{"addr": "a"}])
        collector._observed_pid = 100
        collector.check_daemon_restart(100)
        self.assertEqual(collector.peers.value, [{"addr": "a"}])

    def test_first_observation_does_not_invalidate(self):
        collector = Collector(_make_config())
        collector.peers.success([{"addr": "a"}])
        collector.check_daemon_restart(300)
        self.assertEqual(collector.peers.value, [{"addr": "a"}])
        self.assertEqual(collector._observed_pid, 300)


class TestCollectorPipeline(unittest.TestCase):
    """Full refresh_rpc -> build_snapshot pipeline, mirrors the IBD startup bug."""

    def _config(self):
        return _make_config()

    def test_ibd_startup_busy_peers_not_zero(self):
        col = Collector(self._config())

        def rpc(method):
            if method == "getinfo":
                return {"blocks": 1000, "connections": 2}, None
            if method == "getblockchaininfo":
                return {"initialblockdownload": True}, None
            return None, "RPC busy: timed out after 8 seconds"

        with patch.object(col, "rpc_json", side_effect=rpc), \
             patch("server.process_pid", return_value=999):
            snap = col.collect()
        self.assertEqual(snap["network"]["connections"], 2)
        self.assertFalse(snap["peers"]["available"])
        self.assertIsNone(snap["peers"]["count"])
        self.assertIsNone(snap["ibd"]["peer_aggregation"]["peer_count"])
        self.assertIsNone(snap["ibd"]["peer_aggregation"]["active_download_peers"])
        self.assertIsNone(snap["network"]["inbound"])
        self.assertEqual(snap["freshness"]["getpeerinfo_failures"], 1)

    def test_backoff_window_keeps_unavailable_and_connections(self):
        col = Collector(self._config())

        def rpc(method):
            if method == "getinfo":
                return {"blocks": 1000, "connections": 2}, None
            if method == "getblockchaininfo":
                return {"initialblockdownload": True}, None
            return None, "RPC busy: timed out after 8 seconds"

        with patch.object(col, "rpc_json", side_effect=rpc), \
             patch("server.process_pid", return_value=999):
            col.collect()
            snap = col.collect()
        self.assertEqual(snap["network"]["connections"], 2)
        self.assertFalse(snap["peers"]["available"])
        self.assertIsNone(snap["peers"]["count"])

    def test_success_then_timeout_pipeline_preserves_snapshot(self):
        col = Collector(self._config())
        col.peers.last_attempt = 0.0

        def rpc(method):
            if method == "getpeerinfo":
                return [{"addr": "1.2.3.4:8333", "inbound": False,
                         "blocksinflight": 2, "askqueuesize": 3, "pingtime": 0.1}], None
            if method == "getinfo":
                return {"blocks": 1000, "connections": 2}, None
            return {"initialblockdownload": True}, None

        with patch.object(col, "rpc_json", side_effect=rpc), \
             patch("server.process_pid", return_value=999):
            ok = col.collect()
        self.assertTrue(ok["peers"]["fresh"])
        self.assertEqual(ok["peers"]["count"], 1)

        col.peers.last_attempt = 0.0

        def rpc_busy(method):
            if method == "getpeerinfo":
                return None, "RPC busy: timed out after 8 seconds"
            return rpc(method)

        with patch.object(col, "rpc_json", side_effect=rpc_busy), \
             patch("server.process_pid", return_value=999):
            stale = col.collect()
        self.assertTrue(stale["peers"]["available"])
        self.assertFalse(stale["peers"]["fresh"])
        self.assertTrue(stale["peers"]["stale"])
        self.assertEqual(stale["peers"]["count"], 1)
        self.assertEqual(len(stale["peers"]["items"]), 1)


if __name__ == "__main__":
    unittest.main()
