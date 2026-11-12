#!/usr/bin/env bash
# Rotate the Starlab broker API token (the bearer token for /v1/lease, /v1/state, the pirate watch's control
# endpoint and the Slack agent). Web sign-ins use a separate key and are not affected.
#
#   sudo /home/tommy/starlab-dev/deploy/rotate-broker-token.sh
#
# Waits (up to MAX_WAIT_MIN, default 30) until no scheduled capture runs and the pirate watch is not recording,
# then: writes a new token to Starlab's token file and to ~tommy/.config/observatory/broker.env (the watch's copy),
# restarts Starlab and the watch, checks the new token works and the old one is refused, and prints what to change
# on monkeybrain. The old token is kept next to the token file (mode 600) until you delete it.
# When this checkout is newer than the installed Starlab, the restart is an upgrade (database backup first), so
# one quiet window covers both (2026-11-12: the switch port check, the test form's per-port gain).
set -euo pipefail
DEV=/home/tommy/starlab-dev
STATUS=/data/archive/pirate-watch/status.json
WATCH_COPY=/home/tommy/.config/observatory/broker.env
MAX_WAIT_MIN=${MAX_WAIT_MIN:-30}
say() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { say "STOPPED: $*"; exit 1; }
[ "$(id -u)" = 0 ] || die "run it with sudo"

TOKEN_FILE=$(/opt/starlab/venv/bin/python3 -c 'from aobs.config import load_config; print(load_config("/etc/starlab/starlab.toml").section("broker")["token_file"])')
[ -s "$TOKEN_FILE" ] || die "Starlab's token file $TOKEN_FILE is missing or empty"
say "Starlab's token file: $TOKEN_FILE"

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

OLD=$(tr -d '[:space:]' < "$TOKEN_FILE")
NEW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
STAMP=$(date +%Y%m%d-%H%M%S)
umask 077
cp -p "$TOKEN_FILE" "$TOKEN_FILE.old-$STAMP"
owner=$(stat -c '%U:%G' "$TOKEN_FILE")
printf '%s\n' "$NEW" > "$TOKEN_FILE.new" && chown "$owner" "$TOKEN_FILE.new" && chmod 600 "$TOKEN_FILE.new"
mv "$TOKEN_FILE.new" "$TOKEN_FILE"
printf '%s\n' "$NEW" > "$WATCH_COPY.new" && chown tommy:tommy "$WATCH_COPY.new" && chmod 600 "$WATCH_COPY.new"
mv "$WATCH_COPY.new" "$WATCH_COPY"
say "new token written (old one kept as $TOKEN_FILE.old-$STAMP)"

installed=$(sed -E 's/^.*\+([0-9a-f]+).*$/\1/' /opt/starlab/VERSION)
head=$(git -C "$DEV" rev-parse --short HEAD)
if [ "$installed" != "$head" ] && git -C "$DEV" merge-base --is-ancestor "$installed" "$head" 2>/dev/null; then
  say "upgrading Starlab $installed -> $head ($(git -C "$DEV" log --oneline -1))"
  bash "$DEV/install.sh" --upgrade --yes
else
  systemctl restart starlab
fi
for _ in $(seq 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8751/v1/rf || true)
  [ "$code" = 401 ] || [ "$code" = 200 ] && break
  sleep 2
done
systemctl is-active --quiet starlab || die "Starlab did not come back: journalctl -u starlab -n 80"
new_code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $NEW" http://127.0.0.1:8751/v1/state)
old_code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OLD" http://127.0.0.1:8751/v1/state)
[ "$new_code" = 200 ] || die "the new token is refused (HTTP $new_code); restore with: cp -p $TOKEN_FILE.old-$STAMP $TOKEN_FILE && systemctl restart starlab"
[ "$old_code" = 401 ] || die "the old token still works (HTTP $old_code)"
say "Starlab restarted: new token accepted, old token refused"

systemctl restart starlab-pirate-watch
for _ in $(seq 40); do
  held=$(python3 -c "import json;print(json.load(open('$STATUS')).get('lease_held'))" 2>/dev/null || true)
  [ "$held" = True ] && break
  sleep 3
done
[ "$held" = True ] && say "pirate watch restarted and holds its lease" || say "pirate watch restarted but has no lease yet: journalctl -u starlab-pirate-watch -n 30"

cat <<EOF

=== monkeybrain (the Slack agent) ===
Until this is done the agent's broker calls fail with HTTP 401 (it can't lease R05 or see the station state).
The agent reads BROKER_TOKEN from ~/.config/observatory/broker.env on monkeybrain (agent/obsagent/agent.py).
On monkeybrain, run (it fetches the new token from observatory over SSH, so it never shows on screen):

  NEW=\$(ssh tommy@observatory 'head -1 ~/.config/observatory/broker.env') && \\
  sed -i "s|^BROKER_TOKEN=.*|BROKER_TOKEN=\$NEW|" ~/.config/observatory/broker.env && \\
  grep -c '^BROKER_TOKEN=' ~/.config/observatory/broker.env && systemctl --user restart obsagent

If monkeybrain can't SSH to observatory, show the token here with  sudo cat $WATCH_COPY  and put it in
BROKER_TOKEN=... in that file by hand, then  systemctl --user restart obsagent.
Once the agent works, delete the old token:  sudo rm $TOKEN_FILE.old-$STAMP
EOF
