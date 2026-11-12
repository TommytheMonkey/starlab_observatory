#!/usr/bin/env bash
# Install the Pirate Watch web page into production Starlab and restart both services, but only in a quiet moment.
#
#   sudo /home/tommy/starlab-dev/deploy/install-piratewatch-web.sh
#
# What it does:
#   1. Checks the production files it replaces are exactly the ones this change was built on (else stops, no changes).
#   2. Waits (up to MAX_WAIT_MIN, default 60) until the pirate watch is not recording and no scheduled capture is
#      running, preparing, or due to start within the next 10 minutes. Nothing is changed while it waits.
#   3. Backs up the production files, installs the new ones, and checks they import; if not, restores and stops.
#   4. Restarts starlab.service, then starlab-pirate-watch (the watch loses its broker lease when the broker restarts,
#      so it is restarted second and takes a fresh one), re-checking the quiet moment before each.
#   5. Checks the page and the forwarded API answer.
# Rollback: the backup folder printed at the end, copied back, then `systemctl restart starlab.service`.
set -euo pipefail

DEV=/home/tommy/starlab-dev
VENV=/opt/starlab/venv
WATCH_DATA=/data/archive/pirate-watch
MAX_WAIT_MIN=${MAX_WAIT_MIN:-60}
SOON_MIN=10

# production files this change replaces, and their sha256 when it was built (production as of 2026-11-12)
declare -A EXPECT=(
  [coordination/api.py]=a0bd4b842dbd1b753ebc5791113c6910c6d82897d9133246bb312892c4906d2e
  [scheduling/rf_ui.html]=482c7f7c9acbbf6d95fa97607248a4f7d425dd189c1900606fe9b4411e241c00
)
FILES=(coordination/api.py scheduling/rf_ui.html web/piratewatch_proxy.py web/piratewatch.html)

say() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { say "STOPPED: $*"; exit 1; }

[ "$(id -u)" = 0 ] || die "run it with sudo"
PKG=$("$VENV/bin/python3" -c 'import aobs, os; print(os.path.dirname(aobs.__file__))')
[ -d "$PKG" ] || die "Starlab is not installed at $VENV"

# ---- 1. preflight (no changes)
for f in "${!EXPECT[@]}"; do
  got=$(sha256sum "$PKG/$f" | cut -d' ' -f1)
  [ "$got" = "${EXPECT[$f]}" ] || die "$PKG/$f has changed since this was built; nothing was installed"
done
for f in "${FILES[@]}"; do [ -f "$DEV/src/aobs/$f" ] || die "missing $DEV/src/aobs/$f"; done
"$VENV/bin/python3" -m py_compile "$DEV/src/aobs/coordination/api.py" "$DEV/src/aobs/web/piratewatch_proxy.py" \
  || die "the new files do not compile; nothing was installed"
systemctl is-active --quiet starlab.service || die "starlab.service is not running; start it first"
say "preflight ok: production matches, new files compile"

# ---- 2. quiet moment: watch not recording, no capture running/preparing/due within SOON_MIN minutes
busy() {
  "$VENV/bin/python3" - "$WATCH_DATA/status.json" "$SOON_MIN" <<'PY'
import datetime as dt, json, os, pathlib, subprocess, sys, urllib.request
from aobs.config import load_config
reasons = []
try:
    st = json.loads(pathlib.Path(sys.argv[1]).read_text())
    at = dt.datetime.fromisoformat(st.get("at", "").replace("Z", "+00:00"))
    # a recording refreshes status.json every few seconds; an old "recording" is a hung watch, not a recording
    fresh = (dt.datetime.now(dt.timezone.utc) - at).total_seconds() < 120
    if st.get("state") == "recording" and fresh:
        reasons.append(f"the pirate watch is recording ({st.get('detail')})")
except (OSError, ValueError):
    pass
out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
if any("aobs.scheduling.capture" in line for line in out.splitlines()):
    reasons.append("a scheduled capture process is running")
cfg = load_config("/etc/starlab/starlab.toml")
b = cfg.section("broker")
token = pathlib.Path(b["token_file"]).read_text().strip()
url = f"http://127.0.0.1:{int(b.get('port', 8751))}/v1/jobs?status=scheduled,preparing,recording&hours_back=12&hours_ahead=1"
try:
    jobs = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=15))["jobs"]
except Exception as exc:  # noqa: BLE001
    reasons.append(f"cannot read the schedule from the broker ({exc})")
    jobs = []
