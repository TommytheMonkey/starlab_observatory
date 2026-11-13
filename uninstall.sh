#!/usr/bin/env bash
# Remove Starlab Observatory. By default only the application and its service are removed: the
# configuration, database, recordings and secrets stay, so reinstalling picks up where you left off.
#
#   sudo ./uninstall.sh                    remove the system install, keep data and config
#   ./uninstall.sh --user                  the same for a per-user install
#   sudo ./uninstall.sh --purge            ALSO delete config, database, recordings and secrets (asks to confirm)
set -euo pipefail

MODE=""; CONFIG_DIR=""; PURGE=0; YES=0
usage() {
  cat <<'EOF'
Usage: uninstall.sh [--system|--user] [--config-dir DIR] [--purge] [--yes]

  --purge       also delete the configuration, database, recordings and secrets. You are asked to type the data
                folder's path to confirm (or pass --yes together with --purge).
  --config-dir  where install.sh put starlab.toml and install.env (default /etc/starlab or ~/.config/starlab)
EOF
}
say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --system) MODE=system ;; --user) MODE=user ;; --config-dir) CONFIG_DIR="$2"; shift ;;
    --purge) PURGE=1 ;; --yes|-y) YES=1 ;; -h|--help) usage; exit 0 ;; *) usage >&2; die "unknown option $1" ;;
  esac
  shift
done
[ -n "$MODE" ] || { [ "$(id -u)" = 0 ] && MODE=system || MODE=user; }
[ "$MODE" = user ] || [ "$(id -u)" = 0 ] || die "a system install is removed as root (sudo ./uninstall.sh)"
: "${CONFIG_DIR:=$([ "$MODE" = system ] && echo /etc/starlab || echo "${XDG_CONFIG_HOME:-$HOME/.config}/starlab")}"
ENV="$CONFIG_DIR/install.env"
if [ ! -f "$ENV" ] && [ -f "$ENV.removed" ] && [ "$PURGE" = 1 ]; then ENV="$ENV.removed"   # purge after a keep-data uninstall
elif [ ! -f "$ENV" ]; then die "no Starlab installation found ($ENV is missing)"; fi
# shellcheck disable=SC1090
. "$ENV"
PREFIX="$STARLAB_PREFIX"; DATA_DIR="$STARLAB_DATA_DIR"

say "Removing Starlab Observatory ($MODE install)"
if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
  if [ "$MODE" = system ]; then UNIT=/etc/systemd/system/starlab.service; SCTL="systemctl"
  else UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/starlab.service"; SCTL="systemctl --user"; fi
  if [ -f "$UNIT" ]; then
    $SCTL disable --now starlab.service >/dev/null 2>&1 || true
    rm -f "$UNIT"; $SCTL daemon-reload; ok "service stopped and removed"
  fi
else
  pkill -f "$PREFIX/venv/bin/starlab broker" 2>/dev/null && ok "stopped the running Starlab" || true
fi
if [ "${STARLAB_UDEV:-0}" = 1 ] && [ -f /etc/udev/rules.d/60-starlab-sdr.rules ]; then
  rm -f /etc/udev/rules.d/60-starlab-sdr.rules; command -v udevadm >/dev/null && udevadm control --reload || true
  ok "udev rules removed"
fi
if [ "$MODE" = system ]; then CMD_LINK=/usr/local/bin/starlab; else CMD_LINK="$HOME/.local/bin/starlab"; fi
if [ -f "$CMD_LINK" ] && grep -q 'installed by Starlab' "$CMD_LINK"; then rm -f "$CMD_LINK"; ok "removed the starlab command"; fi
if [ "${STARLAB_TAILSCALE_HTTPS:-0}" = 1 ] && command -v tailscale >/dev/null; then
  tailscale serve --https=443 off >/dev/null 2>&1 && ok "stopped publishing Starlab through Tailscale HTTPS" ||
    echo "    could not turn off Tailscale Serve; check with: tailscale serve status"
fi
if [ -f "$PREFIX/.starlab-install" ]; then
  rm -rf -- "$PREFIX"; ok "application removed ($PREFIX)"
elif [ -d "$PREFIX" ]; then
  die "$PREFIX has no .starlab-install marker; not deleting it. Remove it by hand if it is Starlab's."
fi

if [ "$PURGE" = 1 ]; then
  echo "    This deletes $DATA_DIR (database, recordings, secrets) and $CONFIG_DIR. It cannot be undone."
  if [ "$YES" != 1 ]; then
    printf '    Type the data folder path to confirm: '
    read -r answer
    [ "$answer" = "$DATA_DIR" ] || die "not confirmed; nothing more was deleted"
  fi
  case "$DATA_DIR" in /|/usr|/etc|/var|/opt|/home|"$HOME"|"") die "refusing to delete $DATA_DIR" ;; esac
  rm -rf -- "$DATA_DIR" "$CONFIG_DIR"; ok "data and configuration deleted"
  if [ "$MODE" = system ]; then echo "    The account ${STARLAB_SERVICE_USER} was kept; remove it with: sudo userdel ${STARLAB_SERVICE_USER}"; fi
else
  mv "$ENV" "$CONFIG_DIR/install.env.removed"
  ok "kept your data: $DATA_DIR (database, recordings, secrets) and configuration: $CONFIG_DIR"
  echo "    Reinstalling with install.sh uses them again. To delete them as well: uninstall.sh --purge"
fi
