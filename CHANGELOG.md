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
