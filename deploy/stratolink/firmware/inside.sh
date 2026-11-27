#!/usr/bin/env bash
# Runs inside the build container (see build.sh). /recipe = this directory, /mirror = archived sources, /work = output.
set -euo pipefail
TREE_COMMIT=79a1b578039fa178c5a884e1f53329737539ceb9
declare -A FEEDS=(
  [packages]="https://git.openwrt.org/feed/packages.git 65057dcbb5de371503c9159de3d45824bec482e0"
  [luci]="https://git.openwrt.org/project/luci.git 3b3c2e5f9f82372df8ff01ac65668be47690dcd5"
  [routing]="https://git.openwrt.org/feed/routing.git c30c9ffc93702365439a7647244a052531f2e957"
  [telephony]="https://git.openwrt.org/feed/telephony.git 7f73a9ad19269dcddcb7fc26e03a9823717587bb"
)
cd /work
exec > >(tee -a /work/build.log) 2>&1
echo "== $(date -u +%FT%TZ) Stratolink firmware build (ONLINE=$ONLINE, JOBS=$JOBS)"

if [ ! -d src ]; then
  if [ "$ONLINE" = 1 ]; then
    git clone https://github.com/Seeed-Solution/LoRa_Gateway_OpenWRT.git src
    git -C src checkout -q "$TREE_COMMIT"
  else
    tar xzf /mirror/git/LoRa_Gateway_OpenWRT-79a1b578.tar.gz && mv LoRa_Gateway_OpenWRT src
  fi
fi
cd src

apply() {   # idempotent: skip a patch that is already in
  local dir=$1 patch=$2
  if patch -d "$dir" -p1 -R --dry-run -s -f < "$patch" >/dev/null 2>&1; then echo "already applied: $patch"; return; fi
  patch -d "$dir" -p1 -s < "$patch"; echo "applied: $patch"
}
apply . /recipe/patches/openwrt-prereq-python310.patch
cp /recipe/patches/0002-meshtastic-syncword.patch package/sensecap/sx1302-hal/patches/
cp /recipe/patches/m4-020-glibc-sigstksz.patch tools/m4/patches/020-glibc-sigstksz.patch

# feeds at the release's commits: offline from the mirror's archives (src-link), online with src-git^commit
: > feeds.conf
for f in packages luci routing telephony; do
  read -r url commit <<<"${FEEDS[$f]}"
  if [ "$ONLINE" = 1 ]; then
    echo "src-git $f $url^$commit" >> feeds.conf
  else
    mkdir -p "/work/feeds-src/$f"
    [ -f "/work/feeds-src/$f/.unpacked" ] || { tar xzf /mirror/git/feed-$f-${commit:0:12}.tar.gz -C /work/feeds-src \
      && touch "/work/feeds-src/$f/.unpacked"; }
    echo "src-link $f /work/feeds-src/$f" >> feeds.conf
  fi
done
./scripts/feeds update -a
./scripts/feeds install -a

cp /recipe/diffconfig .config
make defconfig
grep -q '^CONFIG_TARGET_ramips_mt76x8_DEVICE_sensecap_wm7628n=y' .config || { echo "defconfig lost the target" >&2; exit 1; }
grep -q '^CONFIG_PACKAGE_lora_pkt_fwd=y' .config || { echo "defconfig lost lora_pkt_fwd" >&2; exit 1; }

mkdir -p dl
if [ "$ONLINE" != 1 ]; then cp -n /mirror/dl/* dl/ 2>/dev/null || true; fi
export SOURCE_DATE_EPOCH=$(cat version.date)
make -j"$JOBS" download
make -j"$JOBS" world

B=bin/targets/ramips/mt76x8
cp -f $B/*sensecap_wm7628n* $B/config.buildinfo $B/feeds.buildinfo $B/version.buildinfo /work/out/
cp -f build_dir/target-mipsel_24kc_musl/sx1302_hal-2.1.0/ipkg-mipsel_24kc/lora_pkt_fwd/usr/bin/lora/lora_pkt_fwd /work/out/
(cd /work/out && sha256sum *.bin lora_pkt_fwd > SHA256SUMS && cat SHA256SUMS)
echo "== $(date -u +%FT%TZ) done: /work/out"
