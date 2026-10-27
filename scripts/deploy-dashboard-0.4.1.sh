#!/usr/bin/env bash
# Deploy Innova Node Dashboard 0.4.1 on this host.
# Run as root:  sudo bash deploy.sh
#
# - backs the current /opt/innova-node-dashboard up to a timestamped tarball
# - stages the new release next to it and compiles the backend
# - swaps atomically, restarts ONLY innova-node-dashboard.service
# - smoke-tests the API + frontend and rolls back automatically on failure
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/innova-node-dashboard
SERVICE=innova-node-dashboard.service
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/opt/innova-node-dashboard-$STAMP.tar.gz"
NEW="$DEST.new-$STAMP"
OLD="$DEST.old-$STAMP"

if [ "$(id -u)" -ne 0 ]; then echo "must run as root (sudo bash deploy.sh)"; exit 1; fi
if [ ! -d "$DEST" ]; then echo "no dashboard at $DEST"; exit 1; fi

echo "== 1. backup =="
tar -czf "$BACKUP" -C /opt "$(basename "$DEST")"
echo "backup: $BACKUP ($(stat -c%s "$BACKUP") bytes)"
echo "$BACKUP" > /root/.dashboard-last-backup

rollback() {
  echo "== ROLLBACK to $BACKUP =="
  rm -rf "$NEW"
  mkdir -p "$NEW"
  tar -xzf "$BACKUP" -C "$NEW" --strip-components=1
  rm -rf "$DEST"
  mv "$NEW" "$DEST"
  systemctl restart "$SERVICE"
  sleep 5
  echo "restored API:"
  curl -fsS --max-time 10 http://127.0.0.1:8787/api/v1/status | head -c 160 || true
  echo
  echo "ROLLBACK DONE — previous version is live again"
}

echo "== 2. stage =="
rm -rf "$NEW"
mkdir -p "$NEW"
cp -a "$DEST"/. "$NEW"/
install -m 644 "$SRC/backend/server.py"      "$NEW/backend/server.py"
install -m 644 "$SRC/frontend/assets/app.js" "$NEW/frontend/assets/app.js"
install -m 644 "$SRC/VERSION"                "$NEW/VERSION"
python3 -m py_compile "$NEW/backend/server.py"
echo "staged in $NEW (backend compiles)"

echo "== 3. swap + restart $SERVICE =="
mv "$DEST" "$OLD"
mv "$NEW" "$DEST"
systemctl restart "$SERVICE"
sleep 6
systemctl is-active "$SERVICE" || { echo "service not active"; rollback; exit 2; }

echo "== 4. smoke =="
status="$(curl -fsS --max-time 10 http://127.0.0.1:8787/api/v1/status || true)"
echo "$status" | head -c 400; echo
ok=1
case "$status" in *'"schema":7'*) : ;; *) echo "FAIL: schema is not 7"; ok=0 ;; esac
case "$status" in *'"version":"0.4.1"'*) : ;; *) echo "FAIL: dashboard version is not 0.4.1"; ok=0 ;; esac
case "$status" in *'"sync_phase"'*) : ;; *) echo "FAIL: sync_phase missing"; ok=0 ;; esac
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8787/)"
echo "frontend HTTP $code"
[ "$code" = "200" ] || { echo "FAIL: frontend not serving"; ok=0; }
js="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8787/assets/app.js)"
echo "app.js HTTP $js"
[ "$js" = "200" ] || { echo "FAIL: app.js not serving"; ok=0; }

if [ "$ok" -ne 1 ]; then rollback; exit 2; fi

echo "== 5. journal (last 20 lines) =="
journalctl -u "$SERVICE" -n 20 --no-pager || true

echo
echo "DEPLOY OK: dashboard 0.4.1 (schema 7) live from $DEST"
echo "previous tree kept at $OLD ; backup tarball $BACKUP"
echo "manual rollback if ever needed:  bash rollback.sh $BACKUP"
