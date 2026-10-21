# Innova Node Dashboard API

## `GET /api/v1/status`

Read-only JSON status endpoint. Schema 6 adds confirmed daemon lifecycle state:
`node.online` is now tri-state (running / stopped / unknown), daemon-specific
data is suppressed once a shutdown is confirmed, and `chain.last_known_height`
preserves the last observed height as historical information after a stop.
Peer availability semantics (schema 5) are unchanged: a top-level `peers`
object distinguishes unavailable, cached/stale and fresh peer snapshots, and
peer-derived metrics are `null` (not `0`) when no successful `getpeerinfo`
result exists yet.

### Daemon lifecycle

The dashboard tracks the `innovad` process PID and confirms state transitions
before changing how data is presented:

- **running** — a live process with a matching PID was observed. Height,
  connections, peers and traffic are live.
- **stopped** — a previously running daemon was confirmed absent. `node.online`
  is `false`, live data is suppressed (`chain.height`, `network.*` totals,
  traffic rates are `null`), and only historical information remains available
  (`chain.last_known_height`, the stale peer snapshot).
- **unknown** — no running process has ever been observed this run.
  `node.online` is `null`; the UI shows "Checking". No live data is claimed.

**Shutdown confirmation (2 misses).** A single PID-detection miss is treated as
a transient lookup failure and does not stop the node. Two consecutive misses
while the daemon was previously running confirm shutdown. A transient RPC
failure is different: it never changes lifecycle state, and the last successful
peer snapshot is preserved and marked stale.

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
  "api": {"name": "innova-node-dashboard", "version": "v1", "schema": 6},
  "dashboard": { "version": "0.4.0" },
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

### `node` — lifecycle state

```json
{
  "online": true,
  "network": "mainnet",
  "version": "5.0.1",
  "build_commit": "abc123",
  "build_dirty": false,
  "protocol_version": 70015,
  "started_at": "2026-07-28T10:00:00+02:00",
  "uptime_seconds": 7200
}
```

`online` is tri-state:

| value | meaning | UI |
| --- | --- | --- |
| `true` | daemon confirmed running | Online |
| `false` | daemon confirmed stopped (2 consecutive PID misses) | Offline |
| `null` | no running daemon observed this run | Checking |

`started_at` / `uptime_seconds` are `null` while the daemon is not confirmed
running.

### `chain` — extended

```json
{
  "height": 6484358,
  "height_source": "debug_log",
  "height_updated_at": "2026-07-25T17:00:00+02:00",
  "last_known_height": 6812442,
  "initial_block_download": true,
  "verification_progress": 0.9998
}
```

`height` is the live height and is `null` when the daemon is not confirmed
running. `last_known_height` is the most recent live height observed during this
dashboard run, retained as historical information after a stop; it is `null`
until a live height has been observed and never affects `node.online`.
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

When the daemon is confirmed stopped, `network.connections`, `inbound`,
`outbound`, `average_ping_ms`, cumulative traffic and both traffic rates are
`null`: cached values from before shutdown are not presented as live.

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
reported as authoritative. While the daemon is stopped the last snapshot is
preserved and marked `stale`; it is historical, not current, and `network`
peer-derived totals are `null`.

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

Returns HTTP 200 only when the daemon is confirmed running
(`node.online === true`), otherwise HTTP 503. It does not require a successful
RPC response. A stopped daemon is not healthy; an unobserved (unknown) daemon is
not yet confirmed healthy.
