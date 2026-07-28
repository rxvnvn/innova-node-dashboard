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
python3 -m unittest tests.test_ibd_logic -v
```

Tests cover: blocks/min rate calculation, zero growth, negative delta after restart, 5-minute sliding average, session average, ETA, zero-rate ETA suppression, peer aggregation, estimated tip with outliers, sync state classification, bounded sample history, and restart session detection.

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