now = dt.datetime.now(dt.timezone.utc)
soon = now + dt.timedelta(minutes=int(sys.argv[2]))
for j in jobs:
    if not j.get("executes"):
        continue
    if not j.get("reservation_start") or not j.get("reservation_end"):
        continue
    start = dt.datetime.fromisoformat(j["reservation_start"])
    end = dt.datetime.fromisoformat(j["reservation_end"])
    if j["status"] in ("preparing", "recording") or (start <= soon and end >= now):
        reasons.append(f"{j.get('target') or j['job_name']} on {j['receiver_id']} ({j['status']}, "
                       f"{start.astimezone():%H:%M}-{end.astimezone():%H:%M})")
print("; ".join(reasons))
PY
}
wait_quiet() {
  local deadline=$(( $(date +%s) + MAX_WAIT_MIN * 60 )) why last=""
  while :; do
    why=$(busy) || why="the quiet-moment check failed"
    [ -z "$why" ] && return 0
    [ "$why" != "$last" ] && say "waiting: $why"
    last=$why
    [ "$(date +%s)" -lt "$deadline" ] || die "no quiet moment in $MAX_WAIT_MIN min (last: $why); nothing was restarted"
    sleep 15
  done
}
wait_quiet
say "quiet: no recording and no capture running or due within $SOON_MIN min"

# ---- 3. install (with backup), check it imports
BACKUP=/opt/starlab/backups/piratewatch-web-$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP"
for f in "${FILES[@]}"; do
  if [ -f "$PKG/$f" ]; then mkdir -p "$BACKUP/$(dirname "$f")"; cp -p "$PKG/$f" "$BACKUP/$f"; fi
done
restore() {
  for f in "${FILES[@]}"; do
    if [ -f "$BACKUP/$f" ]; then install -m 644 -o root -g root "$BACKUP/$f" "$PKG/$f"; else rm -f "$PKG/$f"; fi
  done
}
for f in "${FILES[@]}"; do install -m 644 -o root -g root "$DEV/src/aobs/$f" "$PKG/$f"; done
if ! "$VENV/bin/python3" -c 'import aobs.coordination.api, aobs.web.piratewatch_proxy' 2>/tmp/piratewatch-web-import.err; then
  restore
  die "the installed code does not import (see /tmp/piratewatch-web-import.err); production files restored, nothing restarted"
fi
say "installed ${#FILES[@]} files (backup: $BACKUP)"

# ---- 4. restarts, each in a quiet moment
wait_quiet
say "restarting starlab.service"
systemctl restart starlab.service
for _ in $(seq 60); do
  curl -fsS -o /dev/null "http://127.0.0.1:8751/piratewatch" 2>/dev/null && break
  sleep 1
done
systemctl is-active --quiet starlab.service || die "starlab.service did not come back; see: journalctl -u starlab.service -n 50 (backup: $BACKUP)"
curl -fsS -o /dev/null "http://127.0.0.1:8751/piratewatch" || die "starlab.service is up but /piratewatch does not answer (backup: $BACKUP)"
say "starlab.service is up and serves /piratewatch"

wait_quiet
say "restarting starlab-pirate-watch"
systemctl restart starlab-pirate-watch
TOKEN_FILE=$("$VENV/bin/python3" -c 'from aobs.config import load_config; print(load_config("/etc/starlab/starlab.toml").section("broker")["token_file"])')
ok=""
for _ in $(seq 45); do
  if curl -fsS -H "Authorization: Bearer $(cat "$TOKEN_FILE")" "http://127.0.0.1:8751/v1/piratewatch" >/tmp/piratewatch-web-status.json 2>/dev/null; then ok=1; break; fi
  sleep 1
done
systemctl is-active --quiet starlab-pirate-watch || die "starlab-pirate-watch did not come back; see: journalctl -u starlab-pirate-watch -n 50"
[ -n "$ok" ] || die "the watch is running but the page cannot reach it through the broker; see: journalctl -u starlab-pirate-watch -n 50"

# ---- 5. done
state=$("$VENV/bin/python3" -c 'import json; d=json.load(open("/tmp/piratewatch-web-status.json")); print(d.get("state"), "-", d.get("detail") or "")')
rm -f /tmp/piratewatch-web-status.json /tmp/piratewatch-web-import.err
say "done. The watch answers through Starlab: $state"
say "open /piratewatch on Starlab (same address as /rf). Rollback files: $BACKUP"
