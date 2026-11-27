#!/usr/bin/env bash
# Rebuild the Stratolink SenseCAP M2 US915 firmware from pinned sources, without menuconfig.
#
#   deploy/stratolink/firmware/build.sh                      # offline, from the archived source mirror
#   ONLINE=1 deploy/stratolink/firmware/build.sh             # fetch the tree and feeds from upstream instead
#   WORK=~/fwbuild CPUS=4 JOBS=4 deploy/stratolink/firmware/build.sh
#
# Steps (all inside the container, as an unprivileged user): unpack Seeed LoRa_Gateway_OpenWRT at 79a1b57, apply the
# three Stratolink patches (Python 3.10 host check, m4 SIGSTKSZ, SX1302 sync word 0x2B), install the four feeds at the
# commits the release used, `make defconfig` from diffconfig, build. Images land in $WORK/out with SHA256SUMS.
#
# The result is functionally the same firmware but NOT bit-identical to the archived image: OpenWrt 21.02 images
# embed a per-build signing key (key-build) and build timestamps. Compare `opkg list-installed`/the manifest, and
# the lora_pkt_fwd binary, not the image hash. The archived release stays the one that was verified on a gateway.
#
# Runs at nice 19 with a CPU cap (CPUS, default 4 of 20) so recordings on this computer keep their CPU.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=${WORK:-$HOME/stratolink-firmware-build}
MIRROR=${MIRROR:-/data/archive/stratolink/firmware/source-mirror}
CPUS=${CPUS:-4}
JOBS=${JOBS:-$CPUS}
ONLINE=${ONLINE:-0}
IMAGE=stratolink-openwrt-builder:21.02

mkdir -p "$WORK/out"
if [ "$ONLINE" != 1 ] && [ ! -d "$MIRROR/dl" ]; then
  echo "no source mirror at $MIRROR (set MIRROR=, or ONLINE=1 to download from upstream)" >&2
  exit 1
fi
nice -n 19 docker build -q -t "$IMAGE" --build-arg UID="$(id -u)" "$HERE" >/dev/null
echo "building in $WORK (log: $WORK/build.log), $CPUS CPUs, make -j$JOBS"
exec nice -n 19 docker run --rm --cpus="$CPUS" --memory=12g \
  -v "$WORK:/work" -v "$HERE:/recipe:ro" $( [ -d "$MIRROR" ] && echo "-v $MIRROR:/mirror:ro" ) \
  -e JOBS="$JOBS" -e ONLINE="$ONLINE" "$IMAGE" bash /recipe/inside.sh
