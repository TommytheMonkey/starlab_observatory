"""Settings for the SSTV watch: defaults here, overrides from a TOML file ([sstvwatch] table), like the pirate watch.

The watch runs as tommy (like starlab-pirate-watch), and /etc/starlab/starlab.toml is readable only by the broker's
account, so its settings live in their own file: ~/.config/starlab/sstvwatch.toml (SSTVWATCH_CONFIG overrides).
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import tomllib
from dataclasses import dataclass, field
from typing import Any

DEFAULT_CONFIG = pathlib.Path(os.environ.get("SSTVWATCH_CONFIG", "~/.config/starlab/sstvwatch.toml")).expanduser()
MODES = ("usb", "lsb", "fm")
WHEN = ("always", "day", "night")


@dataclass
class Channel:
    """One SSTV calling frequency. `hz` is the dial (suppressed-carrier) frequency for USB/LSB, the carrier for FM.
    Dwell weights scale the time spent on it by day and by night (local time); 0 skips it then."""
    name: str
    hz: int
    mode: str = "usb"
    day_weight: float = 1.0
    night_weight: float = 1.0
    when: str = "always"           # always | day | night: outside it the weight is 0 whatever it says

    def weight(self, daytime: bool) -> float:
        if (self.when == "day" and not daytime) or (self.when == "night" and daytime):
            return 0.0
        return float(self.day_weight if daytime else self.night_weight)

    @property
    def band(self) -> str:
        return band_of(self.hz)


def band_of(hz: int) -> str:
    """The amateur band name of a frequency, for filters ("20m", "2m", ...)."""
    for lo, hi, name in ((1_800_000, 2_000_000, "160m"), (3_500_000, 4_000_000, "80m"), (7_000_000, 7_300_000, "40m"),
                         (10_100_000, 10_150_000, "30m"), (14_000_000, 14_350_000, "20m"),
                         (18_068_000, 18_168_000, "17m"), (21_000_000, 21_450_000, "15m"),
                         (24_890_000, 24_990_000, "12m"), (28_000_000, 29_700_000, "10m"),
                         (50_000_000, 54_000_000, "6m"), (144_000_000, 148_000_000, "2m"),
                         (430_000_000, 440_000_000, "70cm")):
        if lo <= hz <= hi:
            return name
    return f"{hz / 1e6:.3f} MHz"


# Defaults asked for on 2026-11-24: 20 m first and longest; 20/15/10 m by day, 40/80 m at night.
DEFAULT_CHANNELS: list[dict[str, Any]] = [
    {"name": "20m", "hz": 14_230_000, "mode": "usb", "day_weight": 4.0, "night_weight": 1.5},
    {"name": "15m", "hz": 21_340_000, "mode": "usb", "day_weight": 2.0, "night_weight": 0.25},
    {"name": "10m", "hz": 28_680_000, "mode": "usb", "day_weight": 2.0, "night_weight": 0.0},
    {"name": "40m", "hz": 7_171_000, "mode": "usb", "day_weight": 0.5, "night_weight": 2.5},
    {"name": "80m", "hz": 3_845_000, "mode": "lsb", "day_weight": 0.0, "night_weight": 2.0, "when": "night"},
]


@dataclass
class Watcher:
    """One HF receiver the watch borrows through the broker. `antenna_port` is the ANTSW-01 port the lease selects
    ("direct" when the receiver is wired straight to its antenna)."""
    receiver: str
    antenna_port: int | str | None = "direct"
    sample_rate_hz: int = 768_000      # Airspy HF+ native rate; the channel is cut out of it
    gain_db: float | None = None       # None: the receiver's AGC (the HF+ has no manual gain)
    driver: str = "airspyhf"           # SoapySDR module
    serial: str = ""

    @property
    def lease_port(self) -> int | None:
        if self.antenna_port is None or self.antenna_port == "direct":
            return None
        if isinstance(self.antenna_port, bool) or not isinstance(self.antenna_port, int):
            raise ValueError(f'{self.receiver}: antenna_port must be "direct" or a switch port number, '
                             f"not {self.antenna_port!r}")
        return self.antenna_port


@dataclass
class Settings:
    data_dir: str = "/data/archive/sstv-watch"
    # The Airspy HF+ is the HF receiver. A second watcher (another HF-capable receiver the broker lends) takes the next
    # channel, never the same one. OBS-R03 is not one: it is the SatNOGS node's dongle on comet, not on this computer
    # (2026-11-24), and the other local receivers are VHF/UHF RTL dongles or the RSP1B that records METEOR.
    watchers: list = field(default_factory=lambda: [{"receiver": "OBS-R10", "serial": "3B528080C04446C7"}])
    channels: list = field(default_factory=lambda: [dict(c) for c in DEFAULT_CHANNELS])

    # dwell (time of day is local, from the site timezone)
    timezone: str = "America/New_York"
    day_start_hour: float = 7.0         # 20/15/10 m open roughly between these; 40/80 m outside them
    day_end_hour: float = 18.0
    dwell_s: float = 300.0              # a weight-1 channel gets this long per visit; weight scales it
    min_dwell_s: float = 60.0
    max_dwell_s: float = 1800.0

    # demodulation and detection
    audio_rate: int = 12_000            # SSB/FM audio rate (SSTV tones are 1100-2300 Hz)
    ssb_low_hz: float = 300.0
    ssb_high_hz: float = 3_000.0
    detect_every_s: float = 1.0         # look for a VIS header this often, over the last detect_window_s
    detect_window_s: float = 3.0
    pre_vis_s: float = 2.0              # audio/IQ kept from before the header, in the saved transmission
    post_image_s: float = 5.0           # kept after the mode's image time
    max_transmission_s: float = 330.0   # the longest mode (PD 290, Scottie DX) plus margin

    # decode and keep
    min_quality: float = 0.35           # decode-quality score 0-1 below this = mostly noise: not kept as an image
    decode_timeout_s: float = 120.0     # one transmission's decode, in its own process
    decode_heartbeat_stale_s: float = 30.0
    keep_iq: bool = True                # save the channel IQ of each transmission (12 kHz complex, ~23 MB per 4 min)

    # ISS (pass-triggered): the broker already records ISS passes on the receiver that has them and decodes SSTV
    # (437.550, and 145.800 when that downlink is switched on); the watch takes those images into its gallery
    iss_import: bool = True
    iss_poll_s: float = 120.0

    # notifications (Slack through the broker, like the pirate watch's alerts)
    notify: bool = True
    batch_window_s: float = 600.0       # more than batch_max good decodes in this window: one batched message
    batch_max: int = 5

    # broker
    broker_url: str = "http://127.0.0.1:8751"
    broker_token_file: str = "~/.config/observatory/broker.env"
    lease_owner: str = "sstv-watch"
    lease_ttl_s: int = 120
    renew_every_s: float = 5.0
    retry_lease_s: float = 60.0

    # control endpoint (status, release/resume, gallery) for the broker's /sstvwatch page; broker token
    control_host: str = "127.0.0.1"
    control_port: int = 8753

    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.channels = [c if isinstance(c, Channel) else Channel(**c) for c in self.channels]
        self.watchers = [w if isinstance(w, Watcher) else Watcher(**w) for w in self.watchers]

    @property
    def root(self) -> pathlib.Path:
        return pathlib.Path(self.data_dir).expanduser()

    def path(self, *parts: str) -> pathlib.Path:
        return self.root.joinpath(*parts)

    def validate(self) -> None:
        if not self.watchers:
            raise ValueError("watchers: at least one HF receiver is needed")
        for w in self.watchers:
            w.lease_port
        names = [c.name for c in self.channels]
        if len(set(names)) != len(names):
            raise ValueError(f"channels: names must be unique ({names})")
        for c in self.channels:
            if c.mode not in MODES:
                raise ValueError(f"channel {c.name}: mode must be one of {MODES}, not {c.mode!r}")
            if c.when not in WHEN:
                raise ValueError(f"channel {c.name}: when must be one of {WHEN}, not {c.when!r}")
            if c.day_weight < 0 or c.night_weight < 0:
                raise ValueError(f"channel {c.name}: weights cannot be negative")
        if not 0 <= self.day_start_hour < self.day_end_hour <= 24:
            raise ValueError("day_start_hour must be before day_end_hour, both 0-24")
        if not 0 <= self.min_quality <= 1:
            raise ValueError("min_quality is a 0-1 score")


def load(path: pathlib.Path | None = None) -> Settings:
    path = path or DEFAULT_CONFIG
    raw: dict = {}
    if path.exists():
        with path.open("rb") as f:
            raw = tomllib.load(f).get("sstvwatch", {})
    known = {f.name for f in dataclasses.fields(Settings)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"{path}: unknown [sstvwatch] settings: {', '.join(unknown)}")
    try:
        s = Settings(**raw)
    except TypeError as exc:                 # an unknown key inside a channel or watcher table
        raise ValueError(f"{path}: {exc}") from None
    s.validate()
    return s
