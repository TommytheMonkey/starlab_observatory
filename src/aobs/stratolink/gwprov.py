"""Ground-station provisioning over SSH, run on the observatory (`python -m aobs.stratolink gateway ...`).

    gateway trust     HOST      install a Stratolink SSH key on the gateway (you type the root password into ssh
                                itself, once; it is never stored or passed on the command line)
    gateway inspect   HOST      read-only: board, region, firmware, EUI, which LoRa services are enabled, compatibility
    gateway capture   HOST      save a working gateway's /root/stratolink.json as the radio-config template
                                (device-specific fields stripped)
    gateway deploy    HOST --name N --lat --lon [--alt]   back up, write config + procd service, disable the stock
                                LoRaWAN services, start, register, wait for the keepalive
    gateway rollback  HOST      stop/disable Stratolink, re-enable the stock lora_pkt_fwd
    gateway verify    EUI       wait for the keepalive and for a decoded RF packet through this gateway
    gateway flash     HOST --release R   (supported hardware only) upload, checksum and sysupgrade the archived image

The gateway's own identity is always read from the gateway: its EUI comes from its eth0 MAC (SenseCAP's rule,
MAC + 0000), its SSH host key is pinned on first contact, its password is never touched. Nothing device-specific is
copied from one gateway to another; the template keeps only the radio settings.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import pathlib
import re
import shlex
import subprocess
import time
from typing import Any, Callable

from . import config as cfgmod
from . import firmware
from .store import Store, iso

UTC = dt.timezone.utc
REPO = pathlib.Path(__file__).resolve().parents[3]
INIT_SCRIPT = REPO / "deploy" / "stratolink" / "gateway" / "stratolink.init"
FALLBACK_TEMPLATE = REPO / "deploy" / "stratolink" / "gateway" / "stratolink.template.json"
KEY = pathlib.Path("~/.ssh/stratolink_gateway_ed25519").expanduser()
KNOWN_HOSTS = pathlib.Path("~/.ssh/stratolink_known_hosts").expanduser()
BOARD = "sensecap,wm7628n"
US915 = ("915", "US915")
DEVICE_FIELDS = ("gateway_ID", "server_address", "serv_port_up", "serv_port_down", "ref_latitude", "ref_longitude",
                 "ref_altitude", "gps_tty_path")


class GatewayError(RuntimeError):
    pass


Runner = Callable[[list[str], bytes | None, float], tuple[int, bytes, str]]


def run_local(cmd: list[str], stdin: bytes | None = None, timeout: float = 60) -> tuple[int, bytes, str]:
    p = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout or b"", (p.stderr or b"").decode("utf-8", "replace")


class Gateway:
    """One gateway over SSH. `runner` is replaceable for tests."""

    def __init__(self, host: str, user: str = "root", runner: Runner = run_local, port: int = 22):
        if not re.fullmatch(r"[A-Za-z0-9.:_-]{1,253}", host):
            raise GatewayError(f"{host!r} is not a host name or address")
        self.host, self.user, self.port, self.runner = host, user, port, runner

    def ssh_args(self, batch: bool = True) -> list[str]:
        a = ["ssh", "-p", str(self.port), "-o", "ConnectTimeout=10", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
             "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=10"]
        if KEY.exists():
            a += ["-i", str(KEY), "-o", "IdentitiesOnly=yes"]
        if batch:
            a += ["-o", "BatchMode=yes"]
        return a + [f"{self.user}@{self.host}"]

    def sh(self, script: str, stdin: bytes | None = None, timeout: float = 60, check: bool = True) -> str:
        return self.raw(script, stdin, timeout, check).decode("utf-8", "replace")

    def raw(self, script: str, stdin: bytes | None = None, timeout: float = 60, check: bool = True) -> bytes:
        code, out, err = self.runner(self.ssh_args() + [script], stdin, timeout)
        if check and code != 0:
            raise GatewayError(f"{self.host}: `{script[:80]}` failed ({code}): {err.strip()[-400:]}")
        return out

    def put(self, path: str, data: bytes, mode: str = "644") -> None:
        """Write a file atomically (dropbear has no SFTP, so through cat)."""
        q = shlex.quote(path)
        self.sh(f"cat > {q}.new && chmod {mode} {q}.new && mv {q}.new {q}", stdin=data)

    # ------------------------------------------------------------ inspection
    INSPECT = r"""
