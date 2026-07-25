#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]]||{ echo 'Run with sudo'; exit 1; }
"$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/install.sh"
systemctl restart innova-node-dashboard.service
echo 'Updated.'
