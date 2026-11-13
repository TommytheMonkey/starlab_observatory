"""Settings for the satcom pirate watch: defaults here, overrides from a TOML file ([piratewatch] table)."""

from __future__ import annotations

import dataclasses
import math
import os
import pathlib
import tomllib
from dataclasses import dataclass, field

DEFAULT_CONFIG = pathlib.Path(os.environ.get("PIRATEWATCH_CONFIG", "~/.config/starlab/piratewatch.toml")).expanduser()


@dataclass
class Settings:
    receiver: str = "OBS-R05"
    # "direct" (or unset): the receiver is wired straight to its antenna (OBS-R05 -> discone since 2026-11-11), so
    # the lease never selects a switch port. An integer selects that ANTSW-01 port while watching.
    antenna_port: int | str | None = "direct"
    data_dir: str = "/data/archive/pirate-watch"

    # sweep (rtl_power)
    start_hz: int = 243_000_000
    stop_hz: int = 270_000_000
    bin_hz: int = 10_000                    # rtl_power rounds to a power-of-two FFT: 10 kHz asked = 5.07 kHz bins (measured)
    integration_s: int = 2                  # rtl_power -i (whole seconds); one full sweep per interval
    crop: float = 0.2                       # drop each hop's edges (tuner roll-off)
    gain_db: float | None = 16.6            # fixed gain; None = tuner AGC (clips on R05)
    ppm: int = 0
    history_days: float = 14.0

    # baseline and detection
    learn_sweeps: int = 600                 # ~20-25 min at 2 s; persists across runs
    threshold_db: float = 8.0
    duty_mask: float = 0.5                  # bins above threshold this often while learning are steady carriers
    min_width_hz: float = 15_000
    max_width_hz: float = 25_000
    channel_raster_hz: int = 5_000
    # interference combs never to record on, each [a tooth Hz, spacing Hz, half width Hz]: every tooth
    # (a + k*spacing) across the band is masked at detection, learned or not
    comb_masks: list = field(default_factory=list)

    # recording (rtl_fm)
    audio_bw_hz: int = 25_000
    silence_s: float = 10.0
    max_clip_s: float = 300.0               # a clip that never goes quiet is a steady carrier: masked afterwards
    stall_s: float = 15.0                   # rtl_power / rtl_fm silent this long = the receiver went away (USB drop)
    squelch_hf_cut_hz: float = 5_000.0
    squelch_quiet_ratio: float = 0.5
    squelch_noise_ref: float = 6_300.0       # open-channel HF level on R05 (measured); adapts while recording

    # transcription
    whisper_python: str = "~/.local/share/starlab/whisper-venv/bin/python"
    whisper_model: str = "large-v3"
    whisper_device: str = "cuda"
    whisper_compute_type: str = "float16"
    no_speech_threshold: float = 0.6
    no_speech_keep_h: float = 24.0          # keep the audio of clips Whisper calls non-speech this long, to check its calls

    # broker
    broker_url: str = "http://127.0.0.1:8751"
    broker_token_file: str = "~/.config/observatory/broker.env"
    lease_owner: str = "pirate-watch"
    lease_ttl_s: int = 120
    renew_every_s: float = 5.0
    retry_lease_s: float = 60.0

    # control endpoint for the Slack agent (release / resume / status); same bearer token as the broker
    control_host: str = ""                 # empty: this computer's Tailscale IPv4 (tailscale_ipv4), else 127.0.0.1
    control_port: int = 8752
    control_local: bool = True              # also listen on 127.0.0.1, for Starlab's web page (through the broker)

    tool_prefix: str | None = None          # directory holding rtl_power/rtl_fm (mocks in tests)
    extra: dict = field(default_factory=dict)

    @property
    def root(self) -> pathlib.Path:
        return pathlib.Path(self.data_dir).expanduser()

    def path(self, *parts: str) -> pathlib.Path:
        return self.root.joinpath(*parts)

    def static_ranges(self) -> list[tuple[float, float]]:
        """The comb teeth as (lo Hz, hi Hz), across start_hz-stop_hz."""
        out: list[tuple[float, float]] = []
        for comb in self.comb_masks:
            if not (isinstance(comb, (list, tuple)) and len(comb) == 3 and all(isinstance(v, (int, float)) for v in comb)):
                raise ValueError(f"comb_masks entries are [tooth_hz, spacing_hz, half_width_hz], not {comb!r}")
            tooth, spacing, half = (float(v) for v in comb)
            if spacing <= 0 or half <= 0 or half * 2 >= spacing:
                raise ValueError(f"comb_masks {comb!r}: spacing must be > 0 and wider than the teeth")
            k = math.ceil((self.start_hz - half - tooth) / spacing)
            while (c := tooth + k * spacing) - half <= self.stop_hz:
                out.append((c - half, c + half))
                k += 1
        return out

    @property
    def lease_port(self) -> int | None:
        """The ANTSW-01 port to ask the broker for; None when the receiver is wired directly."""
        if self.antenna_port is None or self.antenna_port == "direct":
            return None
        if isinstance(self.antenna_port, bool) or not isinstance(self.antenna_port, int):
            raise ValueError(f'antenna_port must be "direct" or a switch port number, not {self.antenna_port!r}')
        return self.antenna_port


def tailscale_ipv4() -> str | None:
    """This computer's Tailscale IPv4 address (`tailscale ip -4`), or None when Tailscale is not installed or up."""
    import subprocess
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    addrs = out.stdout.split() if out.returncode == 0 else []
    return addrs[0] if addrs else None


def load(path: pathlib.Path | None = None) -> Settings:
    path = path or DEFAULT_CONFIG
    raw: dict = {}
    if path.exists():
        with path.open("rb") as f:
            raw = tomllib.load(f).get("piratewatch", {})
    known = {f.name for f in dataclasses.fields(Settings)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"{path}: unknown [piratewatch] settings: {', '.join(unknown)}")
    s = Settings(**raw)
    if not s.control_host:  # Tailscale only, without writing this computer's address into the code
        s.control_host = tailscale_ipv4() or "127.0.0.1"
    s.lease_port            # validates antenna_port
    s.static_ranges()       # validates comb_masks
    return s
