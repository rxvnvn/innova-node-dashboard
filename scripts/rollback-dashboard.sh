#!/usr/bin/env bash
# Restore a dashboard tree from a backup tarball made by deploy.sh.
#   sudo bash rollback.sh /opt/innova-node-dashboard-<stamp>.tar.gz
set -euo pipefail
BACKUP="${1:-$(cat /root/.dashboard-last-backup 2>/dev/null || true)}"
DEST=/opt/innova-node-dashboard
SERVICE=innova-node-dashboard.service

[ "$(id -u)" -eq 0 ] || { echo "must run as root"; exit 1; }
[ -n "$BACKUP" ] && [ -f "$BACKUP" ] || { echo "usage: rollback.sh <backup.tar.gz>"; exit 1; }

echo "restoring $BACKUP -> $DEST"
rm -rf /opt/dashboard-restore
mkdir -p /opt/dashboard-restore
tar -xzf "$BACKUP" -C /opt/dashboard-restore --strip-components=1
rm -rf "$DEST"
mv /opt/dashboard-restore "$DEST"
systemctl restart "$SERVICE"
sleep 5
systemctl is-active "$SERVICE"
curl -fsS --max-time 10 http://127.0.0.1:8787/api/v1/status | head -c 160; echo
echo "restored"
