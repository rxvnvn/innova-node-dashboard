# Innova Node Dashboard API

## `GET /api/v1/status`

Read-only JSON status endpoint. Schema 3 adds IBD benchmark data, peer aggregation, host metrics, and height history.

### Top-level sections

```json
{
  "api": {"name": "innova-node-dashboard", "version": "v1", "schema": 3},
  "dashboard": {"version": "0.3.0"},
  "generated_at": "2026-07-28T12:00:00+02:00",
  "node": { ... },
  "chain": { ... },
  "network": { ... },
  "ibd": { ... },
  "host": { ... },
  "history": [ ... ],
  "freshness": { ... },
  "features": { ... },
  "notices": [],
  "errors": []
}
```

### `chain` — extended

```json
{
  "height": 6484358,
  "height_source": "debug_log",
  "height_updated_at": "2026-07-25T17:00:00+02:00",
  "initial_block_download": true,
  "verification_progress": 0.9998
}
```

`verification_progress` comes from `getblockchaininfo`. Values near 1.0 indicate the node is nearly synced.

### `ibd` — IBD benchmark

```json
{
  "session_type": "resume_ibd",
  "session_start_height": 5000000,
  "estimated_tip": 6500000,
  "current_rate_bpm": 142.5,
  "rate_5min_bpm": 138.2,
  "rate_ema_bpm": 140.1,
  "session_rate_bpm": 135.7,
  "eta_seconds": 104400,
  "last_height_advance_ago_seconds": 5,
  "sync_state": "healthy",
  "peer_aggregation": {
    "active_download_peers": 8,
    "total_blocks_inflight": 32,
    "total_ask_queue": 64,
    "peer_count": 12
  },
  "peers": [ ... ]
}
```

**`session_type`**:
- `cold_ibd` — session started from height < 1000 (cold start)
- `resume_ibd` — session started from higher height (resume)
- `synced` — node is no longer in IBD

**`estimated_tip`** — 75th percentile of peer `startingheight` values, filtered to exclude zeros and values > 2.5× current height.

**Rate fields** — blocks per minute:
- `current_rate_bpm` — from the last two samples
- `rate_5min_bpm` — sliding window average over 300 seconds
- `rate_ema_bpm` — exponential moving average (α=0.15)
- `session_rate_bpm` — from session start to now

**`eta_seconds`** — estimated time remaining to reach `estimated_tip`, calculated using `rate_ema_bpm`. Null when insufficient data or zero rate.

**`sync_state`**:
- `healthy` — height advanced < 60 seconds ago
- `slow` — no advance for 60–180 seconds
- `stalled` — no advance > 180 seconds during IBD
- `synced` — IBD complete
- `rpc_unavailable` — RPC calls failing
- `unknown` — cannot determine state

**`peers`** — array of per-peer details with `addr`, `starting_height`, `blocks_inflight`, `ask_queue`, `bytes_received`, `bytes_sent`, `pingtime`.

### `host` — host metrics

```json
{
  "cpu_count": 4,
  "total_ram_bytes": 8589934592
}
```

No PID, no datadir, no credentials exposed.

### `history` — height samples

Array of up to 60 recent samples (10-minute window at 10-second intervals):

```json
[{"time": 1722168000, "height": 6484358, "connections": 12, "bytes_received": 1024000, "bytes_sent": 512000}]
```

Stored in memory only. No database. After dashboard restart, a new observed session begins.

### `freshness` — extended

```json
{
  "getinfo_age_seconds": 42,
  "getpeerinfo_age_seconds": 93,
  "getblockchaininfo_age_seconds": 42,
  "getinfo_failures": 0,
  "getpeerinfo_failures": 1,
  "getblockchaininfo_failures": 0
}
```

## `GET /health`

Returns HTTP 200 when either the daemon process or a current chain height is detectable. It does not require a successful RPC response.
