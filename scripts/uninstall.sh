#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]]||{ echo 'Run with sudo'; exit 1; }
systemctl disable --now innova-node-dashboard.service 2>/dev/null||true
rm -f /etc/systemd/system/innova-node-dashboard.service; systemctl daemon-reload; rm -rf /opt/innova-node-dashboard
echo 'Application removed; configuration preserved in /etc/innova-node-dashboard.'
