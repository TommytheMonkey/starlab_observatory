#!/bin/sh
# One-time root setup so the broker can run ADS-B part-time (user OK 2026-10-12).
#  1. sudoers: tommy may start/stop/restart observatory-adsb.service without a password (nothing else).
#  2. systemd drop-in: dump1090 takes its --device-index from /run/observatory-sdr/adsb.env,
#     which the broker writes from the live librtlsdr index of OBS-R05. Defaults to 0 (today's value).
# Does not restart ADS-B; the drop-in takes effect the next time it starts.
# Undo: rm /etc/sudoers.d/aobs-adsb /etc/systemd/system/observatory-adsb.service.d/device-index.conf && systemctl daemon-reload
set -e

SUDOERS=/etc/sudoers.d/aobs-adsb
TMP=$(mktemp)
cat > "$TMP" <<'EOF'
tommy ALL=(root) NOPASSWD: /usr/bin/systemctl start observatory-adsb.service, /usr/bin/systemctl stop observatory-adsb.service, /usr/bin/systemctl restart observatory-adsb.service
EOF
visudo -cf "$TMP"
install -m 440 -o root -g root "$TMP" "$SUDOERS"
rm -f "$TMP"

DROPIN=/etc/systemd/system/observatory-adsb.service.d
install -d -m 755 "$DROPIN"
cat > "$DROPIN/device-index.conf" <<'EOF'
[Service]
Environment=ADSB_DEVICE_INDEX=0
EnvironmentFile=-/run/observatory-sdr/adsb.env
ExecStart=
ExecStart=/usr/local/sbin/dump1090 --device-index ${ADSB_DEVICE_INDEX} --gain 40.2 --net --write-json /var/lib/observatory-adsb --write-json-every 1 --quiet
EOF
systemctl daemon-reload
systemctl cat observatory-adsb.service | grep -E 'ExecStart=/|EnvironmentFile'
echo "Done: broker may now pause/resume ADS-B"