echo "board_name=$(cat /tmp/sysinfo/board_name 2>/dev/null)"
echo "model=$(cat /tmp/sysinfo/model 2>/dev/null)"
. /etc/openwrt_release 2>/dev/null; echo "openwrt=$DISTRIB_DESCRIPTION"; echo "openwrt_revision=$DISTRIB_REVISION"
echo "eth0_mac=$(cat /sys/class/net/eth0/address 2>/dev/null)"
for f in eui loraModem sn hw_version; do echo "deviceinfo_$f=$(cat /etc/deviceinfo/$f 2>/dev/null)"; done
echo "pkt_fwd_sha256=$(sha256sum /usr/bin/lora/lora_pkt_fwd 2>/dev/null | cut -d' ' -f1)"
echo "stratolink_json=$( [ -s /root/stratolink.json ] && sha256sum /root/stratolink.json | cut -d' ' -f1)"
for s in lora_pkt_fwd station stratolink; do
  if [ -x /etc/init.d/$s ]; then /etc/init.d/$s enabled && e=enabled || e=disabled; else e=absent; fi
  echo "service_$s=$e"
done
echo "running=$(pgrep -f lora_pkt_fwd | tr '\n' ' ')"
echo "running_cmd=$(for p in $(pgrep -f /lora_pkt_fwd\|./lora_pkt_fwd); do tr '\0' ' ' < /proc/$p/cmdline; echo -n '; '; done)"
echo "spidev=$(ls /dev/spidev* 2>/dev/null | tr '\n' ' ')"
echo "uptime=$(cut -d' ' -f1 /proc/uptime)"
echo "mem_free_kb=$(grep MemAvailable /proc/meminfo | tr -s ' ' | cut -d' ' -f2)"
echo "lan_ip=$(uci -q get network.lan.ipaddr)"
"""

    def inspect(self) -> dict[str, Any]:
        out = self.sh(self.INSPECT)
        info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        info = {k: v.strip() for k, v in info.items()}
        mac = info.get("eth0_mac", "")
        info["derived_eui"] = (mac.replace(":", "").upper() + "0000") if re.fullmatch(r"[0-9a-fA-F:]{17}", mac) else None
        info["eui"] = (info.get("deviceinfo_eui") or info["derived_eui"] or "").upper() or None
        problems, notes = [], []
        if info.get("board_name") != BOARD:
            problems.append(f"board {info.get('board_name')!r} is not {BOARD} (SenseCAP M2 WM7628N): unsupported")
        if info.get("deviceinfo_loraModem") not in US915:
            problems.append(f"LoRa modem region {info.get('deviceinfo_loraModem')!r} is not US915: unsupported")
        if not info.get("spidev"):
            problems.append("no /dev/spidev*: the concentrator is not reachable")
        info["compatible"] = not problems
        info["problems"], info["notes"] = problems, notes
        return info


# ------------------------------------------------------------------ radio config
def template_path(s: cfgmod.Settings) -> pathlib.Path:
    return s.path("gateway-config", "reference.template.json")


def strip_device(cfg: dict[str, Any]) -> dict[str, Any]:
    t = copy.deepcopy(cfg)
    gc = t.get("gateway_conf") or {}
    for k in DEVICE_FIELDS:
        gc.pop(k, None)
    t["gateway_conf"] = gc
    return t


def json_loose(text: str) -> dict[str, Any]:
    """Semtech configs allow /* */ comments."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return json.loads(text)


def load_template(s: cfgmod.Settings) -> tuple[dict[str, Any], str]:
    p = template_path(s)
    if p.exists():
        return json.loads(p.read_text()), f"captured reference ({p})"
    return json_loose(FALLBACK_TEMPLATE.read_text()), f"built-in template ({FALLBACK_TEMPLATE.name}, not yet field-verified)"


