# Innova Node Dashboard API

## `GET /api/v1/status`

Read-only JSON status endpoint. Schema 2 keeps the original top-level sections and adds source/freshness metadata.

Important fields:

```json
{
  "chain": {
    "height": 6484358,
    "height_source": "debug_log",
    "height_updated_at": "2026-07-25T17:00:00+02:00"
  },
  "freshness": {
    "getinfo_age_seconds": 42,
    "getpeerinfo_age_seconds": 93,
    "getinfo_failures": 0,
    "getpeerinfo_failures": 1
  },
  "notices": [
    "RPC busy: getpeerinfo unavailable (timed out after 8 seconds)"
  ],
  "errors": []
}
```

`height_source` is `debug_log`, `rpc`, or `null`. RPC timeout notices are non-fatal because cached values remain usable.

## `GET /health`

Returns HTTP 200 when either the daemon process or a current chain height is detectable. It does not require a successful RPC response.
