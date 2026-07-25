#!/usr/bin/env bash
set -euo pipefail

APP="innova-node-dashboard"
DEST="/opt/${APP}"
CFG="/etc/${APP}"
UNIT="/etc/systemd/system/${APP}.service"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

[[ ${EUID} -eq 0 ]] || { echo "Run: sudo ./scripts/install.sh"; exit 1; }

find_innovad() {
  local candidate
  for candidate in "${INNOVAD_PATH:-}" /home/user/innova/src/innovad /usr/local/bin/innovad /usr/bin/innovad "$(command -v innovad 2>/dev/null || true)"; do
    [[ -n "${candidate}" && -x "${candidate}" ]] && { echo "${candidate}"; return 0; }
  done
  return 1
}

find_datadir() {
  local candidate
  for candidate in "${INNOVA_DATADIR:-}" /home/user/.innova /root/.innova; do
    [[ -n "${candidate}" && -d "${candidate}" ]] && { readlink -f "${candidate}"; return 0; }
  done
  return 1
}

BIN="$(find_innovad || true)"
DATADIR="$(find_datadir || true)"
[[ -n "${BIN}" ]] || { echo "innovad not found; set INNOVAD_PATH"; exit 1; }
[[ -n "${DATADIR}" ]] || { echo "Innova datadir not found; set INNOVA_DATADIR"; exit 1; }

DASHBOARD_USER="${INNOVA_DASHBOARD_USER:-$(stat -c '%U' "${DATADIR}")}"
DASHBOARD_GROUP="${INNOVA_DASHBOARD_GROUP:-$(stat -c '%G' "${DATADIR}")}"

if ! id "${DASHBOARD_USER}" >/dev/null 2>&1; then
  echo "Detected datadir owner '${DASHBOARD_USER}' does not exist."
  exit 1
fi

mkdir -p "${DEST}/backend" "${DEST}/frontend/assets" "${CFG}"
install -m 0755 "${ROOT}/backend/server.py" "${DEST}/backend/server.py"
install -m 0644 "${ROOT}/frontend/index.html" "${DEST}/frontend/index.html"
install -m 0644 "${ROOT}/frontend/assets/app.css" "${DEST}/frontend/assets/app.css"
install -m 0644 "${ROOT}/frontend/assets/app.js" "${DEST}/frontend/assets/app.js"

if [[ ! -f "${CFG}/config.env" ]]; then
  cat > "${CFG}/config.env" <<EOF
INNOVAD_PATH=${BIN}
INNOVA_DATADIR=${DATADIR}
INNOVA_DASHBOARD_HOST=0.0.0.0
INNOVA_DASHBOARD_PORT=8787
INNOVA_DASHBOARD_REFRESH=5
INNOVA_DASHBOARD_RPC_TIMEOUT=8
INNOVA_DASHBOARD_INFO_INTERVAL=60
INNOVA_DASHBOARD_PEER_INTERVAL=120
INNOVA_DASHBOARD_MAX_BACKOFF=900
INNOVA_DASHBOARD_LOG_TAIL_BYTES=262144
INNOVA_DASHBOARD_FRONTEND=${DEST}/frontend
EOF
else
  echo "Preserving existing configuration: ${CFG}/config.env"
fi

chmod 0640 "${CFG}/config.env"
chown root:"${DASHBOARD_GROUP}" "${CFG}/config.env"

sed \
  -e "s/@DASHBOARD_USER@/${DASHBOARD_USER}/g" \
  -e "s/@DASHBOARD_GROUP@/${DASHBOARD_GROUP}/g" \
  "${ROOT}/packaging/systemd/innova-node-dashboard.service" > "${UNIT}"
chmod 0644 "${UNIT}"

chown -R root:root "${DEST}"
chmod -R a+rX "${DEST}"

systemctl daemon-reload
systemctl enable --now innova-node-dashboard.service
systemctl restart innova-node-dashboard.service

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Innova Node Dashboard 0.2.1 installed."
echo "Service user: ${DASHBOARD_USER}:${DASHBOARD_GROUP}"
echo "Node datadir: ${DATADIR}"
echo "Dashboard: http://${IP:-127.0.0.1}:8787"
echo "Config: ${CFG}/config.env"
