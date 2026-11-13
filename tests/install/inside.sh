#!/usr/bin/env bash
# Runs inside the test container (see container-test.sh).
set -euo pipefail
step() { printf '\n\033[1m######## %s\033[0m\n' "$*"; }
fail() { echo "TEST FAILED: $*"; exit 1; }
[ ! -e /dev/bus/usb ] || fail "the container can see USB devices; refusing to test here"
export DEBIAN_FRONTEND=noninteractive
step "clean system: install only python3 + venv"
apt-get update -qq >/dev/null && apt-get install -y -qq python3 python3-venv ca-certificates git >/dev/null
mkdir -p /src && tar -C /src -xzf /work/starlab.tgz
cd /src
[ ! -e /etc/starlab ] && [ ! -e /var/lib/starlab ] || fail "not a clean system"

start() {  # start Starlab as its service user (no systemd in the container)
  runuser -u starlab -- env STARLAB_CONFIG=/etc/starlab/starlab.toml /opt/starlab/venv/bin/starlab broker >/tmp/broker.log 2>&1 &
  for _ in $(seq 1 60); do python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8751/setup', timeout=2)" 2>/dev/null && return 0; sleep 1; done
  cat /tmp/broker.log; fail "Starlab did not start"
}
stop() { pkill -f "/opt/starlab/venv/bin/starlab broker" || true; sleep 2; }
api_bearer() { python3 - "$1" <<'PY'
import json, sys, urllib.request
tok = open("/srv/starlab-data/secrets/api_token").read().strip()
r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8751" + sys.argv[1], headers={"Authorization": "Bearer " + tok}), timeout=30)
print(json.dumps(json.load(r)))
PY
}

step "1. fresh system install (custom data folder, no systemd)"
./install.sh --system --data-dir /srv/starlab-data --yes 2>&1 | tee /tmp/install1.log
grep -q "systemd is not running here" /tmp/install1.log || fail "expected the no-systemd path"
LINK="$(grep -o 'http://[^ ]*/rf/login/[^ ]*' /tmp/install1.log | head -1)"
[ -n "$LINK" ] || fail "the installer printed no sign-in link"
LINK="http://127.0.0.1:8751/rf/${LINK#*/rf/}"          # the same signed link, through this computer's address
grep -q '^host = "0.0.0.0"' /etc/starlab/starlab.toml || fail "default access should listen on the network"
grep -q '^allowed_clients = \["private"\]' /etc/starlab/starlab.toml || fail "no private-network client rule"
grep -q "Tailscale is not installed" /tmp/install1.log || fail "no Tailscale advice without Tailscale"
grep -q "until setup is finished" /tmp/install1.log || fail "the first-run link should last until setup is finished"
[ -x /usr/local/bin/starlab ] || fail "no starlab command"
/usr/local/bin/starlab web-link --first | grep -q '/rf/login/' || fail "starlab web-link (short command) failed"
test "$(stat -c %a /srv/starlab-data/secrets)" = 700 || fail "secrets folder is not private"
test "$(stat -c %a /srv/starlab-data/secrets/api_token)" = 600 || fail "API token is not private"
test "$(stat -c %U /srv/starlab-data/secrets/api_token)" = starlab || fail "API token is not owned by the service user"
grep -q 'data_root = "/srv/starlab-data"' /etc/starlab/starlab.toml || fail "config has the wrong data folder"
! grep -rqiE "tommy|/home/tommy|observatory-sdr|OBS-R0" /etc/starlab || fail "station-specific values leaked into a fresh config"
echo "ok: config, secrets and permissions"

step "2. first-run wizard, simulation mode, predictions, booking (HTTP, as a browser would)"
start
python3 /work/e2e.py http://127.0.0.1:8751 "$LINK"
code="$(python3 -c "import urllib.request,urllib.error,sys
try: urllib.request.urlopen(sys.argv[1], timeout=5); print(200)
except urllib.error.HTTPError as e: print(e.code)" "$LINK")"
[ "$code" = 403 ] || fail "the first-run link still works after setup finished (HTTP $code)"
/usr/local/bin/starlab web-link | grep -q "Valid 15 minutes" || fail "after setup, links should be 15-minute links"
echo "ok: first-run link retired once setup finished"
stop

step "3. rerun the installer: nothing changes"
sum_before="$(sha256sum /etc/starlab/starlab.toml /srv/starlab-data/secrets/api_token)"
./install.sh --system --yes > /tmp/install2.log 2>&1 || { cat /tmp/install2.log; fail "rerun failed"; }
grep -q "is already installed" /tmp/install2.log || fail "rerun reinstalled the application"
grep -q "kept the existing /etc/starlab/starlab.toml" /tmp/install2.log || fail "rerun did not keep the config"
[ "$sum_before" = "$(sha256sum /etc/starlab/starlab.toml /srv/starlab-data/secrets/api_token)" ] || fail "rerun changed config or token"
start
api_bearer /v1/setup | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["completed"], d; print("ok: setup still completed after rerun")'
stop

