#!/usr/bin/env python3
"""Tests for Innova Node Dashboard computational logic."""
from __future__ import annotations

import collections
import datetime as dt
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from server import (
    IBDSampler,
    IBDTracker,
    TimedValue,
    aggregate_peers,
    as_float,
    as_int,
    build_snapshot,
    compute_eta,
    estimate_network_height,
)


class TestAsInt(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(as_int(42), 42)
        self.assertEqual(as_int("100"), 100)
        self.assertEqual(as_int(0), 0)

    def test_none_and_invalid(self):
        self.assertIsNone(as_int(None))
        self.assertIsNone(as_int("abc"))
        self.assertIsNone(as_int([]))


class TestAsFloat(unittest.TestCase):
    def test_normal(self):
        self.assertAlmostEqual(as_float(3.14), 3.14)
        self.assertAlmostEqual(as_float("2.5"), 2.5)

    def test_none_and_invalid(self):
        self.assertIsNone(as_float(None))
        self.assertIsNone(as_float("abc"))


class TestComputeETA(unittest.TestCase):
    def test_normal(self):
        eta = compute_eta(ema_rate=10.0, current_height=900000, target_height=1000000)
        self.assertIsNotNone(eta)
        self.assertAlmostEqual(eta, 100000 / 10.0 * 60.0, places=0)

    def test_no_data(self):
        self.assertIsNone(compute_eta(None, 900000, 1000000))
        self.assertIsNone(compute_eta(10.0, None, 1000000))
        self.assertIsNone(compute_eta(10.0, 900000, None))

    def test_at_tip(self):
        self.assertIsNone(compute_eta(10.0, 1000000, 1000000))
        self.assertIsNone(compute_eta(10.0, 1100000, 1000000))

    def test_zero_rate(self):
        self.assertIsNone(compute_eta(0.0, 900000, 1000000))

    def test_negative_remaining(self):
        self.assertIsNone(compute_eta(10.0, 1000001, 1000000))


class TestEstimateNetworkHeight(unittest.TestCase):
    def test_basic(self):
        peers = [
            {"startingheight": 1000},
            {"startingheight": 1001},
            {"startingheight": 1002},
        ]
        result = estimate_network_height(peers, 990)
        self.assertIsNotNone(result)
        self.assertEqual(result, 1002)

    def test_filters_zeros(self):
        peers = [{"startingheight": 0}, {"startingheight": 1000}]
        result = estimate_network_height(peers, 990)
        self.assertEqual(result, 1000)

    def test_filters_anomalously_high(self):
        peers = [{"startingheight": 1000}, {"startingheight": 5000}]
        result = estimate_network_height(peers, 1000)
        self.assertIsNotNone(result)
        self.assertLessEqual(result, 2500)

    def test_filters_lagging(self):
        peers = [{"startingheight": 100}, {"startingheight": 1000}]
        result = estimate_network_height(peers, 1000)
        self.assertIsNotNone(result)

    def test_empty_peers(self):
        self.assertIsNone(estimate_network_height([], 1000))

    def test_no_current_height(self):
        peers = [{"startingheight": 1000}]
        self.assertIsNone(estimate_network_height(peers, None))

    def test_single_peer(self):
        peers = [{"startingheight": 42}]
        result = estimate_network_height(peers, 40)
        self.assertEqual(result, 42)

    def test_q75_selection(self):
        peers = [{"startingheight": h} for h in range(100, 110)]
        result = estimate_network_height(peers, 100)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result, 107)

    def test_all_filtered(self):
        peers = [{"startingheight": 0}, {"startingheight": -1}]
        self.assertIsNone(estimate_network_height(peers, 1000))


class TestAggregatePeers(unittest.TestCase):
    def test_basic(self):
        peers = [
            {"blocksinflight": 3, "askqueuesize": 5, "addr": "1.2.3.4:8333",
             "startingheight": 1000, "bytesrecv": 1024, "bytessent": 2048, "pingtime": 0.1},
            {"blocksinflight": 0, "askqueuesize": 2, "addr": "5.6.7.8:8333",
             "startingheight": 999, "bytesrecv": 512, "bytessent": 256, "pingtime": 0.2},
        ]
        result = aggregate_peers(peers)
        self.assertEqual(result["active_download_peers"], 1)
        self.assertEqual(result["total_blocks_inflight"], 3)
        self.assertEqual(result["total_ask_queue"], 7)
        self.assertEqual(len(result["details"]), 2)

    def test_empty(self):
        result = aggregate_peers([])
        self.assertEqual(result["active_download_peers"], 0)
        self.assertEqual(result["total_blocks_inflight"], 0)
        self.assertEqual(result["total_ask_queue"], 0)
        self.assertEqual(result["details"], [])

    def test_missing_fields(self):
        peers = [{"addr": "1.2.3.4:8333"}]
        result = aggregate_peers(peers)
        self.assertEqual(result["total_blocks_inflight"], 0)
        self.assertEqual(result["total_ask_queue"], 0)


