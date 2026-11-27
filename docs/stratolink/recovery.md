# Recovery

## Observatory side

| Problem | Do |
|---|---|
| Stratolink stopped | `sudo systemctl restart starlab-stratolink`; `journalctl -u starlab-stratolink -n 50` |
| Need the manual receiver back | `sudo systemctl stop starlab-stratolink`, then run `receiver.py` as before |
| Database damaged | stop the service; move `stratolink.sqlite3*` aside; start: it re-imports `packets.jsonl`. Receptions after the switch-over exist only in the database: back it up (below) |
| Lost settings | `deploy/stratolink/stratolink.example.toml`; trackers and stations are also in the database |
| Lost channel keys | re-enter them on Trackers → Key (they exist nowhere else by design) |

Backup (online, consistent):
`sqlite3 /data/archive/stratolink/stratolink.sqlite3 ".backup /data/archive/stratolink/backup-$(date +%F).sqlite3"`.

## Gateway side

1. **Forwarder not running after reboot:** `ssh root@<gw> /etc/init.d/stratolink status; logread -e stratolink`.
   `/etc/init.d/stratolink restart`. Is `/root/stratolink.json` there?
2. **Back to stock LoRaWAN:** `python -m aobs.stratolink gateway rollback <gw>`.
3. **Restore pre-deploy state:** on the gateway `tar xzf /root/stratolink-backup-<time>.tgz -C / && reboot` (a copy
   is in `/data/archive/stratolink/gateway-config/backups/`).
4. **Bad config / cannot log in:** OpenWrt failsafe. Power on and press the reset button repeatedly when the status
   LED blinks fast during boot (SenseCAP M2: hold the button ~1 s right after power-on; confirm against Seeed's M2
   documentation). Connect to the LAN port with a static 192.168.1.2, `ssh root@192.168.1.1`, `mount_root`, fix or
   `firstboot && reboot` (factory reset of settings).
5. **Bad flash / bricked:** boot the archived `initramfs-kernel.bin` from the bootloader over TFTP (U-Boot menu on
   the serial console, 57600 8N1 on MT7628 boards, option "load system code then write to flash" / "boot from
   TFTP"), then sysupgrade the archived image from the running RAM system. The serial header location and U-Boot
   menu of the M2 are **not documented here yet**: verify with Seeed's documentation before relying on this path.
6. **Factory SenseCAP firmware:** Seeed publishes it for the M2 (sensecap.seeed.cc); flash it like any sysupgrade.

Untested: steps 4-6 have not been rehearsed on Stratolink's gateway. Rehearse on a spare unit before a flight
depends on them.
