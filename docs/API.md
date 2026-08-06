# Innova Node Dashboard API

## `GET /api/v1/status`

Read-only JSON status endpoint. Schema 4 adds restored traffic counters (parsed byte totals), live throughput (`network.traffic`), and per-peer version fields.

### Top-level sections

```json
{
  "api": {"name": "innova-node-dashboard", "version": "v1", "schema": 4},
  "dashboard": {"version": "0.3.1"},
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

### `network` — traffic & throughput

```json
{
  "connections": 12,
  "inbound": 2,
  "outbound": 10,
  "average_ping_ms": 221.4,
  "bytes_received": 565183488,
  "bytes_sent": 83919555,
  "traffic": {
    "received": 565183488,
    "sent": 83919555,
    "rx_rate_bps": 1243000.0,
    "tx_rate_bps": 182000.0,
    "source": "getinfo"
  }
}
```

**Cumulative counters.** `getinfo` returns `datareceived`/`datasent` as human-readable strings (for example `"537.48 MB"`). The backend parses them back to bytes. When `getinfo` totals are unavailable, per-peer byte counters are summed and `source` becomes `"peers"`.

**Throughput.** `rx_rate_bps`/`tx_rate_bps` are derived from consecutive traffic samples (an average over the last counter update interval, normally one `getinfo` poll). No additional RPC polling is performed; the values are computed from data already being fetched.

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

**`peers`** — array of per-peer details with `addr`, `version` (user agent, e.g. `/innova:5.0.1/`), `protocol_version`, `starting_height`, `best_known_height`, `blocks_inflight`, `ask_queue`, `bytes_received`, `bytes_sent`, `pingtime`.

The daemon reports per-peer sent bytes as `bytessend` (not `bytessent`); the backend reads the correct field. `best_known_height` is the peer-reported chain tip and is shown under Start height when the daemon does not expose a starting height.

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