class TestIBDTracker(unittest.TestCase):
    def test_update_height(self):
        t = IBDTracker()
        self.assertIsNone(t.last_height)
        t.update(100)
        self.assertEqual(t.last_height, 100)
        self.assertEqual(t.session_start_height, 100)
        t.update(200)
        self.assertEqual(t.last_height, 200)
        self.assertEqual(t.session_start_height, 100)

    def test_update_none(self):
        t = IBDTracker()
        t.update(100)
        t.update(None)
        self.assertEqual(t.last_height, 100)

    def test_sync_state_synced(self):
        t = IBDTracker()
        t.update(100)
        self.assertEqual(t.sync_state(False, 0), "synced")

    def test_sync_state_rpc_unavailable(self):
        t = IBDTracker()
        t.update(100)
        self.assertEqual(t.sync_state(None, 3), "rpc_unavailable")

    def test_sync_state_healthy(self):
        t = IBDTracker()
        t.update(100)
        self.assertEqual(t.sync_state(True, 0), "healthy")

    def test_sync_state_slow(self):
        t = IBDTracker()
        t.update(100, now=time.time() - 90)
        self.assertEqual(t.sync_state(True, 0), "slow")

    def test_sync_state_stalled(self):
        t = IBDTracker()
        t.update(100, now=time.time() - 300)
        self.assertEqual(t.sync_state(True, 0), "stalled")

    def test_sync_state_unknown(self):
        t = IBDTracker()
        t.update(100)
        self.assertEqual(t.sync_state(None, 0), "unknown")

    def test_ema_rate(self):
        t = IBDTracker()
        self.assertIsNone(t.ema_rate)
        t.update_ema(10.0)
        self.assertAlmostEqual(t.ema_rate, 10.0)
        t.update_ema(20.0)
        self.assertGreater(t.ema_rate, 10.0)
        self.assertLess(t.ema_rate, 20.0)

    def test_ema_rate_none(self):
        t = IBDTracker()
        t.update_ema(None)
        self.assertIsNone(t.ema_rate)

    def test_peaks(self):
        t = IBDTracker()
        t.update_peaks(5, 10, 3)
        self.assertEqual(t.peaks["connections"], 5)
        self.assertEqual(t.peaks["blocks_inflight"], 10)
        self.assertEqual(t.peaks["ask_queue"], 3)
        t.update_peaks(3, 8, 5)
        self.assertEqual(t.peaks["connections"], 5)
        self.assertEqual(t.peaks["blocks_inflight"], 10)
        self.assertEqual(t.peaks["ask_queue"], 5)

    def test_height_delta_detection(self):
        t = IBDTracker()
        t.update(100)
        t.update(100)
        self.assertEqual(t.last_height, 100)
        t.update(200)
        self.assertEqual(t.last_height, 200)
        self.assertEqual(t.session_observed_height, 200)


