# Innova Node Dashboard API

## `GET /api/v1/status`

Read-only JSON status endpoint. Schema 5 makes peer availability explicit: a
top-level `peers` object distinguishes unavailable, cached/stale and fresh peer
snapshots, and peer-derived metrics are `null` (not `0`) when no successful
`getpeerinfo` result exists yet.

### Zero vs unavailable vs cached

The dashboard never infers zero from missing data.

- **Unavailable** — no successful `getpeerinfo` result in this process
  lifetime. `peers.available` is `false`, `peers.count` is `null`, and all
  peer-derived metrics are `null`.
- **Available / empty** — `getpeerinfo` succeeded and returned `[]`.
  `peers.available` is `true`, `peers.count` is `0`. This is the only state in
  which the UI may truthfully show "No peers".
- **Available / cached (stale)** — a previous success exists but the latest
  refresh failed, timed out, or aged past the polling interval. The last
  successful items are preserved and `peers.stale` is `true`.

### Top-level sections

```json
{
  "api": {"name": "innova-node-dashboard", "version": "v1", "schema": 5},
  "dashboard": {"version": "0.3.2"},
  "generated_at": "2026-07-28T12:00:00+02:00",
  "node": { ... },
  "chain": { ... },
  "network": { ... },
  "peers": { ... },
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

**`connections`** comes from `getinfo` and is independent of peer snapshot
availability. `inbound`, `outbound` and `average_ping_ms` are peer-derived:
they are `null` while no successful `getpeerinfo` result exists, and hold real
counts (including `0` for a valid empty peer snapshot) once available.

### `peers` — peer snapshot state

```json
{
  "available": true,
  "fresh": true,
  "stale": false,
  "count": 5,
  "items": [ ... ],
  "age_seconds": 12,
  "last_success_at": "2026-07-28T11:59:48+02:00",
  "last_attempt_at": "2026-07-28T12:00:00+02:00",
  "last_error": null
}
```

| field | type | meaning |
| --- | --- | --- |
| `available` | bool | `true` once a `getpeerinfo` call has ever succeeded this run |
| `fresh` | bool | available and the last success is within the polling interval with no failures since |
| `stale` | bool | available but the latest refresh failed, timed out, or aged past the polling interval |
| `count` | int \| null | number of peers; `null` when unavailable (never `0` unless truly zero peers) |
| `items` | array \| null | per-peer details; `null` when unavailable, `[]` for a valid empty snapshot |
| `age_seconds` | int \| null | seconds since the last successful fetch |
| `last_success_at` | ISO \| null | when the last successful result was stored |
| `last_attempt_at` | ISO \| null | when the last fetch attempt ran |
| `last_error` | string \| null | most recent failure reason |

When `available` is `false`, `count` and `items` are `null` — never `[]`/`0`.

The peer cache is invalidated (returns to `unavailable`) when the `innovad`
process restarts, so stale connections from a previous daemon instance are not
reported as authoritative.

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

**`peer_aggregation`** mirrors the `peers` object: `peer_count` and the derived
counters are `null` while the peer snapshot is unavailable and hold their real
values (including zeros) once available. `ibd.peers` is likewise `null` when
unavailable, never `[]` by default.

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
