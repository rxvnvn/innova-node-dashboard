# Innova Node Dashboard

Lightweight read-only dashboard for `innovad`, with frontend and backend deliberately separated by a stable API:

```text
Browser frontend -> GET /api/v1/status -> Python adapter today / innovad later
```

## Features

Current height, start time, uptime, IBD, connections, inbound/outbound peers, average ping, traffic, version, build commit, mobile layout, JSON API. No third-party Python packages.

### IBD Benchmark (v0.3.0)

Full monitoring panel for Initial Block Download:

- **Progress** — current height / estimated network height with percentage and progress bar
- **Sync state** — Healthy / Slow / Stalled / Synced with color coding
- **Speed** — current blocks/min, 5-minute sliding average, session average
- **ETA** — estimated time remaining using exponential moving average (α=0.15)
- **Network peers** — active download peers, blocks in flight, queued requests
- **Peer table** — expandable per-peer details (address, start height, bytes, ping)
- **Height graph** — compact canvas chart of recent height history
- **Host metrics** — CPU cores, total RAM (no PID, no sensitive data)

#### Cold IBD vs Resume IBD

- **Cold IBD** — session started from height < 1000 (near genesis). Dashboard can confirm this because it observed the start.
- **Resume IBD** — session started from higher height (node restarted mid-sync). Dashboard shows the observed start height.
- **Synced** — IBD complete (`initialblockdownload=false`).

Dashboard never claims Cold IBD unless it observed the start from near-zero.

#### Estimated Network Height

Computed as the 75th percentile of peer `startingheight` values, with filtering:
- Zeros excluded
- Values > 2.5× current height excluded (anomaly protection)
- Values < 0.5× current height excluded (lagging peers)

#### Speed & ETA

- **Current rate** — blocks/min from last two height samples
- **5-minute average** — sliding window over 300 seconds
- **Session average** — from observed session start to now
- **ETA** — `remaining_blocks / ema_rate × 60`. Uses EMA (α=0.15) for smooth estimation. Shows "Calculating…" when insufficient data. Null when rate is zero.

#### History

Dashboard stores a bounded ring buffer of height/traffic samples in memory (max 360 samples, ~1 hour). No database. After restart, a new observed session begins — not a continuation of a previous cold IBD benchmark.

## Test run

```bash
python3 backend/server.py --innovad /home/user/innova/src/innovad --datadir /home/user/.innova --host 0.0.0.0 --port 8787
```

Open `http://VPS_IP:8787`.

```bash
curl http://127.0.0.1:8787/api/v1/status
curl http://127.0.0.1:8787/health
```

## Running a public benchmark

1. Start with a clean datadir (no existing chain data)
2. Launch dashboard pointed at the new datadir
3. The IBD panel will show progress from the start
4. When IBD completes, the panel shows "Synced"
5. Session statistics (elapsed, avg rate, peak connections) are available in the API response

