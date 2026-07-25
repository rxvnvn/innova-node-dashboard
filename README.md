# Innova Node Dashboard

Lightweight read-only dashboard for `innovad`, with frontend and backend deliberately separated by a stable API:

```text
Browser frontend -> GET /api/v1/status -> Python adapter today / innovad later
```

## Features

Current height, start time, uptime, IBD, connections, inbound/outbound peers, average ping, traffic, version, build commit, PID, mobile layout, JSON API. No third-party Python packages.

## Test run

```bash
python3 backend/server.py --innovad /home/user/innova/src/innovad --datadir /home/user/.innova --host 0.0.0.0 --port 8787
```

Open `http://VPS_IP:8787`.

```bash
curl http://127.0.0.1:8787/api/v1/status
curl http://127.0.0.1:8787/health
```

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


## Version 0.2.0

- node uptime now comes from the running `innovad` process via `ps etimes`;
- node start time comes from `ps lstart`;
- the installer detects the owner and group of `INNOVA_DATADIR`;
- the systemd service runs as that owner, avoiding private datadir permission errors;
- the public dashboard UI is now in English.

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