class TestIBDSampler(unittest.TestCase):
    def test_record_and_rate(self):
        s = IBDSampler()
        now = time.time()
        s.samples.append({"time": now, "height": 1000, "connections": 5,
                          "bytes_received": 100, "bytes_sent": 50,
                          "ibd": True, "estimated_tip": 10000,
                          "blocks_inflight": 3, "ask_queue": 5,
                          "active_download_peers": 2})
        s.samples.append({"time": now + 60, "height": 1120, "connections": 5,
                          "bytes_received": 200, "bytes_sent": 100,
                          "ibd": True, "estimated_tip": 10000,
                          "blocks_inflight": 3, "ask_queue": 5,
                          "active_download_peers": 2})
        rate = s.current_rate()
        self.assertIsNotNone(rate)
        self.assertAlmostEqual(rate, 120.0 / 1.0, places=0)

    def test_bounded_history(self):
        s = IBDSampler(max_samples=3)
        for i in range(10):
            s.record(i, 5, 0, 0, True, 10000, 0, 0, 0)
        self.assertLessEqual(len(s.samples), 3)

    def test_record_same_height_merge(self):
        s = IBDSampler()
        now = time.time()
        s.record(100, 5, 100, 50, True, 10000, 3, 5, 2)
        s.record(100, 6, 200, 100, True, 10000, 4, 6, 3)
        self.assertEqual(len(s.samples), 1)
        self.assertEqual(s.samples[-1]["connections"], 6)

    def test_record_none_height_skipped(self):
        s = IBDSampler()
        s.record(None, 5, 0, 0, True, 10000, 0, 0, 0)
        self.assertEqual(len(s.samples), 0)

    def test_session_average(self):
        s = IBDSampler()
        now = time.time()
        s.samples.append({"time": now - 600, "height": 100, "connections": None,
                          "bytes_received": None, "bytes_sent": None,
                          "ibd": None, "estimated_tip": None,
                          "blocks_inflight": None, "ask_queue": None,
                          "active_download_peers": None})
        s.samples.append({"time": now, "height": 400, "connections": None,
                          "bytes_received": None, "bytes_sent": None,
                          "ibd": None, "estimated_tip": None,
                          "blocks_inflight": None, "ask_queue": None,
                          "active_download_peers": None})
        avg = s.session_average(100)
        self.assertIsNotNone(avg)
        self.assertAlmostEqual(avg, 300.0 / 10.0, places=1)

    def test_window_average(self):
        s = IBDSampler()
        base = time.time()
        for i in range(6):
            s.samples.append({"time": base + i * 10, "height": 100 + i * 10,
                              "connections": None, "bytes_received": None,
                              "bytes_sent": None, "ibd": None, "estimated_tip": None,
                              "blocks_inflight": None, "ask_queue": None,
                              "active_download_peers": None})
        avg = s.window_average(60)
        self.assertIsNotNone(avg)

    def test_to_history(self):
        s = IBDSampler()
        s.record(100, 5, 0, 0, True, 10000, 0, 0, 0)
        s.record(200, 5, 100, 50, True, 10000, 1, 2, 1)
        h = s.to_history()
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0]["height"], 100)
        self.assertEqual(h[1]["height"], 200)

    def test_negative_delta(self):
        s = IBDSampler()
        base = time.time()
        s.samples.append({"time": base, "height": 200, "connections": None,
                          "bytes_received": None, "bytes_sent": None,
                          "ibd": None, "estimated_tip": None,
                          "blocks_inflight": None, "ask_queue": None,
                          "active_download_peers": None})
        s.samples.append({"time": base + 10, "height": 100, "connections": None,
                          "bytes_received": None, "bytes_sent": None,
                          "ibd": None, "estimated_tip": None,
                          "blocks_inflight": None, "ask_queue": None,
                          "active_download_peers": None})
        rate = s.current_rate()
        self.assertIsNone(rate)

    def test_current_rate_no_samples(self):
        s = IBDSampler()
        self.assertIsNone(s.current_rate())

    def test_current_rate_one_sample(self):
        s = IBDSampler()
        s.samples.append({"time": time.time(), "height": 100, "connections": None,
                          "bytes_received": None, "bytes_sent": None,
                          "ibd": None, "estimated_tip": None,
                          "blocks_inflight": None, "ask_queue": None,
                          "active_download_peers": None})
        self.assertIsNone(s.current_rate())


class TestIBDTrackerSessionType(unittest.TestCase):
    def test_cold_ibd(self):
        t = IBDTracker()
        t.update(10)
        self.assertEqual(t.session_start_height, 10)

    def test_resume_ibd(self):
        t = IBDTracker()
        t.update(500000)
        self.assertEqual(t.session_start_height, 500000)

    def test_session_start_height_stable(self):
        t = IBDTracker()
        t.update(100)
        t.update(200)
        t.update(300)
        self.assertEqual(t.session_start_height, 100)


class TestIBDTrackerStaleDetection(unittest.TestCase):
    def test_healthy_just_advanced(self):
        t = IBDTracker()
        t.update(100, now=time.time())
        self.assertEqual(t.sync_state(True, 0), "healthy")

    def test_slow(self):
        t = IBDTracker()
        t.update(100, now=time.time() - 120)
        self.assertEqual(t.sync_state(True, 0), "slow")

    def test_stalled(self):
        t = IBDTracker()
        t.update(100, now=time.time() - 300)
        self.assertEqual(t.sync_state(True, 0), "stalled")


