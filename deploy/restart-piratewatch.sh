#!/usr/bin/env bash
# Restart the pirate watch between recordings (and refresh the web page file, which needs no Starlab restart).
#
#   sudo /home/tommy/starlab-dev/deploy/restart-piratewatch.sh
#
# Waits (up to MAX_WAIT_MIN, default 30) until the watch is not recording and no scheduled capture process runs.
set -euo pipefail
DEV=/home/tommy/starlab-dev
STATUS=/data/archive/pirate-watch/status.json
PAGE=/opt/starlab/venv/lib/python3.14/site-packages/aobs/web/piratewatch.html
PAGE_BEFORE=ac6d7156d14bfbbf99b44edd94ce2e09c291305d54cc7cb07b089970aaee2d55     # the page as installed by install-piratewatch-web.sh (21e6199)
MAX_WAIT_MIN=${MAX_WAIT_MIN:-30}
say() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { say "STOPPED: $*"; exit 1; }
[ "$(id -u)" = 0 ] || die "run it with sudo"

# the page: replaced only if production still has the version this builds on
if ! cmp -s "$DEV/src/aobs/web/piratewatch.html" "$PAGE"; then
  if [ "$(sha256sum "$PAGE" | cut -d' ' -f1)" = "$PAGE_BEFORE" ]; then
    cp -p "$PAGE" "$PAGE.bak-$(date +%Y%m%d-%H%M%S)"
    install -m 644 -o root -g root "$DEV/src/aobs/web/piratewatch.html" "$PAGE"
    say "web page updated (served per request: no Starlab restart)"
  else
    say "web page NOT updated: production's copy is not the one this was built on"
  fi
fi

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
  [ "$(date +%s)" -lt "$deadline" ] || die "no quiet moment in $MAX_WAIT_MIN min; not restarted"
  sleep 1
done
systemctl restart starlab-pirate-watch
sleep 5
systemctl is-active --quiet starlab-pirate-watch || die "the watch did not come back: journalctl -u starlab-pirate-watch -n 50"
say "watch restarted (pid $(systemctl show -p MainPID --value starlab-pirate-watch))"
