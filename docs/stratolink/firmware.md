# Ground-station firmware

## The archived release

`/data/archive/stratolink/firmware/sensecap-m2-us915-meshtastic-2026.11.27/` (outside the build tree; images read-only)

| | |
|---|---|
| `images/…-squashfs-sysupgrade.bin` | the flashed image, SHA-256 `2a220aa2cbaeda91cb6638da32456737ee4166084385a8a99134e91732131c69` (verified) |
| `images/…-initramfs-kernel.bin` | RAM-only recovery image, SHA-256 in `SHA256SUMS` |
| `patches/0002-meshtastic-syncword.patch` | SX1302 LoRa service modem private sync word 0x12 → 0x2B |
| `patches/openwrt-prereq-python310.patch` | lets OpenWrt 21.02 accept Python 3.10 |
| `patches/m4-020-glibc-sigstksz.patch` | m4 1.4.18 on glibc ≥ 2.34 (was a hand edit in build_dir) |
| `config/openwrt.config`, `config/diffconfig` | full and minimal OpenWrt configuration |
| `buildinfo/` | manifest, config/feeds/version buildinfo, upstream sha256sums |
| `packages/` | `lora_pkt_fwd` and `sx1302-utils` ipk |
| `source/` | sx1302_hal 2.1.0 and m4 tarballs |
| `RELEASE.json` | commits, versions, hardware compatibility, build environment, every file's checksum |

`../source-mirror/` holds every download (`dl/`) and `git archive` snapshots of the Seeed tree and the four feeds at
their commits, so a rebuild needs no network.

Verify: `cd …/sensecap-m2-us915-meshtastic-2026.11.27 && sha256sum -c SHA256SUMS`, or
`python -m aobs.stratolink firmware`. Starlab serves the images on Ground stations → Add a ground station, and only
while their checksums still match.

## Sources

| | |
|---|---|
| OpenWrt tree | github.com/Seeed-Solution/LoRa_Gateway_OpenWRT @ `79a1b578039fa178c5a884e1f53329737539ceb9` (OpenWrt 21.02.0 r16279-5cc0535800) |
| feeds | packages `65057dcb…`, luci `3b3c2e5f…`, routing `c30c9ffc…`, telephony `7f73a9ad…` (full hashes in RELEASE.json) |
| sx1302_hal | Lora-net 2.1.0, commit `4b42025d…`, tarball SHA-256 `4b62aa6a…de5b`, Seeed package release 16 + their `0001-feat-adapt-sensecap-m2.patch` |
| build host | Docker `ubuntu:22.04@sha256:b000e69a…`, gcc 11.2, Python 3.10.12, package versions in RELEASE.json |

## Rebuilding

```bash
~/starlab-dev/deploy/stratolink/firmware/build.sh            # offline, from the source mirror
ONLINE=1 ~/starlab-dev/deploy/stratolink/firmware/build.sh   # from upstream
```

No menuconfig: it unpacks the tree, applies the three patches, links the feeds at their commits, `make defconfig`
from `diffconfig`, and builds in Docker as an unprivileged user at nice 19 on 4 CPUs (override `CPUS`, `JOBS`,
`WORK`). Output: `$WORK/out` with `SHA256SUMS`.

**Tested 2026-11-27** (offline, 4 CPUs, 18 minutes): the rebuilt `lora_pkt_fwd` is bit-identical to the archived
one (`3bcdd626…a287`), `config.buildinfo` is identical, the image contains the same 215 packages and the sysupgrade
image is the same size. Package *version strings* differ (the source snapshots carry no `.git`, so OpenWrt derives
versions from dates, e.g. `busybox 1.33.1-210831.80408` instead of `1.33.1-1`, and LuCI `210901.20922` instead of
`git-…`); `ONLINE=1` uses git checkouts and avoids that.

The rebuild is functionally identical but **not bit-identical** to the archived image: OpenWrt 21.02 embeds a
per-build signing key and timestamps. The archived, field-verified image stays the one to flash. Compare
`lora_pkt_fwd` (archived binary SHA-256 `3bcdd626…a287`) and the manifest when checking a rebuild.

## What the patch does

The SX1302's LoRa *service* modem (the single-SF channel `chan_Lora_std`, which can do 250 kHz) uses the private
sync word when `lorawan_public` is false. Stock HAL writes peak positions 2/4 (sync word 0x12); the patch writes
4/22, which is the SX126x-family sync word 0x2B that Meshtastic uses. The multi-SF channels (125 kHz only) are not
used. The deployed config therefore has `lorawan_public: false` and `chan_Lora_std` at 906.875 MHz, 250 kHz, SF11.

## Hardware compatibility

Supported: **SenseCAP M2 LoRaWAN gateway, US915**, OpenWrt device `sensecap_wm7628n`
(`/tmp/sysinfo/board_name` = `sensecap,wm7628n`, model "SenseCAP WM7628N", MT7628AN, SX1302 + SX1250, `/dev/spidev0.1`).

Not supported (the provisioning tool refuses them): any other board name, and any other region
(`/etc/deviceinfo/loraModem` not `915`/`US915`: different RF front end). Other SenseCAP M2 variants (Multi-Platform,
Data-Only, Helium) have not been checked; inspect one with `gateway inspect` before considering it.

## Flashing

From a gateway already running OpenWrt (stock SenseCAP or this firmware), either

* the gateway's web page: System → Backup / Flash Firmware → upload the sysupgrade image, **untick "Keep settings"**; or
* `python -m aobs.stratolink gateway flash <ip> --release sensecap-m2-us915-meshtastic-2026.11.27`, which checks the
  board name and region, uploads, verifies the SHA-256 on the gateway, runs `sysupgrade -T`, and only flashes after
  you type `FLASH <its EUI>`.

After a flash without settings the gateway comes back at 192.168.168.1 on its LAN port with no root password and a
new SSH host key: connect a laptop to it, set a root password (`passwd` or the web page), set its network, then
continue with [ground-stations.md](ground-stations.md). Never power it off during the ~3 minutes of a flash.
Recovery: [recovery.md](recovery.md).