class TestComputeETAEdgeCases(unittest.TestCase):
    def test_large_remaining(self):
        eta = compute_eta(1.0, 1, 1000001)
        self.assertIsNotNone(eta)
        self.assertGreater(eta, 0)

    def test_negative_eta_not_possible(self):
        eta = compute_eta(10.0, 900000, 1000000)
        self.assertIsNotNone(eta)
        self.assertGreater(eta, 0)


class TestIBDTrackerLastAdvanceTime(unittest.TestCase):
    def test_fresh_tracker_returns_none(self):
        t = IBDTracker()
        self.assertIsNone(t.last_advance_time())

    def test_first_height_sample_returns_none(self):
        t = IBDTracker()
        t.update(100, now=1000.0)
        self.assertIsNone(t.last_advance_time())

    def test_same_height_returns_none(self):
        t = IBDTracker()
        t.update(100, now=1000.0)
        t.update(100, now=1010.0)
        self.assertIsNone(t.last_advance_time())

    def test_height_increase_records_advance(self):
        t = IBDTracker()
        t.update(100, now=1000.0)
        t.update(200, now=1010.0)
        self.assertEqual(t.last_advance_time(), 1010.0)

    def test_height_decrease_does_not_update_advance(self):
        t = IBDTracker()
        t.update(200, now=1000.0)
        t.update(300, now=1010.0)
        self.assertEqual(t.last_advance_time(), 1010.0)
        t.update(150, now=1020.0)
        self.assertEqual(t.last_advance_time(), 1010.0)

    def test_multiple_increases_tracks_last(self):
        t = IBDTracker()
        t.update(100, now=1000.0)
        t.update(200, now=1010.0)
        t.update(300, now=1020.0)
        self.assertEqual(t.last_advance_time(), 1020.0)

    def test_increase_after_decrease(self):
        t = IBDTracker()
        t.update(100, now=1000.0)
        t.update(200, now=1010.0)
        t.update(150, now=1020.0)
        t.update(250, now=1030.0)
        self.assertEqual(t.last_advance_time(), 1030.0)


def _make_config(tmp_debug_log=None):
    from server import Config
    return Config(
        host="127.0.0.1",
        port=8787,
        refresh=5,
        timeout=5,
        info_interval=60,
        peer_interval=120,
        max_backoff=30,
        log_tail_bytes=262144,
        innovad="innovad",
        datadir=None,
        conf=None,
        debug_log=tmp_debug_log,
        frontend=Path("/dev/null"),
    )


