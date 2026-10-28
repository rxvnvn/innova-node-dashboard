## 0.4.3 — 2026-10-28

### Fixed — the dashboard no longer triggers a ~0.47 GB read burst every minute

`getinfo` on this chain computes six wallet aggregates (stake weight, balance,
new mint, stake, unconfirmed, immature). Each one walks the whole wallet and
resolves every transaction through the block index, which on the authoritative
V2 path is one `fseek` + `fread` per lookup on `tip-records.dat` /
`tip-derived.dat` plus block reads from `blkNNNN.dat`.

Measured on 31.77.247.41 (kernel 7.0.0-38, 1 GiB cgroup): one `getinfo` call =
**~935 MB read over two calls, ~1.4 s each**, attributed with a bounded
`uprobe`+`vfs_read` probe. Polled every 60 s it evicted the page cache that
`msghand` needed and made P2P receive/handshake wait on block I/O.

- The dashboard now polls only wallet-free sources: `getblockcount` (height),
  `getnetworkinfo` (version/protocol/connections), `getnettotals` (traffic),
  `getblockchaininfo` (blocks/IBD/difficulty/money supply), `getpeerinfo`.
- `getinfo` is **off by default**. It is kept behind `--build-info-interval`
  (env `INNOVA_DASHBOARD_BUILD_INFO_INTERVAL`) purely for `buildcommit` /
  `builddirty`, which no cheap RPC exposes.
- `chain.difficulty` is now the proof-of-stake difficulty from
  `getblockchaininfo` (the dashboard has always shown a single number).
- `freshness` adds `getnetworkinfo_*` / `getnettotals_*` ages and failures plus
  `getinfo_enabled`.
- RPC health is computed from the sources that are actually polled, so a
  disabled `getinfo` can no longer mask a dead RPC.
- API schema 7 → 8 (additive).

### Tests

166 tests (was 155). New `tests/test_rpc_budget.py` pins the contract: the
default RPC method set contains no `getinfo`, fields map from the cheap
sources, and a full `collect()` cycle issues no `getinfo` call.

# Changelog

All notable changes to Innova Node Dashboard. This file records releases from
0.4.1 onwards; earlier release notes are the `## Version …` sections of
`README.md`.

## [0.4.2] - 2026-10-28

### Changed
- **Release structure only** — the deploy/rollback tooling that landed in
  `8015194` (after the `v0.4.1` tag) is now part of a tagged release, so
  `v0.4.2` is a single ref that contains both the sync-state fix and the scripts
  used to ship it. No runtime behaviour change: `backend/server.py` differs from
  0.4.1 only in `APP_VERSION`.

### Notes
- `v0.4.1` is left untouched (no history rewrite, no force push).
- Deploying 0.4.2 is optional: the running 0.4.1 service already serves the
  fixed logic; the only functional artefact this release adds is tooling.

## [0.4.1] - 2026-10-28

### Fixed
- **False "Synchronization complete"**: the dashboard rendered that verdict
  whenever a cached `initialblockdownload=false` was present, even when the RPC
  that produced it was minutes old and failing. The value is now trusted only
  while `chain.state_fresh` is true (limit `max(120, 2 × info_interval)`).
- **Lag claimed from one peer**: the pipeline treated any single peer height as
  the network tip. A lag verdict now requires a quorum (`chain.confirmed_tip`,
  at least two peers within one block); the outlier is reported separately as
  `chain.unconfirmed_peer_tip`.

### Added
- `ibd.sync_phase` — canonical four-state verdict (`synced`, `syncing`,
  `degraded`, `rpc_unavailable`, plus `unknown`).
- `ibd.sync_reason` — machine-readable cause of the verdict.
- `ibd.sync_basis` — the independent freshness axes (chain RPC age/limit/
  failures, peer snapshot age/freshness, height source/age/staleness, confirmed
  lag).
- `chain.state_updated_at`, `chain.state_age_seconds`, `chain.state_fresh`,
  `chain.state_max_age_seconds`, `chain.confirmed_tip`,
  `chain.confirmed_tip_support`, `chain.peer_announcements`,
  `chain.unconfirmed_peer_tip`, `chain.behind_confirmed_blocks`.
- Notices for degraded chain state, unavailable RPC and confirmed lag.
- Frontend: phase-aware headline and chip, chain age and last trusted update in
  the freshness line, sync panel kept visible while degraded.
- Tests: `tests/test_sync_verdict.py` (25 cases, covering the eight acceptance
  scenarios) and frontend cases 11–15.

### Changed
- API schema 6 → 7 (additive only).
- `ibd.session_type` becomes `synced` only when the phase is `synced`.
- `sync_state` emits `degraded` for stale chain data instead of `synced`.

### Compatibility
- No existing field was removed or renamed; consumers that only read
  `chain.initial_block_download` keep working, and the frontend still renders
  payloads that predate `sync_phase`.
