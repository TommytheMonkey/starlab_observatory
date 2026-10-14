#!/bin/sh
# Undo apply.sh: METEOR back to OBS-R01 (helix), OBS-R02 back to ISS at 145 MHz.
set -e
D=/home/tommy/auto-observe/deploy/temp-r02
install -m 644 -o root -g root $D/recorder.config.orig.json /etc/observatory-recorder/config.json
install -m 644 -o root -g root $D/sdr.config.orig.json /etc/observatory-sdr/config.json
systemctl restart observatory-record@OBS-R02.service observatory-record@OBS-R01.service
echo "Original receiver assignment restored"