def render(template: dict[str, Any], eui: str, server: str, port: int) -> dict[str, Any]:
    cfg = copy.deepcopy(template)
    cfg.pop("_comment", None)
    sx = cfg.setdefault("SX130x_conf", {})
    for r in ("radio_0", "radio_1"):          # receive only: no radio may transmit, whatever the template says
        if r in sx:
            sx[r]["tx_enable"] = False
    sx["lorawan_public"] = False              # the patched private sync word (0x2B) is used only when not public
    gc = cfg.setdefault("gateway_conf", {})
    gc.update({"gateway_ID": eui, "server_address": server, "serv_port_up": port, "serv_port_down": port})
    gc.setdefault("keepalive_interval", 10)
    gc.setdefault("stat_interval", 30)
    std = sx.get("chan_Lora_std") or {}
    if not std.get("enable"):
        raise GatewayError("the template has no enabled chan_Lora_std (the 250 kHz SF11 LONG_FAST channel)")
    return cfg


def radio_summary(cfg: dict[str, Any]) -> dict[str, Any]:
    sx = cfg.get("SX130x_conf", {})
    std = sx.get("chan_Lora_std", {})
    radio = sx.get(f"radio_{std.get('radio', 0)}", {})
    freq = (radio.get("freq") or 0) + (std.get("if") or 0)
    return {"freq_mhz": freq / 1e6 if freq else None, "bw_khz": (std.get("bandwidth") or 0) / 1000 or None,
            "sf": std.get("spread_factor"), "cr": "4/5", "preset": "LONG_FAST", "sync_word": "0x2B",
            "receive_only": not any(sx.get(r, {}).get("tx_enable") for r in ("radio_0", "radio_1"))}


# ------------------------------------------------------------------ operations
def trust(gw: Gateway) -> str:
    if not KEY.exists():
        KEY.parent.mkdir(mode=0o700, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "stratolink@observatory", "-f",
                        str(KEY)], check=True)
    pub = KEY.with_suffix(".pub").read_text().strip()
    script = ("mkdir -p /etc/dropbear && touch /etc/dropbear/authorized_keys && chmod 600 /etc/dropbear/authorized_keys"
              f" && (grep -qF {shlex.quote(pub)} /etc/dropbear/authorized_keys || echo {shlex.quote(pub)} >> "
              "/etc/dropbear/authorized_keys) && echo ok")
    # interactive: ssh asks for the password itself
    code = subprocess.run(gw.ssh_args(batch=False) + [script]).returncode
    if code != 0:
        raise GatewayError(f"could not install the key on {gw.host} (ssh exit {code})")
    return pub


def capture(gw: Gateway, s: cfgmod.Settings) -> dict[str, Any]:
    raw = gw.sh("cat /root/stratolink.json")
    cfg = json_loose(raw)
    t = strip_device(cfg)
    p = template_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (p.parent / f"captured-{gw.host}-{stamp}.json").write_text(raw)        # the original, for the record
    p.write_text(json.dumps(t, indent=2) + "\n")
    return {"template": str(p), "radio": radio_summary(cfg), "original_gateway_ID":
            (cfg.get("gateway_conf") or {}).get("gateway_ID")}


