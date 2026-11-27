"""Settings for Stratolink: defaults here, overrides from ~/.config/starlab/stratolink.toml ([stratolink] table;
STRATOLINK_CONFIG overrides the path), the SSTV and pirate watches' pattern (they run as tommy, and /etc/starlab is
readable only by the broker's account).

Channel keys that are not public live apart from this file, in ~/.config/starlab/stratolink-keys.toml (mode 600; the
watch refuses a key file that its group or others can read), as  [channels.<name>] psk = "<base64>".
"""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import pathlib
import stat
import tomllib
from dataclasses import dataclass, field
from typing import Any

from . import meshtastic as mt

DEFAULT_CONFIG = pathlib.Path(os.environ.get("STRATOLINK_CONFIG", "~/.config/starlab/stratolink.toml")).expanduser()
DEFAULT_KEYS = pathlib.Path(os.environ.get("STRATOLINK_KEYS", "~/.config/starlab/stratolink-keys.toml")).expanduser()


@dataclass
class Settings:
    data_dir: str = "/data/archive/stratolink"

    # Semtech UDP. Bound to the observatory's LAN address only (never 0.0.0.0), and datagrams are accepted only from
    # allowed_sources (addresses or CIDR blocks: each ground station's address, or the VPN range remote ones use).
    udp_host: str = "192.168.4.66"
    udp_port: int = 1700
    allowed_sources: list = field(default_factory=lambda: ["192.168.4.34"])
    rebind_retry_s: float = 10.0

    # Ground stations known at first start (more are added with `gateway register` or from the page). Location is
    # what the map and the distance/bearing figures use.
    gateways: list = field(default_factory=list)
    gateway_offline_s: float = 90.0     # no PULL_DATA keepalive (10 s) or status (30 s) for this long: offline

    # Public channels decoded for everyone; private ones get their key from the keys file
    channels: list = field(default_factory=lambda: [{"name": "LongFast", "psk": "AQ=="}])
    keys_file: str = str(DEFAULT_KEYS)

    # Trackers known at first start (more are registered from the page or `tracker register`)
    trackers: list = field(default_factory=list)

    # contact thresholds (a flight can set its own)
    loss_warn_s: float = 180.0
    loss_lost_s: float = 600.0

    # position sanity
    max_speed_mps: float = 150.0        # faster than this between fixes is a bad fix (jet stream peaks ~100 m/s)
    min_alt_m: float = -500.0
    max_alt_m: float = 60_000.0

    # privacy: other people's nodes on the public mesh. Their positions are kept only to this many decimal places
    # (2 = ~1 km), never as a track; raw frames of non-tracker packets are dropped after raw_retention_h.
    foreign_position_decimals: int = 2
    raw_retention_h: float = 24.0

    # existing logs imported at start (the manual receiver's packets.jsonl wrote naive local timestamps)
    import_paths: list = field(default_factory=lambda: ["/home/tommy/stratolink/receiver/packets.jsonl"])
    import_timezone: str = "America/New_York"

    # firmware archive served on the Ground stations tab
    firmware_dir: str = "/data/archive/stratolink/firmware"

    # control endpoint for the broker's /stratolink page (broker bearer token, like the SSTV watch)
    broker_token_file: str = "~/.config/observatory/broker.env"
    control_host: str = "127.0.0.1"
    control_port: int = 8754

    extra: dict = field(default_factory=dict)

    @property
    def root(self) -> pathlib.Path:
        return pathlib.Path(self.data_dir).expanduser()

    def path(self, *parts: str) -> pathlib.Path:
        return self.root.joinpath(*parts)

    @property
    def db_path(self) -> pathlib.Path:
        return self.path("stratolink.sqlite3")

    def networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(s, strict=False) for s in self.allowed_sources]

    def source_allowed(self, ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(a in n for n in self.networks())

    def validate(self) -> None:
        if self.udp_host in ("0.0.0.0", "::", ""):
            raise ValueError("udp_host must be the observatory's LAN address, not every interface")
        if not 1 <= int(self.udp_port) <= 65535:
            raise ValueError("udp_port must be 1-65535")
        try:
            nets = self.networks()
        except ValueError as exc:
            raise ValueError(f"allowed_sources: {exc}") from None
        if not nets:
            raise ValueError("allowed_sources: list each ground station's address (or its VPN range)")
        for n in nets:
            if n.prefixlen == 0:
                raise ValueError("allowed_sources: 0.0.0.0/0 would accept datagrams from anywhere")
        for c in self.channels:
            if set(c) - {"name", "psk"} or "name" not in c:
                raise ValueError(f"channels: each is {{name, psk}} (psk optional: from the keys file), not {c}")
        for t in self.trackers:
            mt.node_num(t.get("node_id", ""))
        if not 0 < self.loss_warn_s < self.loss_lost_s:
            raise ValueError("loss_warn_s must be positive and below loss_lost_s")


def load(path: pathlib.Path | None = None) -> Settings:
    path = path or DEFAULT_CONFIG
    raw: dict = {}
    if path.exists():
        with path.open("rb") as f:
            raw = tomllib.load(f).get("stratolink", {})
    known = {f.name for f in dataclasses.fields(Settings)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"{path}: unknown [stratolink] settings: {', '.join(unknown)}")
    s = Settings(**raw)
    s.validate()
    return s


# ------------------------------------------------------------------ channel keys
class KeyFileError(ValueError):
    pass


def read_keys(path: pathlib.Path) -> dict[str, str]:
    """{channel name: base64 PSK} from the keys file; {} when it does not exist."""
    if not path.exists():
        return {}
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise KeyFileError(f"{path} is readable by others (mode {oct(mode & 0o777)}); chmod 600 it")
    with path.open("rb") as f:
        data = tomllib.load(f)
    out = {}
    for name, v in (data.get("channels") or {}).items():
        if not isinstance(v, dict) or not isinstance(v.get("psk"), str):
            raise KeyFileError(f"{path}: [channels.{name}] needs psk = \"<base64>\"")
        out[name] = v["psk"]
    return out


def write_key(path: pathlib.Path, channel: str, psk_b64: str) -> None:
    """Add or replace one channel key; the file is created 600 and rewritten atomically."""
    mt.Channel.from_psk(channel, psk_b64)            # validates the key
    if not channel or any(c in channel for c in "\"\\\n]["):
        raise KeyFileError("channel names cannot contain quotes, brackets, backslashes or newlines")
    keys = read_keys(path)
    keys[channel] = psk_b64
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# Stratolink private channel keys. Mode 600. Never commit or share this file.\n")
        for name, psk in sorted(keys.items()):
            f.write(f'[channels."{name}"]\npsk = "{psk}"\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def channels(s: Settings, extra_names: list[str] | None = None) -> list[mt.Channel]:
    """The channels to try, public ones from settings and private ones from the keys file, plus the stock presets'
    default-key channels (so a tracker left on another preset name still decodes)."""
    keys = read_keys(pathlib.Path(s.keys_file).expanduser())
    out: list[mt.Channel] = []
    for c in s.channels:
        psk = c.get("psk") or keys.get(c["name"])
        if psk is None:
            continue
        out.append(mt.Channel.from_psk(c["name"], psk))
    for name in extra_names or []:
        if name in keys and not any(c.name == name for c in out):
            out.append(mt.Channel.from_psk(name, keys[name], public=False))
    for name, psk in keys.items():
        if not any(c.name == name for c in out):
            out.append(mt.Channel.from_psk(name, psk, public=False))
    for c in mt.DEFAULT_CHANNELS:
        if not any(o.hash == c.hash and o.key == c.key for o in out):
            out.append(c)
    return out
