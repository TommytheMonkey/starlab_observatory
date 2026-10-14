#!/bin/bash
# Cut receivers over from their legacy recorder services to the broker (the unified scheduler).
#
#   sudo deploy/sudo/migrate-recorders.sh [--skip-shadow] [OBS-R01] [RSP1B]
#
# Refuses unless: no broker recording is running, nothing is reserved on these receivers in the
# next 20 minutes, the legacy recorder is not writing a file right now, and (unless --skip-shadow)
# the shadow comparison shows the broker would have recorded what the old service recorded.
# Undo with deploy/sudo/rollback-recorders.sh.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
REPO=/home/tommy/auto-observe
AOBS=(sudo -u tommy "$REPO/.venv/bin/aobs")
declare -A UNIT=([OBS-R01]=observatory-record@OBS-R01.service [RSP1B]=meteor-rsp1b.service)
receivers=(); skip=()
for a in "$@"; do
  case $a in
    --skip-shadow) skip=(--skip-shadow) ;;
    OBS-R01|RSP1B) receivers+=("$a") ;;
    *) echo "Unknown argument: $a" >&2; exit 2 ;;
  esac
done
[[ ${#receivers[@]} -gt 0 ]] || receivers=(OBS-R01 RSP1B)

echo "== Preflight"
"${AOBS[@]}" schedule preflight --receivers "${receivers[@]}" "${skip[@]}"
for rx in "${receivers[@]}"; do
  if [[ $rx == OBS-R01 ]] && pgrep -f "rtl_sdr -d OBS-R01" >/dev/null; then
    echo "OBS-R01's legacy recorder is recording right now; try again after the pass." >&2; exit 3
  fi
  if [[ $rx == RSP1B ]] && find /data/sdrplay/meteor -maxdepth 1 -name 'auto_*.cs16' -mmin -1 | grep -q .; then
    echo "The RSP1B legacy recorder wrote a file in the last minute; try again after the pass." >&2; exit 3
  fi
done

echo "== Install the broker unit"
install -m 0644 "$REPO/deploy/broker/observatory-broker.service" /etc/systemd/system/observatory-broker.service
systemctl daemon-reload

echo "== Stop, disable and mask the legacy recorders"
for rx in "${receivers[@]}"; do
  systemctl disable --now "${UNIT[$rx]}"
  systemctl mask "${UNIT[$rx]}"
done
# Old independent schedulers that must never come back on a reboot.
systemctl mask observatory-record@OBS-R02.service observatory-sdr-scheduler.service

echo "== Hand the receivers to the broker"
for rx in "${receivers[@]}"; do
  "${AOBS[@]}" schedule set-mode --receivers "$rx" --mode broker
done
if printf '%s\n' "${receivers[@]}" | grep -qx OBS-R01; then
  sudo -u tommy sed -i 's/^satops_publish = true /satops_publish = false/' "$REPO/config/observatory.toml"
  "${AOBS[@]}" schedule retire-satops-events || echo "(could not remove old satops calendar events; harmless duplicates)"
fi

echo "== Run the broker under systemd"
pkill -u tommy -f "[.]venv/bin/aobs broker" || true   # [.] keeps pkill from matching itself
sleep 2
systemctl enable --now observatory-broker.service
sleep 5
systemctl --no-pager --lines=5 status observatory-broker.service
echo
echo "Done. Check with: $REPO/.venv/bin/aobs schedule jobs"
