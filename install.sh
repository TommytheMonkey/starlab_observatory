#!/usr/bin/env bash
# Starlab Observatory installer. Safe to run again: every step checks what is already there.
#
#   sudo ./install.sh                 system install: /opt/starlab, /etc/starlab, /var/lib/starlab, service user "starlab"
#   ./install.sh --user               per-user install under ~/.local/share/starlab and ~/.config/starlab
#   sudo ./install.sh --upgrade       upgrade an existing install from this source tree (database backed up first)
#
# Run ./install.sh --help for every option. Nothing records automatically after installing: the setup wizard
# opens in the browser, and recordings start only when you book them or enable automatic recording yourself.
set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE=""; PREFIX=""; CONFIG_DIR=""; DATA_DIR=""; SERVICE_USER=""; BIND=""; PORT=""
NO_SERVICE=0; NO_START=0; UDEV=0; UPGRADE=0; DRY=0; WITH_SLACK=0
MIN_PY="3.12"; MIN_FREE_GB=2; WARN_FREE_GB=10

usage() {
  cat <<'EOF'
Usage: install.sh [options]

  --system              install for the whole machine (default when run as root)
  --user                install for the current user only (default when not root)
  --prefix DIR          application and its Python environment
                        (system: /opt/starlab, user: ~/.local/share/starlab/app)
  --config-dir DIR      starlab.toml and install.env (system: /etc/starlab, user: ~/.config/starlab)
  --data-dir DIR        database, recordings, secrets (system: /var/lib/starlab, user: ~/.local/share/starlab/data)
  --service-user NAME   system installs: the account Starlab runs as (default: starlab; created if missing)
  --bind ADDR           listen address (default 127.0.0.1: this computer only)
  --port N              port (default 8751)
  --no-service          do not install a systemd service (containers, or to run `starlab broker` yourself)
  --no-start            install the service but do not start it
  --udev                system installs: add udev rules so the service user can open common USB SDRs
  --with-slack          also install the optional Slack Socket Mode package (slack_sdk); not needed otherwise
  --upgrade             upgrade an existing installation from this source tree
  --source DIR          the Starlab source tree to install (default: where this script is)
  --yes                 do not ask questions
  --dry-run             only print what would be done
  -h, --help            this text
EOF
}

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m %s\n' "$*"; }
warn() { printf '    \033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then printf '    would run: %s\n' "$*"; else "$@"; fi; }
as_service_user() {   # run a command as the account Starlab runs as
  if [ "$MODE" = system ] && [ "$(id -un)" != "$SERVICE_USER" ]; then
    runuser -u "$SERVICE_USER" -- env STARLAB_CONFIG="$CONFIG_DIR/starlab.toml" "$@"
  else
    env STARLAB_CONFIG="$CONFIG_DIR/starlab.toml" "$@"
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --system) MODE=system ;; --user) MODE=user ;;
    --prefix) PREFIX="$2"; shift ;; --config-dir) CONFIG_DIR="$2"; shift ;; --data-dir) DATA_DIR="$2"; shift ;;
    --service-user) SERVICE_USER="$2"; shift ;; --bind) BIND="$2"; shift ;; --port) PORT="$2"; shift ;;
    --no-service) NO_SERVICE=1 ;; --no-start) NO_START=1 ;; --udev) UDEV=1 ;; --upgrade) UPGRADE=1 ;;
    --with-slack) WITH_SLACK=1 ;;
    --source) SOURCE="$(cd "$2" && pwd)"; shift ;; --yes|-y) ;; --dry-run) DRY=1 ;;   # never prompts; --yes accepted
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option $1" ;;
  esac
  shift
done

[ -n "$MODE" ] || { [ "$(id -u)" = 0 ] && MODE=system || MODE=user; }
if [ "$MODE" = system ]; then
  [ "$(id -u)" = 0 ] || die "a system install needs root (sudo ./install.sh), or use --user"
  : "${CONFIG_DIR:=/etc/starlab}"
else
  [ "$(id -u)" != 0 ] || die "--user installs are for a normal account; as root, use the default system install"
  : "${CONFIG_DIR:=${XDG_CONFIG_HOME:-$HOME/.config}/starlab}"
fi

# An earlier install remembers its choices; options given now win.
if [ -f "$CONFIG_DIR/install.env" ]; then
  # shellcheck disable=SC1091
  . "$CONFIG_DIR/install.env"
  PREFIX="${PREFIX:-$STARLAB_PREFIX}"; DATA_DIR="${DATA_DIR:-$STARLAB_DATA_DIR}"
  SERVICE_USER="${SERVICE_USER:-$STARLAB_SERVICE_USER}"; NO_SERVICE_SAVED="${STARLAB_NO_SERVICE:-0}"
  [ "$NO_SERVICE" = 1 ] || NO_SERVICE="$NO_SERVICE_SAVED"