## Tests

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
node tests/test_frontend_render.js
```

Backend tests cover: blocks/min rate calculation, zero growth, negative delta after restart, 5-minute sliding average, session average, ETA, zero-rate ETA suppression, peer aggregation, estimated tip with outliers, sync state classification, bounded sample history, restart session detection, the peer snapshot lifecycle (unavailable / fresh / cached / zero-peer), and the daemon lifecycle (stop confirmation, cache invalidation, last-known height). The Node script checks the rendered UI states for unknown, zero, fresh and stale peer data, the offline badge and last-known height, and the release version link.

## Install

```bash
sudo ./scripts/install.sh
```

Custom paths:

```bash
sudo INNOVAD_PATH=/home/user/innova/src/innovad INNOVA_DATADIR=/home/user/.innova ./scripts/install.sh
```

Config: `/etc/innova-node-dashboard/config.env`

```bash
systemctl status innova-node-dashboard
journalctl -u innova-node-dashboard -f
```

## Update

From a refreshed Git checkout:

```bash
sudo ./scripts/update.sh
```

## Security

The dashboard is read-only and does not expose arbitrary RPC execution or credentials. Public access still reveals height, uptime, version and connection count. Restrict port 8787 with firewall/VPN when private monitoring is preferred.

## Future innovad integration

Serve the same static frontend and implement `GET /api/v1/status` in the daemon according to `docs/API.md`. The frontend contains no Python-specific code.

## Version 0.4.1

Truthful sync state — the dashboard no longer reports "Synchronization complete"
on the strength of a stale RPC value:

- **Cached `IBD=false` is not proof** — `initialblockdownload=false` is accepted
  only while the chain RPC that produced it is fresh (`chain.state_fresh`,
  limit `max(120, 2 × info_interval)`). Past that age the status degrades
  instead of claiming synchronisation.
- **Four explicit states** — `ibd.sync_phase` is one of `synced`, `syncing`,
  `degraded`, `rpc_unavailable` (plus `unknown` before any chain state exists),
  with a machine-readable `ibd.sync_reason`.
- **Lag needs a quorum** — the "behind" verdict uses `chain.confirmed_tip`, the
  highest peer height announced by at least two peers (within one block). A
  single peer announcing a height far above everybody else is exposed as
  `chain.unconfirmed_peer_tip` and can never mark the node as behind on its own.
- **Independent freshness axes** — the peer panel, the chain RPC and the height
  probe age separately (`ibd.sync_basis`); a live peer panel never upgrades a
  stale chain verdict, and the footer now shows the chain age and the last
  trusted chain update.
- **Recovery is automatic** — the verdict returns to `synced` on the first fresh
  poll; no restart or manual action involved.
- **Compatibility** — the schema is bumped to 7 additively: every pre-existing
  field is unchanged, `ibd.sync_state` keeps its legacy values and now emits
  `degraded` where it previously claimed `synced` on stale data.
- See `CHANGELOG.md` for the release notes and `docs/API.md` for the field docs.

## Version 0.4.0

Current-height visibility per node:

- **Peer "Current height" column** — the peer table now shows each node's live
  chain height (`best_known_height`, the peer-reported tip) instead of the static
  "Start height". Starting height is no longer shown on screen; it is retained in
  the API payload for internal statistics. Unknown peer heights render `N/A`.
- **Hero label** — the primary metric is labelled "Current Height" and always
  comes from the node's canonical chain height (RPC first; when RPC is briefly
  unavailable the cached last-known value and its source are shown).

## Version 0.3.3

Daemon lifecycle correctness — the dashboard now distinguishes a live node from
historical monitoring data:

- **Confirmed lifecycle state** — `node.online` is tri-state: `true` (running),
  `false` (confirmed stopped), `null` (unknown / not yet observed).
- **2-miss shutdown confirmation** — a single PID-detection miss is a transient
  lookup failure; two consecutive misses while the node was running confirm
  shutdown. Temporary RPC failures never change lifecycle state and keep the
  last peer snapshot (marked `cached`).
- **Stopped daemon is not live** — cached height no longer keeps a stopped node
  "online". While stopped, live height, connections, peer-derived totals and
  traffic rates are suppressed.
- **Last known height** — `chain.last_known_height` preserves the last observed
  live height as historical information ("Last known height: 6 812 442"), shown
  only when offline and never affecting `node.online`.
- **Clean restart** — when the daemon restarts (PID change), daemon-specific
  caches and traffic samples are invalidated so old data is never presented as
  current; live state repopulates from the next polls.
- **Version release link** — the footer version links to the matching GitHub
  Release (`…/releases/tag/v<version>`), built dynamically from the API
  version, opened in a new tab.
- **Offline badge** — a confirmed stopped daemon shows "Offline"; unknown shows
  "Checking".
- API schema bumped to 6 for the new `chain.last_known_height` field and
  tri-state `node.online`.

## Version 0.3.2

Peer snapshot correctness — the dashboard no longer conflates "peer data not
yet available" with "the node has zero peers":

- **Explicit peer state** — the API now exposes a top-level `peers` object with
  `available` / `fresh` / `stale`, `count`, `items`, age and last-attempt/error
  metadata (schema 5).
- **Zero vs unavailable** — a successful `getpeerinfo` returning `[]` is the
  only state that reports a true zero. Until the first successful call, `count`
  and peer-derived metrics are `null`, never `0`.
- **Cached data survives RPC failures** — a timed-out or failed refresh keeps
  the last successful peer snapshot and marks it `stale`, with a visible
  `cached` indicator and age in the UI.
- **Peer-derived metrics** — inbound, outbound, average ping, active download
  peers, blocks in flight and queued requests render as `—` (unknown) instead
  of a false `0`. Connections still come from `getinfo` and remain available.
- **Startup behavior** — `getpeerinfo` is already attempted immediately on the
  first status poll (the scheduler's first tick), then returns to the
  120-second steady-state cadence with exponential backoff; no extra RPC
  pressure is added.
- **Daemon restart** — when `innovad` restarts (PID change), the peer cache is
  invalidated so connections from the previous daemon instance are not reported
  as authoritative.

## Version 0.3.1

UX polish iteration — same architecture, same RPC load, more information per pixel:

- **Compact header** — brand and online badge on one line; header is ~50% shorter
- **Compact hero** — Current height remains the dominant element; right-side sync/source status moved to a single row
- **Peer Version column** — shows each peer's user agent (`/innova:5.0.1/`) or protocol version
- **Peer start height** — populated from the peer-reported chain height when the daemon does not report a start height
- **Traffic counters restored** — `datareceived`/`datasent` are returned as human strings (`"537.48 MB"`) by `getinfo`; they are now parsed to bytes. Per-peer sent bytes use the daemon's `bytessend` field (the RPC exposes a typo, not `bytessent`)
- **Live traffic speed** — `↓ RX speed` / `↑ TX speed` in the Network & Peers panel, derived from the existing traffic samples (no extra RPC polling)
- **Ping color coding** — green (< 100 ms), yellow (100–300 ms), red (> 300 ms) on both the average ping and per-peer ping values
- **Height history** — tighter graph spacing, and the section is prepared for selectable time ranges (toolbar scaffold, currently hidden)
- API schema bumped to 4 with the new `network.traffic` object and peer `version`/`best_known_height` fields

## Version 0.3.0

- IBD Benchmark monitoring panel with progress, speed, ETA, sync state
- Peer aggregation: active download peers, blocks in flight, queued requests
- Expandable peer detail table
- Compact height history canvas graph
- Estimated network height from peer starting heights (75th percentile, filtered)
- Sync state classification: Healthy / Slow / Stalled / Synced
- Session type detection: Cold IBD / Resume IBD
- Host metrics (CPU, RAM) without sensitive data
- `getblockchaininfo` RPC call for reliable IBD state and verification progress
- Bounded in-memory sample history (~1 hour at 10-second intervals)
- Dark theme consistent with existing design, responsive mobile layout
- 51 unit tests for computational logic

## Version 0.2.1

- block height is read from the latest `SetBestChain ... height=N` entry in `debug.log`;
- only the last 256 KiB of the log is inspected, avoiding full-file scans;
- `getinfo` is cached and polled every 60 seconds by default;
- `getpeerinfo` is cached and polled every 120 seconds by default;
- failed RPC calls use exponential backoff up to 15 minutes while the last successful snapshot remains available;
- RPC congestion during IBD is shown as a non-fatal cached-data notice rather than a dashboard failure;
- block heights use spaces as digit group separators, for example `7 838 242`;
- the API exposes metric source and freshness metadata.

The browser still refreshes every five seconds, but this no longer means an RPC call every five seconds.

## Version 0.2.0

- node uptime now comes from the running `innovad` process via `ps etimes`;
- node start time comes from `ps lstart`;
- the installer detects the owner and group of `INNOVA_DATADIR`;
- the systemd service runs as that owner, avoiding private datadir permission errors;
- the public dashboard UI is now in English.
