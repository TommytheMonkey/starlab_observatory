#!/usr/bin/env bash
# Upgrade the installed Starlab to this checkout's committed HEAD, the way rotate-broker-token.sh does it (a clean
# worktree of HEAD, install.sh --upgrade, database backup first), without rotating the broker token.
#
#   sudo /home/tommy/starlab-dev/deploy/upgrade-from-head.sh
#
# Refuses to start while a scheduled capture runs, while the pirate watch is recording, or when a booked recording's
# reservation starts within GUARD_MIN minutes (default 20). Waits up to MAX_WAIT_MIN (default 30) for a quiet moment.
# Never installs uncommitted edits. The pirate watch runs from ~/starlab-dev/.venv and is not restarted.
set -euo pipefail
DEV=/home/tommy/starlab-dev
STATUS=/data/archive/pirate-watch/status.json
MAX_WAIT_MIN=${MAX_WAIT_MIN:-30}
GUARD_MIN=${GUARD_MIN:-20}
say() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { say "STOPPED: $*"; exit 1; }
[ "$(id -u)" = 0 ] || die "run it with sudo"

TOKEN_FILE=$(/opt/starlab/venv/bin/python3 -c 'from aobs.config import load_config; print(load_config("/etc/starlab/starlab.toml").section("broker")["token_file"])')
installed=$(sed -E 's/^.*\+([0-9a-f]+).*$/\1/' /opt/starlab/VERSION)
head=$(git -C "$DEV" rev-parse --short HEAD)
[ "$installed" != "$head" ] || die "Starlab $installed is already installed; nothing to do"
git -C "$DEV" merge-base --is-ancestor "$installed" "$head" 2>/dev/null \
  || die "installed $installed is not an ancestor of HEAD $head; refusing"
say "will upgrade $installed -> $head:"
git -C "$DEV" log --oneline "$installed..$head" | sed 's/^/      /'

quiet() {
  /opt/starlab/venv/bin/python3 - "$STATUS" "$TOKEN_FILE" "$GUARD_MIN" <<'PY'
import datetime as dt, json, subprocess, sys, urllib.request
why = []
now = dt.datetime.now(dt.timezone.utc)
try:
    s = json.load(open(sys.argv[1]))
    age = (now - dt.datetime.fromisoformat(s["at"].replace("Z", "+00:00"))).total_seconds()
    if s.get("state") == "recording" and age < 120:
        why.append(f"the watch is recording ({s.get('detail')})")
except (OSError, ValueError, KeyError):
    pass
if any("aobs.scheduling.capture" in l for l in subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout.splitlines()):
    why.append("a scheduled capture is running")
try:
    tok = open(sys.argv[2]).read().strip()
    req = urllib.request.Request("http://127.0.0.1:8751/v1/jobs?hours_back=1&hours_ahead=2",
                                 headers={"Authorization": f"Bearer {tok}"})
    jobs = json.load(urllib.request.urlopen(req, timeout=10))
    jobs = jobs.get("jobs", jobs) if isinstance(jobs, dict) else jobs
    for j in jobs:
        if j.get("status") not in ("scheduled", "preparing", "recording"):
            continue
        a = dt.datetime.fromisoformat(str(j["reservation_start"]).replace("Z", "+00:00"))
        b = dt.datetime.fromisoformat(str(j["reservation_end"]).replace("Z", "+00:00"))
        a, b = (x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc) for x in (a, b))
        if a - dt.timedelta(minutes=int(sys.argv[3])) < now < b:
            why.append(f"{j['job_name']} on {j['receiver_id']} is reserved from {a:%H:%M} UTC")
except Exception as exc:  # noqa: BLE001 - cannot see the schedule: not quiet
    why.append(f"cannot read the schedule ({exc})")
print("; ".join(why))
PY
}
deadline=$(( $(date +%s) + MAX_WAIT_MIN * 60 )); last=""
while why=$(quiet) && [ -n "$why" ]; do
  [ "$why" != "$last" ] && say "waiting: $why"; last=$why
  [ "$(date +%s)" -lt "$deadline" ] || die "no quiet moment in $MAX_WAIT_MIN min; nothing changed"
  sleep 5
done

CLEAN=$(mktemp -d /tmp/starlab-deploy-XXXXXX); chown tommy:tommy "$CLEAN"; rmdir "$CLEAN"
sudo -u tommy git -C "$DEV" worktree add --quiet --detach "$CLEAN" "$head"
trap 'sudo -u tommy git -C "$DEV" worktree remove --force "$CLEAN" 2>/dev/null || true' EXIT
say "upgrading Starlab $installed -> $head ($(git -C "$DEV" log --oneline -1 "$head")), from a clean checkout"
bash "$CLEAN/install.sh" --upgrade --yes

for _ in $(seq 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8751/v1/rf || true)
  [ "$code" = 401 ] || [ "$code" = 200 ] && break
  sleep 2
done
systemctl is-active --quiet starlab || die "Starlab did not come back: journalctl -u starlab -n 80"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $(tr -d '[:space:]' < "$TOKEN_FILE")" http://127.0.0.1:8751/v1/state)
[ "$code" = 200 ] || die "Starlab is up but refuses its own token (HTTP $code): journalctl -u starlab -n 80"
say "Starlab $(cat /opt/starlab/VERSION) is running; the broker token is unchanged"