def deploy(gw: Gateway, s: cfgmod.Settings, store: Store, name: str, lat: float, lon: float, alt: float | None,
           server: str | None = None, dry_run: bool = False, log: Callable[[str], None] = print) -> dict[str, Any]:
    info = gw.inspect()
    if not info["compatible"]:
        raise GatewayError("not deploying: " + "; ".join(info["problems"]))
    eui = info["eui"]
    if not eui or not re.fullmatch(r"[0-9A-F]{16}", eui):
        raise GatewayError(f"could not read this gateway's EUI (eth0 MAC {info.get('eth0_mac')!r})")
    clash = store.gateway(eui)
    if clash and clash.get("registered") and clash.get("name") not in (None, name):
        log(f"note: {eui} is already registered as {clash['name']!r}; it will be renamed {name!r}")
    template, source = load_template(s)
    cfg = render(template, eui, server or s.udp_host, int(s.udp_port))
    data = (json.dumps(cfg, indent=2) + "\n").encode()
    log(f"gateway {gw.host}: {info.get('model')} {info.get('openwrt')}, EUI {eui}, region "
        f"{info.get('deviceinfo_loraModem')}; config from {source}")
    log(f"services now: lora_pkt_fwd {info['service_lora_pkt_fwd']}, station {info['service_station']}, "
        f"stratolink {info['service_stratolink']}; running: {info.get('running_cmd') or 'none'}")
    plan = ["back up /etc/config, /etc/rc.d, /root/*.json to /root/stratolink-backup-<time>.tgz (and to the "
            "observatory)", f"write /root/stratolink.json (gateway_ID {eui}, server {cfg['gateway_conf']['server_address']}"
            f":{s.udp_port}, both radios tx_enable false)", "install /etc/init.d/stratolink (procd, respawn)",
            "stop and disable lora_pkt_fwd and station; stop a manually started lora_pkt_fwd",
            "enable and start stratolink", f"register {eui} as {name!r} at {lat}, {lon}"]
    for p in plan:
        log(f"  - {p}")
    if dry_run:
        return {"eui": eui, "plan": plan, "config": cfg, "dry_run": True}
    started = dt.datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    bk = f"/root/stratolink-backup-{stamp}.tgz"
    gw.sh(f"tar czf {bk} /etc/config /etc/rc.d /etc/init.d $(ls /root/*.json 2>/dev/null) 2>/dev/null; ls -l {bk}")
    local_bk = s.path("gateway-config", "backups", f"{eui}-{stamp}.tgz")
    local_bk.parent.mkdir(parents=True, exist_ok=True)
    local_bk.write_bytes(gw.raw(f"cat {bk}", timeout=120))
    gw.put("/root/stratolink.json", data, "600")
    gw.put("/etc/init.d/stratolink", INIT_SCRIPT.read_bytes(), "755")
    gw.sh("for s in lora_pkt_fwd station; do [ -x /etc/init.d/$s ] && { /etc/init.d/$s stop; /etc/init.d/$s disable; }; "
          "done; true")
    gw.sh("for p in $(pgrep -f lora_pkt_fwd); do kill $p; done; sleep 2; true")
    gw.sh("/etc/init.d/stratolink enable && /etc/init.d/stratolink start; sleep 3; pgrep -f lora_pkt_fwd >/dev/null")
    g = store.register_gateway(eui, name=name, lat=lat, lon=lon, alt_m=alt, ip=gw.host,
                               hardware=info.get("model"), firmware=info.get("openwrt_revision"),
                               radio=radio_summary(cfg), notes=f"deployed {iso(started)} from {source}")
    log(f"deployed; backup {bk} on the gateway"
        + (f" and {local_bk} here" if local_bk.exists() and local_bk.stat().st_size else ""))
    return {"eui": eui, "gateway": g, "backup": bk, "started": iso(started)}


def rollback(gw: Gateway) -> str:
    return gw.sh("[ -x /etc/init.d/stratolink ] && { /etc/init.d/stratolink stop; /etc/init.d/stratolink disable; }; "
                 "/etc/init.d/lora_pkt_fwd enable && /etc/init.d/lora_pkt_fwd start; sleep 2; "
                 "echo stratolink: $(/etc/init.d/stratolink enabled 2>/dev/null && echo enabled || echo disabled); "
                 "echo lora_pkt_fwd: $(/etc/init.d/lora_pkt_fwd enabled && echo enabled)")


