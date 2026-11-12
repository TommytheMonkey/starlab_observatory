#!/usr/bin/env bash
# Put ANTSW-01 back in service in front of R05 (2026-11-12: M2 Eggbeater on port 1, discone on port 2).
#
#   sudo /home/tommy/starlab-dev/deploy/enable-antsw.sh
#
# Waits (up to MAX_WAIT_MIN, default 30) until no scheduled capture runs and the pirate watch is not recording,
# then: gives the starlab service account the board's API token, upgrades Starlab from this checkout (database
# backup first; the switch address saved on the Settings page takes effect at this start), and restarts the
# pirate watch so it asks for port 2. The receiver setting and the Signal Paths layout are done over the API
# afterwards; neither needs another restart.
set -euo pipefail
DEV=/home/tommy/starlab-dev
STATUS=/data/archive/pirate-watch/status.json
TOKEN_SRC=/home/tommy/.config/observatory/antswitch.env
TOKEN_DST=/data/starlab/.config/observatory/antswitch.env      # ~starlab: LiveHardware's default token_file
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

if ! cmp -s "$TOKEN_SRC" "$TOKEN_DST" 2>/dev/null; then
  install -D -o starlab -g starlab -m 600 "$TOKEN_SRC" "$TOKEN_DST"
  chown starlab:starlab "$(dirname "$TOKEN_DST")"
  say "switch token installed for the starlab account ($TOKEN_DST)"
fi

say "upgrading Starlab from $DEV ($(git -C "$DEV" log --oneline -1))"
bash "$DEV/install.sh" --upgrade --yes
for _ in $(seq 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8751/v1/rf || true)
  [ "$code" = 401 ] || [ "$code" = 200 ] && break
  sleep 2
done
systemctl is-active --quiet starlab || die "Starlab did not come back: journalctl -u starlab -n 80"
say "Starlab is up ($(cat /opt/starlab/VERSION))"

systemctl restart starlab-pirate-watch
sleep 5
systemctl is-active --quiet starlab-pirate-watch || die "the watch did not come back: journalctl -u starlab-pirate-watch -n 50"
say "pirate watch restarted (asks for ANTSW-01 port 2)"
say "done: tell Claude, which sets the switch receiver and saves the layout over the API"
