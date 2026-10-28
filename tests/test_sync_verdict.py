#!/usr/bin/env python3
"""Sync-verdict tests: stale chain state must never be reported as "synced".

Covers the eight acceptance scenarios for the 0.4.1 fix:
  1. fresh RPC + IBD=false   -> SYNCED
  2. fresh RPC + IBD=true    -> SYNCING
  3. stale RPC + cached false-> NOT synced (DEGRADED)
  4. RPC timeout             -> DEGRADED / RPC_UNAVAILABLE
  5. fresh peers + stale chain -> states are kept separate
  6. confirmed peer heights  -> lag is judged on quorum, not on one peer
  7. RPC recovery            -> status recovers automatically
  8. API compatibility       -> existing keys and legacy labels survive
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from server import (  # noqa: E402
    CONFIRMED_TIP_MIN_SUPPORT,
    DAEMON_RUNNING,
    Config,
    DaemonState,
    IBDSampler,
    IBDTracker,
    TimedValue,
    build_snapshot,
    chain_state_max_age,
    confirmed_tip_from_peers,
    resolve_sync_phase,
)

INFO_INTERVAL = 60
MAX_AGE = chain_state_max_age(INFO_INTERVAL)


def _config() -> Config:
    return Config(
        host="127.0.0.1", port=8787, refresh=5, timeout=5,
        info_interval=INFO_INTERVAL, peer_interval=120, max_backoff=30,
        log_tail_bytes=262144, innovad="innovad", datadir=None, conf=None,
        debug_log=None, frontend=Path("/dev/null"),
    )


def _running() -> DaemonState:
    return DaemonState(state=DAEMON_RUNNING, pid=4242)


def _age(state: TimedValue, seconds: int) -> TimedValue:
    """Backdate a cached value so its age_seconds() is ``seconds``."""
    state.updated_wall = dt.datetime.now().astimezone() - dt.timedelta(seconds=seconds)
    return state


def _peers(heights: list[int]) -> list[dict]:
    return [
        {"addr": f"10.0.0.{i}:14530", "inbound": False, "bestknownheight": h,
         "blocksinflight": 0, "askqueuesize": 0, "bytes_received": 1, "bytes_sent": 1}
        for i, h in enumerate(heights)
    ]


def _snapshot(ibd, *, chain_age=1, chain_failures=0, heights=None, height=1000):
    """Build a snapshot with explicit freshness for chain + peers."""
    info = TimedValue()
    info.success({"blocks": height, "connections": 5, "version": 5050500,
                  "initialblockdownload": ibd})
    peers = TimedValue()
    peers.success(_peers(heights if heights is not None else [height] * 3))
    chain = TimedValue()
    chain.success({"initialblockdownload": ibd})
    chain.failures = chain_failures
    _age(chain, chain_age)
    with patch("server.process_pid", return_value=4242), \
         patch("server.process_times", return_value=(None, 60)):
        return build_snapshot(_config(), info, peers, chain, IBDTracker(),
                              IBDSampler(), metrics={}, daemon=_running())


class TestSyncPhaseUnits(unittest.TestCase):
    """The pure decision function, without the snapshot plumbing."""

    def test_fresh_false_is_synced(self):
        self.assertEqual(resolve_sync_phase(ibd_rpc=False, chain_fresh=True), "synced")

    def test_stale_false_is_not_synced(self):
        self.assertEqual(resolve_sync_phase(ibd_rpc=False, chain_fresh=False), "degraded")

    def test_fresh_true_is_syncing(self):
        self.assertEqual(resolve_sync_phase(ibd_rpc=True, chain_fresh=True), "syncing")

    def test_missing_with_failures_is_rpc_unavailable(self):
        self.assertEqual(
            resolve_sync_phase(ibd_rpc=None, chain_fresh=False, chain_failures=2),
            "rpc_unavailable")

    def test_missing_without_failures_is_unknown(self):
        self.assertEqual(resolve_sync_phase(ibd_rpc=None, chain_fresh=False), "unknown")

    def test_stopped_daemon_is_unknown(self):
        self.assertEqual(resolve_sync_phase(ibd_rpc=False, chain_fresh=True, running=False),
                         "unknown")

    def test_confirmed_lag_switches_to_syncing(self):
        self.assertEqual(
            resolve_sync_phase(ibd_rpc=False, chain_fresh=True, behind_confirmed=1),
            "synced")
        self.assertEqual(
            resolve_sync_phase(ibd_rpc=False, chain_fresh=True, behind_confirmed=2),
            "syncing")

    def test_require_height_degrades_when_height_missing(self):
        self.assertEqual(
            resolve_sync_phase(ibd_rpc=False, chain_fresh=True, height=None,
                               require_height=True),
            "degraded")


class TestConfirmedTip(unittest.TestCase):
    def test_single_outlier_never_confirms(self):
        info = confirmed_tip_from_peers(_peers([1000] * 9 + [1067]))
        self.assertEqual(info["confirmed_tip"], 1000)
        # Support counts peers announcing the confirmed tip *or higher within
        # tolerance*, so the lone outlier still counts towards 1000's support.
        self.assertEqual(info["support"], 10)
        self.assertEqual(info["max_peer_tip"], 1067)
        self.assertEqual(info["announcements"], 10)

    def test_quorum_confirms_higher_tip(self):
        info = confirmed_tip_from_peers(_peers([1067] * 5 + [1000] * 5))
        self.assertEqual(info["confirmed_tip"], 1067)
        self.assertEqual(info["support"], 5)

    def test_tolerance_accepts_near_heights(self):
        info = confirmed_tip_from_peers(_peers([1067, 1066, 900]))
        self.assertEqual(info["confirmed_tip"], 1067)
        self.assertEqual(info["support"], 2)

    def test_no_announcements(self):
        self.assertEqual(confirmed_tip_from_peers([]),
                         {"confirmed_tip": None, "support": 0,
                          "max_peer_tip": None, "announcements": 0})
        self.assertEqual(confirmed_tip_from_peers(None)["confirmed_tip"], None)

    def test_min_support_constant_is_at_least_two(self):
        self.assertGreaterEqual(CONFIRMED_TIP_MIN_SUPPORT, 2)


class TestSnapshotScenarios(unittest.TestCase):
    """The eight acceptance scenarios, exercised through build_snapshot()."""

    def test_1_fresh_rpc_ibd_false_is_synced(self):
        snap = _snapshot(False, chain_age=5, heights=[1000, 1000, 1000], height=1000)
        self.assertEqual(snap["ibd"]["sync_phase"], "synced")
        self.assertEqual(snap["ibd"]["sync_reason"], "fresh_chain_state_at_confirmed_tip")
        self.assertEqual(snap["ibd"]["session_type"], "synced")
        self.assertTrue(snap["chain"]["state_fresh"])
        self.assertEqual(snap["chain"]["initial_block_download"], False)

    def test_2_fresh_rpc_ibd_true_is_syncing(self):
        snap = _snapshot(True, chain_age=5, heights=[1000, 1000, 1000], height=1000)
        self.assertEqual(snap["ibd"]["sync_phase"], "syncing")
        self.assertEqual(snap["ibd"]["sync_reason"], "ibd_in_progress")
        self.assertNotEqual(snap["ibd"]["session_type"], "synced")

    def test_3_stale_rpc_with_cached_false_is_not_synced(self):
        snap = _snapshot(False, chain_age=3280, chain_failures=6,
                         heights=[1000, 1000, 1000], height=1000)
        self.assertNotEqual(snap["ibd"]["sync_phase"], "synced")
        self.assertEqual(snap["ibd"]["sync_phase"], "degraded")
        self.assertNotEqual(snap["ibd"]["sync_state"], "synced")
        self.assertNotEqual(snap["ibd"]["session_type"], "synced")
        self.assertFalse(snap["chain"]["state_fresh"])
        self.assertEqual(snap["chain"]["state_age_seconds"], 3280)
        self.assertTrue(any("not asserted" in n for n in snap["notices"]))

    def test_4_rpc_timeout_is_rpc_unavailable(self):
        info = TimedValue()
        info.success({"blocks": 1000, "connections": 5})
        peers = TimedValue()
        peers.success(_peers([1000, 1000]))
        chain = TimedValue()          # never fetched
        chain.failures = 4            # and failing
        with patch("server.process_pid", return_value=4242), \
             patch("server.process_times", return_value=(None, 60)):
            snap = build_snapshot(_config(), info, peers, chain, IBDTracker(),
                                  IBDSampler(), metrics={}, daemon=_running())
        self.assertIsNone(snap["chain"]["initial_block_download"])
        self.assertEqual(snap["ibd"]["sync_phase"], "rpc_unavailable")
        self.assertIsNone(snap["chain"]["state_age_seconds"])
        self.assertFalse(snap["chain"]["state_fresh"])

    def test_5_fresh_peers_with_stale_chain_are_separate(self):
        snap = _snapshot(False, chain_age=4000, chain_failures=3,
                         heights=[1000, 1000, 1000], height=1000)
        basis = snap["ibd"]["sync_basis"]
        self.assertTrue(basis["peers_fresh"])
        self.assertFalse(basis["chain_rpc_fresh"])
        self.assertEqual(snap["peers"]["stale"], False)
        self.assertTrue(snap["peers"]["available"])
        self.assertEqual(snap["ibd"]["sync_phase"], "degraded")
        # The peer panel may be live while the chain verdict is degraded: they
        # are independent axes and must not be merged into one "fresh" signal.
        self.assertEqual(snap["chain"]["state_age_seconds"], 4000)

    def test_6_single_unconfirmed_peer_does_not_create_lag(self):
        heights = [1000] * 9 + [1067]
        snap = _snapshot(False, chain_age=5, heights=heights, height=1000)
        self.assertEqual(snap["chain"]["confirmed_tip"], 1000)
        self.assertEqual(snap["chain"]["unconfirmed_peer_tip"], 1067)
        self.assertEqual(snap["chain"]["behind_confirmed_blocks"], 0)
        self.assertEqual(snap["ibd"]["sync_phase"], "synced")

    def test_6b_confirmed_quorum_creates_lag(self):
        heights = [1067] * 5 + [1000] * 5
        snap = _snapshot(False, chain_age=5, heights=heights, height=1000)
        self.assertEqual(snap["chain"]["confirmed_tip"], 1067)
        self.assertEqual(snap["chain"]["behind_confirmed_blocks"], 67)
        self.assertEqual(snap["ibd"]["sync_phase"], "syncing")
        self.assertEqual(snap["ibd"]["sync_reason"], "behind_confirmed_tip")
        self.assertTrue(any("Behind" in n for n in snap["notices"]))

    def test_7_recovery_restores_synced(self):
        chain = TimedValue()
        chain.success({"initialblockdownload": False})
        info = TimedValue()
        info.success({"blocks": 1000, "connections": 5})
        peers = TimedValue()
        peers.success(_peers([1000, 1000, 1000]))
        with patch("server.process_pid", return_value=4242), \
             patch("server.process_times", return_value=(None, 60)):
            _age(chain, 3600)
            degraded = build_snapshot(_config(), info, peers, chain, IBDTracker(),
                                      IBDSampler(), metrics={}, daemon=_running())
            chain.success({"initialblockdownload": False})   # a fresh poll lands
            recovered = build_snapshot(_config(), info, peers, chain, IBDTracker(),
                                       IBDSampler(), metrics={}, daemon=_running())
        self.assertEqual(degraded["ibd"]["sync_phase"], "degraded")
        self.assertEqual(recovered["ibd"]["sync_phase"], "synced")
        self.assertTrue(recovered["chain"]["state_fresh"])
        self.assertLess(recovered["chain"]["state_age_seconds"], 5)

    def test_8_api_compatibility(self):
        snap = _snapshot(False, chain_age=5, heights=[1000, 1000], height=1000)
        # Pre-existing top-level keys must survive.
        for key in ("api", "dashboard", "generated_at", "node", "chain", "network",
                    "peers", "ibd", "host", "history", "freshness", "features",
                    "notices", "errors"):
            self.assertIn(key, snap)
        self.assertEqual(snap["api"]["version"], "v1")
        self.assertEqual(snap["api"]["schema"], 7)
        self.assertEqual(snap["dashboard"]["version"], "0.4.2")
        for key in ("height", "height_source", "initial_block_download",
                    "verification_progress"):
            self.assertIn(key, snap["chain"])
        for key in ("session_type", "sync_state", "eta_seconds",
                    "last_height_advance_ago_seconds", "peer_aggregation"):
            self.assertIn(key, snap["ibd"])
        for key in ("getinfo_age_seconds", "getpeerinfo_age_seconds",
                    "getblockchaininfo_age_seconds", "getblockchaininfo_failures"):
            self.assertIn(key, snap["freshness"])
        # New fields are additive.
        for key in ("state_fresh", "state_updated_at", "confirmed_tip",
                    "behind_confirmed_blocks"):
            self.assertIn(key, snap["chain"])
        for key in ("sync_phase", "sync_reason", "sync_basis"):
            self.assertIn(key, snap["ibd"])

    def test_stopped_daemon_is_unknown_not_synced(self):
        info = TimedValue()
        info.success({"blocks": 1000})
        peers = TimedValue()
        peers.success(_peers([1000]))
        chain = TimedValue()
        chain.success({"initialblockdownload": False})
        with patch("server.process_pid", return_value=None), \
             patch("server.process_times", return_value=(None, None)):
            snap = build_snapshot(_config(), info, peers, chain, IBDTracker(),
                                  IBDSampler(), metrics={},
                                  daemon=DaemonState(state="stopped", pid=None))
        self.assertNotEqual(snap["ibd"]["sync_phase"], "synced")


class TestLegacySyncStateContract(unittest.TestCase):
    """The historic sync_state() signature keeps its documented behaviour."""

    def test_legacy_calls_unchanged(self):
        tracker = IBDTracker()
        self.assertEqual(tracker.sync_state(False, 0), "synced")
        self.assertEqual(tracker.sync_state(None, 3), "rpc_unavailable")
        self.assertEqual(tracker.sync_state(None, 0), "unknown")
        self.assertIn(tracker.sync_state(True, 0), {"healthy", "slow", "stalled"})

    def test_stale_false_degrades_through_the_legacy_entry_point(self):
        tracker = IBDTracker()
        self.assertEqual(
            tracker.sync_state(False, 0, chain_age_seconds=3280,
                               max_chain_age_seconds=MAX_AGE),
            "degraded")


if __name__ == "__main__":
    unittest.main(verbosity=2)
