#!/bin/bash
# Give receivers back to their legacy recorder services (undoes migrate-recorders.sh).
#
#   sudo deploy/sudo/rollback-recorders.sh [OBS-R01] [RSP1B]
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
REPO=/home/tommy/auto-observe
AOBS=(sudo -u tommy "$REPO/.venv/bin/aobs")
declare -A UNIT=([OBS-R01]=observatory-record@OBS-R01.service [RSP1B]=meteor-rsp1b.service)
receivers=("$@")
[[ ${#receivers[@]} -gt 0 ]] || receivers=(OBS-R01 RSP1B)

echo "== Preflight (no broker recording running, nothing reserved in 20 min)"
"${AOBS[@]}" schedule preflight --receivers "${receivers[@]}" --skip-shadow

echo "== Mark the receivers legacy again and restart the broker (it cancels its future automatic jobs)"
for rx in "${receivers[@]}"; do
  "${AOBS[@]}" schedule set-mode --receivers "$rx" --mode legacy
done
if printf '%s\n' "${receivers[@]}" | grep -qx OBS-R01; then
  sudo -u tommy sed -i 's/^satops_publish = false/satops_publish = true /' "$REPO/config/observatory.toml"
fi
systemctl restart observatory-broker.service
sleep 5

echo "== Start the legacy recorders"
for rx in "${receivers[@]}"; do
  systemctl unmask "${UNIT[$rx]}"
  systemctl enable --now "${UNIT[$rx]}"
done
systemctl --no-pager is-active "${UNIT[@]}" || true
