# Innova Node Dashboard Roadmap

## v0.4 candidates

- ETA refinement
- Prometheus exporter
- WebSocket updates
- Peer diagnostics
- Multiple node support
- Historical charts
- Event timeline

## Future

- Stable versioned monitoring API
- Native read-only monitoring API in `innovad`
- Opt-in built-in Dashboard enabled from `innova.conf`
- Safe localhost binding by default
- Direct internal metrics without heavy RPC polling
- Reuse the mature standalone frontend inside `innovad`

The standalone Dashboard remains the development and compatibility layer for
older Innova Core versions and for testing new monitoring features before
native integration.
