#!/bin/sh
# TEMP (2026-10-12): record METEOR M2-3/M2-4 on OBS-R02 (discone) while the helix on OBS-R01 is disconnected.
set -e
D=/home/tommy/auto-observe/deploy/temp-r02
install -m 644 -o root -g root $D/recorder.config.r02.json /etc/observatory-recorder/config.json
install -m 644 -o root -g root $D/sdr.config.r02.json /etc/observatory-sdr/config.json
systemctl restart observatory-record@OBS-R02.service
echo "OBS-R02 now records METEOR at 137.5 MHz"