def verify(store: Store, eui: str, since: dt.datetime, keepalive_timeout: float = 120, rf_timeout: float = 600,
           log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Wait for this gateway's keepalive, then for a decoded packet it received after `since`."""
    t0, s_since = time.monotonic(), iso(since)
    while True:
        g = store.gateway(eui) or {}
        if g.get("last_pull") and g["last_pull"] >= s_since:
            log(f"keepalive from {eui} at {g['last_pull']} (from {g.get('last_ip')})")
            break
        if time.monotonic() - t0 > keepalive_timeout:
            raise GatewayError(f"no keepalive from {eui} within {keepalive_timeout:.0f} s: is the observatory address "
                               "right in its config, and is its address in allowed_sources?")
        sleep(3)
    log(f"waiting up to {rf_timeout:.0f} s for an RF packet: send a message from a tracker or a Meshtastic radio")
    t1 = time.monotonic()
    while time.monotonic() - t1 < rf_timeout:
        r = store.one("SELECT r.*, p.port, p.sender FROM rx r LEFT JOIN packets p ON p.id = r.packet_ref WHERE "
                      "r.eui = ? AND r.rx_utc >= ? AND r.crc = 1 ORDER BY r.id LIMIT 1", (eui, s_since))
        if r:
            log(f"RF packet through {eui}: {r['status']} {r.get('port') or ''} from "
                f"{'!%08x' % r['sender'] if r.get('sender') is not None else '?'}, RSSI {r['rssi']} dBm, SNR {r['snr']} dB")
            return {"keepalive": True, "rf": r}
        sleep(3)
    raise GatewayError(f"keepalive OK but no CRC-valid RF packet through {eui} within {rf_timeout:.0f} s")


def flash(gw: Gateway, s: cfgmod.Settings, release: str, confirm: Callable[[str], str] = input,
          log: Callable[[str], None] = print) -> None:
    info = gw.inspect()
    if not info["compatible"]:
        raise GatewayError("refusing to flash: " + "; ".join(info["problems"]))
    root = pathlib.Path(s.firmware_dir).expanduser()
    rel = next((r for r in firmware.releases(root) if r["id"] == release), None)
    if rel is None:
        raise GatewayError(f"no archived release {release!r}")
    img = next((f for f in rel["files"] if f["path"].endswith("squashfs-sysupgrade.bin")), None)
    path = firmware.file_for(root, release, img["path"]) if img else None
    if path is None:
        raise GatewayError("the archived sysupgrade image is missing or no longer matches its checksum")
    if rel["hardware"]["board_name"] != info["board_name"]:
        raise GatewayError(f"the image is for {rel['hardware']['board_name']}, the gateway is {info['board_name']}")
    log(f"uploading {path.name} ({path.stat().st_size} bytes) to /tmp/stratolink-fw.bin")
    gw.put("/tmp/stratolink-fw.bin", path.read_bytes(), "600")
    got = gw.sh("sha256sum /tmp/stratolink-fw.bin | cut -d' ' -f1").strip()
    if got != img["sha256"]:
        gw.sh("rm -f /tmp/stratolink-fw.bin", check=False)
        raise GatewayError(f"checksum on the gateway {got} != archived {img['sha256']}: not flashing")
    gw.sh("sysupgrade -T /tmp/stratolink-fw.bin")
    log("sysupgrade accepts the image. Flashing wipes the gateway's settings (-n): it comes back on its default "
        "address (192.168.168.1 on the LAN port), with a new SSH host key and no root password. Power must not be "
        "interrupted for ~3 minutes.")
    if confirm(f"Type FLASH {info['eui']} to flash: ").strip() != f"FLASH {info['eui']}":
        gw.sh("rm -f /tmp/stratolink-fw.bin", check=False)
        raise GatewayError("not confirmed; nothing flashed")
    gw.runner(gw.ssh_args() + ["sysupgrade -n /tmp/stratolink-fw.bin >/dev/null 2>&1 &"], None, 30)
    log("flashing started. Remove the gateway's old host key: "
        f"ssh-keygen -R {gw.host} -f {KNOWN_HOSTS}")


def env_password_refused() -> None:
    """Passwords are typed into ssh, never taken from the environment or arguments."""
    for k in ("STRATOLINK_GW_PASSWORD", "SSHPASS"):
        if os.environ.get(k):
            raise GatewayError(f"{k} is set: Stratolink does not take gateway passwords from the environment; use "
                               "`gateway trust HOST` once (you type the password into ssh)")
