#!/usr/bin/env bash
# Second step of putting ANTSW-01 in front of R05 (2026-11-12):
#   * clears the 4 channels the pirate watch masked as "steady carriers" while the board sat on port 1 (Eggbeater,
#     14:35:51-15:08:07Z); the learned floor itself predates that window and matches port 2, so it is kept
#   * upgrades Starlab to per-port gain profiles (needed before they are saved in the layout)
#
#   sudo /home/tommy/starlab-dev/deploy/finish-antsw.sh
#
# Waits (up to MAX_WAIT_MIN, default 30) until no scheduled capture runs and the watch is not recording.
set -euo pipefail
DEV=/home/tommy/starlab-dev
WATCH_DIR=/data/archive/pirate-watch
STATUS=$WATCH_DIR/status.json
MAX_WAIT_MIN=${MAX_WAIT_MIN:-30}
say() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { say "STOPPED: $*"; exit 1; }
[ "$(id -u)" = 0 ] || die "run it with sudo"

quiet() {
  /opt/starlab/venv/bin/python3 - "$STATUS" <<'PY'
import datetime as dt, json, subprocess, sys
why = []
try:
    s = json.load(open(sys.argv[1]))
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(s["at"].replace("Z", "+00:00"))).total_seconds()
    if s.get("state") == "recording" and age < 120:
        why.append(f"the watch is recording ({s.get('detail')})")
except (OSError, ValueError, KeyError):
    pass
if any("aobs.scheduling.capture" in l for l in subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout.splitlines()):
    why.append("a scheduled capture is running")
print("; ".join(why))
PY
}
deadline=$(( $(date +%s) + MAX_WAIT_MIN * 60 )); last=""
while why=$(quiet) && [ -n "$why" ]; do
  [ "$why" != "$last" ] && say "waiting: $why"; last=$why
  [ "$(date +%s)" -lt "$deadline" ] || die "no quiet moment in $MAX_WAIT_MIN min; nothing changed"
  sleep 1
done

# the watch saves its baseline when it stops, so edit the file only while it is stopped
systemctl stop starlab-pirate-watch
cp -p "$WATCH_DIR/baseline.npz" "$WATCH_DIR/baseline.npz.bak-$(date +%Y%m%d-%H%M%S)"
sudo -u tommy "$DEV/.venv/bin/python" - "$WATCH_DIR/baseline.npz" <<'PY'
import sys, numpy as np
path = sys.argv[1]
z = dict(np.load(path))
f, em = z["freqs"], z["extra_mask"]
# masked on the Eggbeater (port 1), 14:42-15:05Z: 246.0694, 264.7283, 263.2928, 253.0376 MHz (+-15 kHz)
wrong = np.zeros_like(em)
for c in (246_069_400, 264_728_300, 263_292_800, 253_037_600):
    wrong |= (f >= c - 15_000) & (f <= c + 15_000)
before = int(em.sum())
z["extra_mask"] = em & ~wrong
with open(path, "wb") as fh:
    np.savez(fh, **z)
print(f"auto-masked bins: {before} -> {int(z['extra_mask'].sum())}")
PY
say "baseline: the 4 port-1 masks cleared (backup next to it)"

say "upgrading Starlab from $DEV ($(git -C "$DEV" log --oneline -1))"
bash "$DEV/install.sh" --upgrade --yes
for _ in $(seq 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8751/v1/rf || true)
  [ "$code" = 401 ] || [ "$code" = 200 ] && break
  sleep 2
done
systemctl is-active --quiet starlab || die "Starlab did not come back: journalctl -u starlab -n 80"
say "Starlab is up ($(cat /opt/starlab/VERSION))"

systemctl start starlab-pirate-watch
sleep 5
systemctl is-active --quiet starlab-pirate-watch || die "the watch did not come back: journalctl -u starlab-pirate-watch -n 50"
say "pirate watch started; tell Claude, which saves the per-port gain profiles"
