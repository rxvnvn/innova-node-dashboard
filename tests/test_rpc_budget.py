"""RPC budget contract: the dashboard must never poll ``getinfo`` by default.

Measured on the production node 31.77.247.41 (kernel 7.0.0-38, innad 5.0.5,
MemoryMax=1 GiB): one ``getinfo`` call read ~0.47 GB from tip-records.dat +
blk0003.dat and took ~1.4 s, because ``getinfo`` computes six wallet aggregates
(stake weight, balance, new mint, stake, unconfirmed, immature) and each of them
walks the whole wallet, resolving every tx through the block index. Polling it
every 60 s evicted the page cache the message handler needed and stalled P2P.

These tests pin the replacement: cheap, wallet-free sources only.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import server  # noqa: E402
from server import Collector, TimedValue, build_snapshot  # noqa: E402


def _config(**overrides):
    kwargs = dict(
        host="127.0.0.1", port=8799, refresh=5, timeout=8,
        info_interval=60, peer_interval=120, max_backoff=900,
        log_tail_bytes=4096, innovad="/bin/true", datadir=None, conf=None,
        debug_log=None, frontend=Path("."),
    )
    kwargs.update(overrides)
    return server.Config(**kwargs)


def _snapshot(info=None, netinfo=None, nettotals=None, blockchain=None, build_info_interval=0):
    info_state = TimedValue()
    info_state.success(info or {})
    netinfo_state = TimedValue()
    if netinfo is not None:
        netinfo_state.success(netinfo)
    nettotals_state = TimedValue()
    if nettotals is not None:
        nettotals_state.success(nettotals)
    blockchain_state = TimedValue()
    blockchain_state.success(blockchain or {})
    peers_state = TimedValue()
    peers_state.success([])
    height_state = TimedValue()
    height_state.success(1000)
    return build_snapshot(
        _config(build_info_interval=build_info_interval), info_state, peers_state,
        blockchain_state, server.IBDTracker(), server.IBDSampler(), metrics={},
        daemon=server.DaemonState(state=server.DAEMON_RUNNING),
        height_state=height_state,
        netinfo_state=netinfo_state, nettotals_state=nettotals_state,
    )


class TestPolledMethods(unittest.TestCase):
    def test_default_config_never_polls_getinfo(self):
        col = Collector(_config())
        methods = {source.method for source in col.sources.values()}
        self.assertNotIn("getinfo", methods)
        self.assertNotIn("info", col.sources)

    def test_default_config_polls_only_cheap_sources(self):
        col = Collector(_config())
        self.assertEqual(
            {source.method for source in col.sources.values()},
            {"getblockcount", "getnetworkinfo", "getnettotals", "getpeerinfo", "getblockchaininfo"},
        )

    def test_getinfo_is_opt_in_and_slow(self):
        col = Collector(_config(build_info_interval=1800))
        self.assertIn("info", col.sources)
        self.assertEqual(col.sources["info"].interval, 1800)

    def test_disabled_source_reports_no_age_and_no_failure(self):
        col = Collector(_config())
        self.assertIsNone(col.info.age_seconds())
        self.assertEqual(col.info.failures, 0)


class TestFieldMapping(unittest.TestCase):
    def test_identity_and_connections_come_from_getnetworkinfo(self):
        snap = _snapshot(netinfo={"version": 5000500, "protocolversion": 70016, "connections": 9})
        self.assertEqual(snap["node"]["version"], "v5.0.5")
        self.assertEqual(snap["node"]["protocol_version"], 70016)
        self.assertEqual(snap["network"]["connections"], 9)

    def test_traffic_comes_from_getnettotals(self):
        snap = _snapshot(nettotals={"totalbytesrecv": 1234567, "totalbytessent": 7654321})
        self.assertEqual(snap["network"]["bytes_received"], 1234567)
        self.assertEqual(snap["network"]["bytes_sent"], 7654321)
        self.assertEqual(snap["network"]["traffic"]["source"], "getnettotals")

    def test_chain_fields_come_from_getblockchaininfo(self):
        snap = _snapshot(blockchain={
            "blocks": 8195257, "initialblockdownload": False,
            "difficulty": {"proof-of-work": 1.0, "proof-of-stake": 42.5},
            "moneysupply": 21000000.0,
        })
        self.assertIs(snap["chain"]["initial_block_download"], False)
        self.assertEqual(snap["chain"]["difficulty"], 42.5)
        self.assertEqual(snap["chain"]["money_supply"], 21000000.0)

    def test_build_identity_is_absent_unless_getinfo_is_enabled(self):
        snap = _snapshot(netinfo={"version": 5000500})
        self.assertIsNone(snap["node"]["build_commit"])
        self.assertIsNone(snap["node"]["build_dirty"])
        self.assertFalse(snap["freshness"]["getinfo_enabled"])

    def test_build_identity_is_used_when_getinfo_is_enabled(self):
        snap = _snapshot(info={"buildcommit": "8fdb7c93", "builddirty": True}, build_info_interval=1800)
        self.assertEqual(snap["node"]["build_commit"], "8fdb7c93")
        self.assertIs(snap["node"]["build_dirty"], True)
        self.assertTrue(snap["freshness"]["getinfo_enabled"])

    def test_freshness_reports_the_cheap_sources(self):
        snap = _snapshot(netinfo={"version": 1}, nettotals={"totalbytesrecv": 1})
        self.assertIsNotNone(snap["freshness"]["getnetworkinfo_age_seconds"])
        self.assertIsNotNone(snap["freshness"]["getnettotals_age_seconds"])


class TestNoRegressionInPipeline(unittest.TestCase):
    def test_collector_polls_no_getinfo_through_a_full_collect(self):
        col = Collector(_config())
        calls = []

        def rpc(method, timeout=None):
            calls.append(method)
            if method == "getblockcount":
                return 8195257, None
            if method == "getnetworkinfo":
                return {"version": 5000500, "protocolversion": 70016, "connections": 6}, None
            if method == "getnettotals":
                return {"totalbytesrecv": 10, "totalbytessent": 20}, None
            if method == "getblockchaininfo":
                return {"blocks": 8195257, "initialblockdownload": False}, None
            if method == "getpeerinfo":
                return [], None
            raise AssertionError(f"unexpected RPC method {method}")

        with patch.object(col, "rpc_json", side_effect=rpc), \
             patch("server.process_pid", return_value=999):
            snap = col.collect()
        self.assertNotIn("getinfo", calls)
        self.assertEqual(snap["chain"]["height"], 8195257)
        self.assertEqual(snap["ibd"]["sync_phase"], "synced")


if __name__ == "__main__":
    unittest.main()
