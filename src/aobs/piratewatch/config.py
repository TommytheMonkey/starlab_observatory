"""Settings for the satcom pirate watch: defaults here, overrides from a TOML file ([piratewatch] table)."""

from __future__ import annotations

import dataclasses
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
    gain_db: float | None = 38.6            # fixed gain; None = tuner AGC (clips on R05)
    ppm: int = 0
    history_days: float = 14.0

    # baseline and detection
    learn_sweeps: int = 600                 # ~20-25 min at 2 s; persists across runs
    threshold_db: float = 8.0
    duty_mask: float = 0.5                  # bins above threshold this often while learning are steady carriers
    min_width_hz: float = 15_000
    max_width_hz: float = 25_000
    channel_raster_hz: int = 5_000

    # recording (rtl_fm)
    audio_bw_hz: int = 25_000
    silence_s: float = 10.0
    max_clip_s: float = 300.0               # a clip that never goes quiet is a steady carrier: masked afterwards
    squelch_hf_cut_hz: float = 5_000.0
    squelch_quiet_ratio: float = 0.5
    squelch_noise_ref: float = 6_300.0       # open-channel HF level on R05 (measured); adapts while recording

    # transcription
    whisper_python: str = "~/.local/share/starlab/whisper-venv/bin/python"
    whisper_model: str = "large-v3"
    whisper_device: str = "cuda"
    whisper_compute_type: str = "float16"
    no_speech_threshold: float = 0.6

    # broker
    broker_url: str = "http://127.0.0.1:8751"
    broker_token_file: str = "~/.config/observatory/broker.env"
    lease_owner: str = "pirate-watch"
    lease_ttl_s: int = 120
    renew_every_s: float = 5.0
    retry_lease_s: float = 60.0

    # control endpoint for the Slack agent (release / resume / status); same bearer token as the broker
    control_host: str = "100.64.49.37"     # observatory's Tailscale address only
    control_port: int = 8752

    tool_prefix: str | None = None          # directory holding rtl_power/rtl_fm (mocks in tests)
    extra: dict = field(default_factory=dict)

    @property
    def root(self) -> pathlib.Path:
        return pathlib.Path(self.data_dir).expanduser()

    def path(self, *parts: str) -> pathlib.Path:
        return self.root.joinpath(*parts)

    @property
    def lease_port(self) -> int | None:
        """The ANTSW-01 port to ask the broker for; None when the receiver is wired directly."""
        if self.antenna_port is None or self.antenna_port == "direct":
            return None
        if isinstance(self.antenna_port, bool) or not isinstance(self.antenna_port, int):
            raise ValueError(f'antenna_port must be "direct" or a switch port number, not {self.antenna_port!r}')
        return self.antenna_port


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
    s.lease_port            # validates antenna_port
    return s