step "3b. rerun with --access local changes only the listen address"
./install.sh --system --access local --yes > /tmp/install2b.log 2>&1 || { cat /tmp/install2b.log; fail "rerun --access local failed"; }
grep -q '^host = "127.0.0.1"' /etc/starlab/starlab.toml || fail "--access local did not change the listen address"
compgen -G "/etc/starlab/starlab.toml.bak-*" >/dev/null || fail "no backup of starlab.toml before changing it"
grep -q 'data_root = "/srv/starlab-data"' /etc/starlab/starlab.toml || fail "--access changed more than the address"
./install.sh --system --access network --yes > /tmp/install2c.log 2>&1 || fail "rerun --access network failed"
grep -q '^host = "0.0.0.0"' /etc/starlab/starlab.toml || fail "--access network did not change the listen address back"
echo "ok: --access switches the listen address and keeps a backup"

step "4. upgrade (database backed up first)"
sed -i 's/^version = "\(.*\)"/version = "\1.post1"/' pyproject.toml
./install.sh --upgrade --yes > /tmp/install3.log 2>&1 || { cat /tmp/install3.log; fail "upgrade failed"; }
grep -q "Backing up the database before upgrading" /tmp/install3.log || fail "no backup before upgrading"
compgen -G "/srv/starlab-data/db/backups/*manual*" >/dev/null || fail "no backup file"
grep -q "(was " /tmp/install3.log || fail "upgrade did not report the previous version"
start
api_bearer /v1/inventory | python3 -c 'import json,sys; d=json.load(sys.stdin); r={x["id"]: x["state"] for x in d["receivers"]}; assert r == {"SIM-A": "simulated"}, r; print("ok: receivers survived the upgrade", r)'
stop

step "5. uninstall, keeping data"
./uninstall.sh --yes > /tmp/un1.log 2>&1 || { cat /tmp/un1.log; fail "uninstall failed"; }
[ ! -e /opt/starlab ] || fail "application not removed"
[ ! -e /usr/local/bin/starlab ] || fail "starlab command not removed"
[ -s /srv/starlab-data/db/starlab.sqlite3 ] && [ -s /srv/starlab-data/secrets/api_token ] || fail "data was not kept"
[ -f /etc/starlab/starlab.toml ] || fail "config was not kept"
echo "ok: application removed, data and config kept"

step "6. reinstall picks the kept data up again"
./install.sh --system --yes > /tmp/install4.log 2>&1 || { cat /tmp/install4.log; fail "reinstall failed"; }
grep -q "reusing it" /tmp/install4.log || fail "reinstall did not find the kept data"
start
api_bearer /v1/setup | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["completed"], d; print("ok: wizard state survived uninstall + reinstall")'
stop

step "7. uninstall --purge deletes data only when confirmed"
echo "wrong answer" | ./uninstall.sh --purge > /tmp/un2.log 2>&1 && fail "purge went ahead without confirmation" || true
[ -d /srv/starlab-data ] || fail "purge deleted data without confirmation"
./install.sh --system --yes >/dev/null 2>&1
echo "/srv/starlab-data" | ./uninstall.sh --purge > /tmp/un3.log 2>&1 || { cat /tmp/un3.log; fail "purge failed"; }
[ ! -e /srv/starlab-data ] && [ ! -e /etc/starlab ] && [ ! -e /opt/starlab ] || fail "purge left files behind"
echo "ok: purge needs the typed path, then removes everything"

step "8. per-user install as another account (no root, nothing under /etc or /opt)"
useradd -m alice
runuser -u alice -- bash -c 'cd /src && ./install.sh --user --port 8761 > /tmp/alice.log 2>&1' || { cat /tmp/alice.log; fail "user install failed"; }
runuser -u alice -- test -x /home/alice/.local/share/starlab/app/venv/bin/starlab || fail "user install: no app"
runuser -u alice -- test -s /home/alice/.local/share/starlab/data/db/starlab.sqlite3 || fail "user install: no database"
runuser -u alice -- test -f /home/alice/.config/starlab/starlab.toml || fail "user install: no config"
[ ! -e /etc/starlab ] && [ ! -e /opt/starlab ] || fail "user install wrote system folders"
runuser -u alice -- bash -c 'cd /src && ./uninstall.sh --user >/dev/null' || fail "user uninstall failed"
echo "ok: per-user install and uninstall"

printf '\n\033[1;32mALL INSTALLATION TESTS PASSED\033[0m\n'
