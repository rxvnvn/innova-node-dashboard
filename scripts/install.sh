#!/usr/bin/env bash
set -euo pipefail
APP=innova-node-dashboard; DEST=/opt/$APP; CFG=/etc/$APP; ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $EUID -eq 0 ]]||{ echo 'Run: sudo ./scripts/install.sh'; exit 1; }
find_innovad(){ for p in "${INNOVAD_PATH:-}" /home/user/innova/src/innovad /usr/local/bin/innovad /usr/bin/innovad "$(command -v innovad 2>/dev/null||true)"; do [[ -n "$p" && -x "$p" ]]&&{ echo "$p"; return; }; done; }
BIN="$(find_innovad||true)"; [[ -n "$BIN" ]]||{ echo 'innovad not found; set INNOVAD_PATH'; exit 1; }
id innova-dashboard &>/dev/null||useradd --system --home-dir "$DEST" --shell /usr/sbin/nologin innova-dashboard
mkdir -p "$DEST/backend" "$DEST/frontend/assets" "$CFG"
install -m755 "$ROOT/backend/server.py" "$DEST/backend/server.py"; install -m644 "$ROOT/frontend/index.html" "$DEST/frontend/index.html"; install -m644 "$ROOT/frontend/assets/app.css" "$DEST/frontend/assets/app.css"; install -m644 "$ROOT/frontend/assets/app.js" "$DEST/frontend/assets/app.js"
if [[ ! -f "$CFG/config.env" ]]; then cat > "$CFG/config.env" <<EOF
INNOVAD_PATH=$BIN
INNOVA_DATADIR=${INNOVA_DATADIR:-/home/user/.innova}
INNOVA_DASHBOARD_HOST=0.0.0.0
INNOVA_DASHBOARD_PORT=8787
INNOVA_DASHBOARD_REFRESH=5
INNOVA_DASHBOARD_RPC_TIMEOUT=8
INNOVA_DASHBOARD_FRONTEND=$DEST/frontend
EOF
chmod 640 "$CFG/config.env"; chown root:innova-dashboard "$CFG/config.env"; fi
install -m644 "$ROOT/packaging/systemd/innova-node-dashboard.service" /etc/systemd/system/innova-node-dashboard.service
chown -R root:root "$DEST"; chmod -R a+rX "$DEST"; systemctl daemon-reload; systemctl enable --now innova-node-dashboard.service
IP="$(hostname -I 2>/dev/null|awk '{print $1}')"; echo "Installed: http://${IP:-127.0.0.1}:8787"; echo "Config: $CFG/config.env"