else
  [ "$UPGRADE" = 0 ] || die "--upgrade: no installation found ($CONFIG_DIR/install.env is missing)"
  if [ -f "$CONFIG_DIR/install.env.removed" ]; then      # uninstalled earlier with data kept: reuse its folders
    # shellcheck disable=SC1091
    . "$CONFIG_DIR/install.env.removed"
    PREFIX="${PREFIX:-$STARLAB_PREFIX}"; DATA_DIR="${DATA_DIR:-$STARLAB_DATA_DIR}"
    SERVICE_USER="${SERVICE_USER:-$STARLAB_SERVICE_USER}"
    echo "    found a previous installation's data in $DATA_DIR; reusing it"
  fi
fi
if [ "$MODE" = system ]; then
  : "${PREFIX:=/opt/starlab}"; : "${DATA_DIR:=/var/lib/starlab}"; : "${SERVICE_USER:=starlab}"
else
  : "${PREFIX:=${XDG_DATA_HOME:-$HOME/.local/share}/starlab/app}"; : "${DATA_DIR:=${XDG_DATA_HOME:-$HOME/.local/share}/starlab/data}"
  SERVICE_USER="$(id -un)"
fi
: "${BIND:=127.0.0.1}"; : "${PORT:=8751}"
VENV="$PREFIX/venv"
CONFIG="$CONFIG_DIR/starlab.toml"

