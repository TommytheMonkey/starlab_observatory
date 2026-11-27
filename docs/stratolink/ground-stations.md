# Adding a ground station (SenseCAP M2 US915)

All commands run on the observatory from `~/starlab-dev` (`.venv/bin/python -m aobs.stratolink …`, shortened here to
`sl`). Nothing device-specific is copied between gateways: each one's EUI comes from its own eth0 MAC (SenseCAP's
rule, MAC + `0000`), its SSH host key is pinned when first seen (`~/.ssh/stratolink_known_hosts`), and passwords are
only ever typed into ssh itself (the tool refuses `SSHPASS`/`STRATOLINK_GW_PASSWORD`).

## 0. Once: capture the working radio config

The template for new stations comes from the gateway that is known to work. Capturing strips `gateway_ID`, server
and GPS reference fields and keeps only the radio settings.

```bash
sl gateway trust 192.168.4.34      # ssh asks for its root password once; installs ~/.ssh/stratolink_gateway_ed25519
sl gateway capture 192.168.4.34    # -> /data/archive/stratolink/gateway-config/reference.template.json
```

Until a capture exists, the built-in `deploy/stratolink/gateway/stratolink.template.json` is used (built from the
SX1302 US915 reference and SenseCAP's paths; not yet run on hardware), and `deploy` says which one it used.

## 1. Firmware

Download the image from Starlab (Ground stations → Add a ground station) and flash it as described in
[firmware.md](firmware.md). Set the root password, put it on the network, note its address.

## 2. Trust, inspect

```bash
sl gateway trust 192.168.4.77
sl gateway inspect 192.168.4.77
```

`inspect` is read-only. It reports the board name, region, OpenWrt version, eth0 MAC and derived EUI, the
`lora_pkt_fwd` checksum (the archived build is `3bcdd626…`), which LoRa services are enabled or running, and
`"compatible": true/false` with the reasons.

## 3. Deploy

```bash
sl gateway deploy 192.168.4.77 --name "Ridge" --lat 35.9012 --lon -81.2034 --alt 412 --dry-run
sl gateway deploy 192.168.4.77 --name "Ridge" --lat 35.9012 --lon -81.2034 --alt 412
```

The dry run prints the exact config and plan. The real run:

1. refuses an incompatible gateway;
2. backs up `/etc/config`, `/etc/rc.d`, `/etc/init.d` and `/root/*.json` to `/root/stratolink-backup-<time>.tgz` on
   the gateway and to `/data/archive/stratolink/gateway-config/backups/<EUI>-<time>.tgz`;
3. writes `/root/stratolink.json`: the template, its own EUI, server `udp_host:1700`, `lorawan_public: false`,
   **`tx_enable: false` on both radios** whatever the template says;
4. installs `/etc/init.d/stratolink` (procd, respawning, `deploy/stratolink/gateway/stratolink.init`);
5. stops and **disables** `lora_pkt_fwd` (stock LoRaWAN) and `station` (Basics Station), so neither can take the
   concentrator after a reboot, and stops a manually started forwarder;
6. enables and starts `stratolink`;
7. registers the station (name, position, address, radio settings), which also allows its address to send;
8. waits for its PULL_DATA keepalive, then for a CRC-valid RF packet: **send a test message from a tracker** (or any
   Meshtastic radio on LongFast). It prints the packet with RSSI and SNR.

A station elsewhere (not on 192.168.4.x) reaches the observatory over a VPN: deploy with `--server <the
observatory's VPN address>`, set `udp_host` to listen there, and add the VPN range to `allowed_sources`. Never
forward UDP 1700 from the internet.

## 4. Afterwards

`sl gateway verify <EUI> --timeout 600` repeats the check at any time. Stations show on Ground stations with status,
temperature, acknowledgement rate, packet counts and coverage, and on the map.

## The existing Observatory gateway (192.168.4.34)

It was started by hand (`cd /usr/bin/lora && ./lora_pkt_fwd -c /root/stratolink.json`), so it does not survive a
reboot. To make it persistent, **after reviewing the plan**:

```bash
sl gateway trust 192.168.4.34
sl gateway inspect 192.168.4.34
sl gateway capture 192.168.4.34
sl gateway deploy 192.168.4.34 --name Observatory --lat <exact> --lon <exact> --alt <m> --dry-run   # review
sl gateway deploy 192.168.4.34 --name Observatory --lat <exact> --lon <exact> --alt <m>
```

The rendered config is the captured one with the same EUI and server, plus `tx_enable: false` (receive-only) and
`lorawan_public: false` (already required for the 0x2B sync word). Then reboot the gateway once and check that the
keepalive comes back (`sl gateway verify 2CF7F12004B70000`).

## Rollback

```bash
sl gateway rollback 192.168.4.77      # stop + disable stratolink, enable + start the stock lora_pkt_fwd
```

or on the gateway: `/etc/init.d/stratolink stop; /etc/init.d/stratolink disable; /etc/init.d/lora_pkt_fwd enable;
/etc/init.d/lora_pkt_fwd start`. The pre-deploy backup restores everything:
`tar xzf /root/stratolink-backup-<time>.tgz -C / && reboot`.