class TestBuildSnapshotNoCrash(unittest.TestCase):
    @patch("server.process_times", return_value=(None, None))
    @patch("server.process_pid", return_value=None)
    def test_build_snapshot_with_fresh_tracker(self, _pid, _times):
        config = _make_config()
        info = TimedValue()
        peers = TimedValue()
        blockchain = TimedValue()
        tracker = IBDTracker()
        sampler = IBDSampler()
        metrics = {"cpu_count": 1, "total_ram_bytes": 1024}

        snap = build_snapshot(config, info, peers, blockchain, tracker, sampler, metrics)
        self.assertIsInstance(snap, dict)
        self.assertIn("ibd", snap)
        self.assertIsNone(snap["ibd"]["last_height_advance_ago_seconds"])

    @patch("server.process_times", return_value=(None, None))
    @patch("server.process_pid", return_value=None)
    def test_build_snapshot_after_height_advance(self, _pid, _times):
        config = _make_config()
        info = TimedValue()
        info.success({"blocks": 100, "connections": 5})
        peers = TimedValue()
        peers.success([])
        blockchain = TimedValue()
        blockchain.success({"initialblockdownload": True})
        tracker = IBDTracker()
        sampler = IBDSampler()

        now = time.time()
        tracker.update(100, now=now - 60)
        tracker.update(200, now=now)

        snap = build_snapshot(config, info, peers, blockchain, tracker, sampler, metrics={})
        self.assertIsInstance(snap, dict)
        self.assertIn("ibd", snap)
        self.assertIsNotNone(snap["ibd"]["last_height_advance_ago_seconds"])
        self.assertGreaterEqual(snap["ibd"]["last_height_advance_ago_seconds"], 0)
        self.assertEqual(snap["chain"]["height"], 100)

    @patch("server.process_times", return_value=(None, None))
    @patch("server.process_pid", return_value=None)
    def test_build_snapshot_rpc_unavailable_is_not_http_failure(self, _pid, _times):
        config = _make_config()
        info = TimedValue()
        info.failure("RPC unavailable")
        peers = TimedValue()
        peers.failure("RPC unavailable")
        blockchain = TimedValue()
        blockchain.failure("RPC unavailable")
        tracker = IBDTracker()
        sampler = IBDSampler()

        snap = build_snapshot(config, info, peers, blockchain, tracker, sampler, metrics={})
        self.assertIsInstance(snap, dict)
        self.assertFalse(snap["node"]["online"])
        self.assertIn("ibd", snap)

    @patch("server.process_times", return_value=(None, None))
    @patch("server.process_pid", return_value=None)
    def test_build_snapshot_connections_and_traffic(self, _pid, _times):
        config = _make_config()
        info = TimedValue()
        info.success({"blocks": 500, "connections": 12, "datareceived": 1024, "datasent": 2048})
        peers = TimedValue()
        peers.success([
            {"inbound": True, "pingtime": 0.05},
            {"inbound": False, "pingtime": 0.10},
        ])
        blockchain = TimedValue()
        blockchain.success({"initialblockdownload": False})
        tracker = IBDTracker()
        sampler = IBDSampler()

        snap = build_snapshot(config, info, peers, blockchain, tracker, sampler, metrics={})
        net = snap["network"]
        self.assertEqual(net["connections"], 12)
        self.assertEqual(net["inbound"], 1)
        self.assertEqual(net["outbound"], 1)
        self.assertEqual(net["bytes_received"], 1024)
        self.assertEqual(net["bytes_sent"], 2048)
        self.assertIsNotNone(net["average_ping_ms"])

    @patch("server.process_times", return_value=(None, None))
    @patch("server.process_pid", return_value=None)
    def test_build_snapshot_history_populated(self, _pid, _times):
        config = _make_config()
        info = TimedValue()
        peers = TimedValue()
        blockchain = TimedValue()
        tracker = IBDTracker()
        sampler = IBDSampler()

        snap = build_snapshot(config, info, peers, blockchain, tracker, sampler, metrics={})
        self.assertIsInstance(snap["history"], list)
        self.assertIsInstance(snap["features"], dict)

    @patch("server.process_times", return_value=(None, None))
    @patch("server.process_pid", return_value=None)
    def test_height_decrease_reindex_does_not_panic(self, _pid, _times):
        config = _make_config()
        info = TimedValue()
        info.success({"blocks": 50, "connections": 3})
        peers = TimedValue()
        peers.success([])
        blockchain = TimedValue()
        blockchain.success({"initialblockdownload": True})
        tracker = IBDTracker()
        sampler = IBDSampler()

        now = time.time()
        tracker.update(200, now=now - 120)
        tracker.update(300, now=now - 60)
        tracker.update(100, now=now)

        snap = build_snapshot(config, info, peers, blockchain, tracker, sampler, metrics={})
        self.assertIsInstance(snap, dict)
        self.assertIn("ibd", snap)
        self.assertEqual(snap["ibd"]["session_type"], "cold_ibd")


class TestJSONEndpointPayload(unittest.TestCase):
    @patch("server.process_times", return_value=(None, None))
    @patch("server.process_pid", return_value=None)
    def test_full_snapshot_is_json_serializable(self, _pid, _times):
        config = _make_config()
        info = TimedValue()
        info.success({"blocks": 1000, "connections": 8, "version": "1.0", "datareceived": 4096, "datasent": 8192})
        peers = TimedValue()
        peers.success([{"inbound": True, "pingtime": 0.05}])
        blockchain = TimedValue()
        blockchain.success({"initialblockdownload": True, "verificationprogress": 0.99})
        tracker = IBDTracker()
        sampler = IBDSampler()

        now = time.time()
        tracker.update(900, now=now - 120)
        tracker.update(1000, now=now)

        snap = build_snapshot(config, info, peers, blockchain, tracker, sampler, metrics={"cpu_count": 2})
        payload = json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode()
        parsed = json.loads(payload)
        self.assertEqual(parsed["chain"]["height"], 1000)
        self.assertIn("last_height_advance_ago_seconds", parsed["ibd"])
        self.assertIsNotNone(parsed["network"]["connections"])
        self.assertIsNotNone(parsed["network"]["bytes_received"])
        self.assertIsNotNone(parsed["network"]["bytes_sent"])


if __name__ == "__main__":
    unittest.main()