for d in "$PREFIX" "$CONFIG_DIR" "$DATA_DIR"; do
  case "$d" in /*) ;; *) die "paths must be absolute: $d" ;; esac
  case "$d" in /|/usr|/etc|/var|/opt|/home|"$HOME") die "refusing to use $d itself; choose a subdirectory" ;; esac
done

say "Starlab Observatory installer ($MODE install$([ "$UPGRADE" = 1 ] && echo ', upgrade')$([ "$DRY" = 1 ] && echo ', dry run'))"
echo "    source:  $SOURCE"
echo "    app:     $PREFIX     config: $CONFIG_DIR     data: $DATA_DIR     runs as: $SERVICE_USER"
echo "    web:     http://$BIND:$PORT"
[ -f "$SOURCE/pyproject.toml" ] && [ -d "$SOURCE/src/aobs" ] || die "$SOURCE is not a Starlab source tree"

# ------------------------------------------------------------------ 1. system checks
say "Checking this computer"
[ "$(uname -s)" = Linux ] || die "Starlab runs on Linux (this is $(uname -s))"
OS_ID=unknown; OS_NAME="unknown Linux"
if [ -r /etc/os-release ]; then . /etc/os-release; OS_ID="${ID:-unknown}"; OS_NAME="${PRETTY_NAME:-$OS_ID}"; fi
ARCH="$(uname -m)"
case "$OS_ID ${ID_LIKE:-}" in
  *debian*|*ubuntu*|*raspbian*) ok "$OS_NAME ($ARCH)" ;;
  *) warn "$OS_NAME is not a tested distribution (tested: Debian, Ubuntu, Raspberry Pi OS); continuing" ;;
esac
case "$ARCH" in x86_64|aarch64|arm64) ;; armv7l|armv6l) warn "32-bit ARM is untested; numpy/scipy may need to build from source" ;;
  *) warn "architecture $ARCH is untested" ;; esac

PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 is missing. Install it: sudo apt install python3 python3-venv"
PYV="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PY" -c "import sys; sys.exit(0 if sys.version_info[:2] >= tuple(map(int, '$MIN_PY'.split('.'))) else 1)" ||
  die "Python $PYV is too old; Starlab needs $MIN_PY or newer (Ubuntu 24.04, Debian 13 and Raspberry Pi OS 13 have it)"
ok "Python $PYV"
"$PY" -c "import venv, ensurepip" 2>/dev/null || die "Python's venv module is missing. Install it: sudo apt install python3-venv"
ok "python3-venv"

probe_dir="$DATA_DIR"; while [ ! -d "$probe_dir" ]; do probe_dir="$(dirname "$probe_dir")"; done
FREE_GB=$(( $(df -Pk "$probe_dir" | awk 'NR==2 {print $4}') / 1048576 ))
[ "$FREE_GB" -ge "$MIN_FREE_GB" ] || die "only ${FREE_GB} GB free under $probe_dir; Starlab needs at least ${MIN_FREE_GB} GB (recordings need far more)"
[ "$FREE_GB" -ge "$WARN_FREE_GB" ] && ok "${FREE_GB} GB free for data" || warn "only ${FREE_GB} GB free for data; one satellite pass of raw IQ is 1-8 GB"

for tool in rtl_sdr SoapySDRUtil satdump ssh; do
  if command -v "$tool" >/dev/null; then ok "found $tool"; else
    case "$tool" in
      rtl_sdr) warn "rtl_sdr not found: RTL-SDR receivers need it (sudo apt install rtl-sdr)" ;;
      SoapySDRUtil) warn "SoapySDR not found: Airspy, SDRplay and other receivers need it (sudo apt install soapysdr-tools python3-soapysdr, plus the module for your radio, e.g. soapysdr-module-airspy)" ;;
      satdump) warn "SatDump not found: decoding satellite images needs it (see https://www.satdump.org)" ;;
      ssh) warn "OpenSSH client not found: receivers on other computers (Settings → Receiver hosts) need it (sudo apt install openssh-client)" ;;
    esac
  fi
done
echo "    No receiver? Simulation mode in the setup wizard lets you try everything without one."

# ------------------------------------------------------------------ 2. account and folders
say "Account and folders"
if [ "$MODE" = system ]; then
  if id "$SERVICE_USER" >/dev/null 2>&1; then ok "account $SERVICE_USER exists"
  else run useradd --system --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"; ok "created system account $SERVICE_USER"; fi
  for g in plugdev dialout; do
    if getent group "$g" >/dev/null; then
      if id -nG "$SERVICE_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$g"; then ok "$SERVICE_USER is in $g"
      else run usermod -aG "$g" "$SERVICE_USER"; ok "added $SERVICE_USER to $g (USB radio access)"; fi
    fi
  done
fi
run mkdir -p "$PREFIX" "$CONFIG_DIR" "$DATA_DIR" "$DATA_DIR/secrets" "$DATA_DIR/recordings" "$DATA_DIR/db"
if [ "$MODE" = system ]; then
  run chown root:"$SERVICE_USER" "$CONFIG_DIR"; run chmod 0750 "$CONFIG_DIR"
  run chown -R "$SERVICE_USER":"$SERVICE_USER" "$DATA_DIR"
fi
run chmod 0750 "$DATA_DIR"; run chmod 0700 "$DATA_DIR/secrets"
[ "$DRY" = 1 ] || touch "$PREFIX/.starlab-install"     # marks the folder as ours for uninstall
ok "folders ready (secrets folder is private)"

# ------------------------------------------------------------------ 3. application
say "Application"
SRC_VERSION="$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$SOURCE/pyproject.toml" | head -1)"
if command -v git >/dev/null && git -C "$SOURCE" rev-parse --short HEAD >/dev/null 2>&1; then
  SRC_VERSION="$SRC_VERSION+$(git -C "$SOURCE" rev-parse --short HEAD)$(git -C "$SOURCE" diff --quiet 2>/dev/null || echo .dirty)"
fi
OLD_VERSION="$(cat "$PREFIX/VERSION" 2>/dev/null || true)"
if [ ! -x "$VENV/bin/python" ]; then
  run "$PY" -m venv --system-site-packages "$VENV"; ok "Python environment created (system packages such as SoapySDR stay visible)"
fi
if [ "$UPGRADE" = 1 ] || [ "$OLD_VERSION" != "$SRC_VERSION" ] || [ ! -x "$VENV/bin/starlab" ]; then
  if [ "$UPGRADE" = 1 ] && [ "$DRY" = 0 ] && [ -f "$CONFIG" ]; then
    say "Backing up the database before upgrading"
    as_service_user "$VENV/bin/starlab" db backup || die "the database backup failed; nothing was changed"
    if [ "$NO_SERVICE" = 0 ] && command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
      if [ "$MODE" = system ]; then run systemctl stop starlab.service || true; else run systemctl --user stop starlab.service || true; fi
      ok "service stopped for the upgrade"
    fi
  fi
  # build from a private copy: the source folder may be read-only or someone else's, and stays untouched
  BUILD="$(mktemp -d)"; trap 'rm -rf "$BUILD"' EXIT
  tar -C "$SOURCE" --exclude=./.git --exclude=./.venv --exclude=./.dev --exclude='*.egg-info' --exclude=__pycache__ \
      -cf - . | tar -C "$BUILD" -xf -
  run "$VENV/bin/pip" install --quiet --disable-pip-version-check --upgrade "$BUILD" ||
    die "installing Starlab's Python packages failed (see above). Check the network connection, or install python3-numpy python3-scipy python3-skyfield from apt and run this again."
  [ "$DRY" = 1 ] || echo "$SRC_VERSION" > "$PREFIX/VERSION"
  ok "Starlab ${SRC_VERSION} installed${OLD_VERSION:+ (was $OLD_VERSION)}"
else
  ok "Starlab $SRC_VERSION is already installed"
fi
if [ "$WITH_SLACK" = 1 ]; then          # optional: Slack Socket Mode (on every run, also without a version change)
  run "$VENV/bin/pip" install --quiet --disable-pip-version-check "slack_sdk>=3.33" ||
    die "installing the optional Slack package (slack_sdk) failed; check the network connection"
  ok "Slack Socket Mode package installed"
fi

# ------------------------------------------------------------------ 4. configuration and secrets
say "Configuration"
if [ -f "$CONFIG" ]; then
  ok "kept the existing $CONFIG"
elif [ "$DRY" = 1 ]; then
  echo "    would write $CONFIG"
else
  umask 027
  cat > "$CONFIG" <<EOF
# Starlab Observatory bootstrap configuration, written by install.sh on $(date -u +%Y-%m-%d).
# Only what is needed before the database opens lives here; everything else is in the web UI (Settings),
# stored in the database with full history. This file is never overwritten by the installer.

[paths]
data_root = "$DATA_DIR"
# database_url = "sqlite:///$DATA_DIR/db/starlab.sqlite3"   (the default)
# secrets_dir  = "$DATA_DIR/secrets"                          (the default)

[broker]
host = "$BIND"            # 127.0.0.1 = this computer only. To use it from other machines, prefer a VPN or a
port = $PORT                   # reverse proxy with HTTPS over 0.0.0.0.

[scheduler]
allowed_output_roots = ["$DATA_DIR"]
default_output_root = "$DATA_DIR/recordings/jobs"
auto_output_root = "$DATA_DIR/recordings/auto"
EOF
  if [ "$MODE" = system ]; then chown root:"$SERVICE_USER" "$CONFIG"; chmod 0640 "$CONFIG"; fi
  ok "wrote $CONFIG"
fi
if [ -s "$DATA_DIR/secrets/api_token" ]; then ok "kept the existing API token"
elif [ "$DRY" = 0 ]; then
  ( umask 077; "$PY" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$DATA_DIR/secrets/api_token" )
  if [ "$MODE" = system ]; then chown "$SERVICE_USER":"$SERVICE_USER" "$DATA_DIR/secrets/api_token"; fi
  ok "generated an API token in $DATA_DIR/secrets (outside the source tree, readable only by $SERVICE_USER)"
fi
if [ "$DRY" = 0 ]; then
  cat > "$CONFIG_DIR/install.env" <<EOF
# Written by install.sh; read by install.sh (reruns, --upgrade) and uninstall.sh.
STARLAB_MODE=$MODE
STARLAB_PREFIX=$PREFIX
STARLAB_DATA_DIR=$DATA_DIR
STARLAB_SERVICE_USER=$SERVICE_USER
STARLAB_NO_SERVICE=$NO_SERVICE
STARLAB_UDEV=$UDEV
STARLAB_VERSION=$SRC_VERSION
EOF
fi

# ------------------------------------------------------------------ 5. database
say "Database"
if [ "$DRY" = 1 ]; then echo "    would run: starlab db migrate"; else
  as_service_user "$VENV/bin/starlab" db migrate >/dev/null || die "creating/upgrading the database failed (see above)"
  ok "$(as_service_user "$VENV/bin/starlab" db status | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print("schema", d["schema_version"], "-", d["state"], "-", d["database"])')"
fi

# ------------------------------------------------------------------ 6. udev rules (optional)
if [ "$UDEV" = 1 ]; then
  say "USB permissions (udev)"
  [ "$MODE" = system ] || die "--udev needs a system install"
  RULES=/etc/udev/rules.d/60-starlab-sdr.rules
  if [ "$DRY" = 0 ]; then
    cat > "$RULES.tmp" <<'EOF'
# Installed by Starlab's install.sh: lets members of plugdev open these USB SDRs.
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1df7", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="60a1", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="03eb", ATTRS{idProduct}=="800c", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="6089", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0456", ATTRS{idProduct}=="b673", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="6108", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="601f", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="2500", MODE="0660", GROUP="plugdev"
EOF
    if cmp -s "$RULES.tmp" "$RULES"; then rm "$RULES.tmp"; ok "udev rules already installed"
    else mv "$RULES.tmp" "$RULES"; command -v udevadm >/dev/null && udevadm control --reload && udevadm trigger --subsystem-match=usb || true
      ok "installed $RULES (replug receivers once)"; fi
  fi
  echo "    RTL-SDR: if the TV-tuner driver grabs the dongle, blacklist it: echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/starlab-rtl.conf"
fi

# ------------------------------------------------------------------ 7. service
START_CMD="STARLAB_CONFIG=$CONFIG $VENV/bin/starlab broker"
if [ "$NO_SERVICE" = 1 ]; then
  say "Service"; ok "skipped (--no-service). Start Starlab with:  $START_CMD"
elif ! command -v systemctl >/dev/null || [ ! -d /run/systemd/system ]; then
  say "Service"; warn "systemd is not running here, so no service was installed. Start Starlab with:  $START_CMD"
  NO_SERVICE=1
else
  say "Service"
  if [ "$MODE" = system ]; then UNIT=/etc/systemd/system/starlab.service; SCTL="systemctl"
  else UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/starlab.service"; SCTL="systemctl --user"; mkdir -p "$(dirname "$UNIT")"; fi
  UNIT_TMP="$(mktemp)"
  {
    echo "[Unit]"
    echo "Description=Starlab Observatory"
    echo "After=network-online.target"
    echo "Wants=network-online.target"
    echo
    echo "[Service]"
    [ "$MODE" = system ] && echo "User=$SERVICE_USER" && echo "Group=$SERVICE_USER"
    echo "Environment=STARLAB_CONFIG=$CONFIG"
    echo "Environment=PYTHONUNBUFFERED=1"
    echo "ExecStart=$VENV/bin/starlab broker"
    echo "Restart=on-failure"
    echo "RestartSec=5"
    if [ "$MODE" = system ]; then
      echo "NoNewPrivileges=yes"
      echo "ProtectSystem=strict"
      echo "ProtectHome=yes"
      echo "PrivateTmp=yes"
      echo "ReadWritePaths=$DATA_DIR"
    fi
    echo
    echo "[Install]"
    [ "$MODE" = system ] && echo "WantedBy=multi-user.target" || echo "WantedBy=default.target"
  } > "$UNIT_TMP"
  if cmp -s "$UNIT_TMP" "$UNIT"; then rm "$UNIT_TMP"; ok "service definition unchanged ($UNIT)"
  else run install -m 0644 "$UNIT_TMP" "$UNIT"; rm -f "$UNIT_TMP"; run $SCTL daemon-reload; ok "installed $UNIT"; fi
  run $SCTL enable starlab.service >/dev/null 2>&1 && ok "enabled at boot"
  if [ "$MODE" = user ]; then echo "    For a user service to run without you logged in: sudo loginctl enable-linger $(id -un)"; fi
  if [ "$NO_START" = 1 ]; then ok "not started (--no-start):  $SCTL start starlab"
  else run $SCTL restart starlab.service; ok "started"; fi
fi

# ------------------------------------------------------------------ 8. done
if [ "$DRY" = 0 ] && [ "$NO_SERVICE" = 0 ] && [ "$NO_START" = 0 ]; then
  say "Waiting for Starlab to answer"
  for _ in $(seq 1 30); do
    if "$PY" -c "import urllib.request,sys; urllib.request.urlopen('http://$BIND:$PORT/setup', timeout=2)" 2>/dev/null; then ok "answering on port $PORT"; break; fi
    sleep 1
  done
fi
say "Done"
echo "    Open:      http://$BIND:$PORT/   (from this computer$([ "$BIND" != 127.0.0.1 ] && echo ' or the network'))"
if [ "$DRY" = 0 ]; then
  LINK="$(as_service_user "$VENV/bin/starlab" web-link --base-url "http://$([ "$BIND" = 0.0.0.0 ] && hostname || echo "$BIND"):$PORT" 2>/dev/null || true)"
  if [ -n "$LINK" ]; then echo "    Sign in:   $LINK"; echo "               (valid 15 minutes; get a new one any time with: STARLAB_CONFIG=$CONFIG $VENV/bin/starlab web-link)"; fi
fi
echo "    The setup wizard opens first. Nothing records until you book a recording or enable automatic recording."
echo "    Config: $CONFIG   Data: $DATA_DIR   Uninstall: $SOURCE/uninstall.sh$([ "$MODE" = user ] && echo ' --user')"
