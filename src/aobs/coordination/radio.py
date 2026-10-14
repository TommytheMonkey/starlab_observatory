"""Command builders and parsers for the rtl-sdr tools the broker drives.

Everything here is pure (no subprocesses), so it can be unit-tested: the
broker runs the commands and hands the output back to these parsers.
"""

from __future__ import annotations

import statistics
import wave
from dataclasses import dataclass
from typing import Any

MIN_HZ = 24_000_000          # R820T/R860 tuner floor; below it needs direct sampling
MAX_HZ = 1_766_000_000
DIRECT_MAX_HZ = 28_800_000   # direct sampling covers up to half the 28.8 MHz ADC rate
MAX_SWEEP_BINS = 8192

AUDIO_MODES = {
    # mode -> (rtl_fm -M, rtl_fm -s, output WAV rate, extra flags).
    # Never pass -r: rtl_fm's arbitrary resampler dies with SIGFPE (seen live 2026-10-12),
    # so every mode outputs at its own -s, except wbfm, which decimates internally to -r.
    "nfm": ("fm", 16_000, 16_000, []),
    "fm": ("fm", 32_000, 32_000, []),
    "wfm": ("wbfm", 170_000, 32_000, ["-r", "32000"]),
    "am": ("am", 16_000, 16_000, []),
    "usb": ("usb", 16_000, 16_000, []),
    "lsb": ("lsb", 16_000, 16_000, []),
}
AUDIO_RATE = 16_000          # what Whisper wants; it resamples anything else itself


class RadioError(ValueError):
    pass


def check_range(hz: int, direct: bool) -> None:
    if direct:
        if not 0 < hz <= DIRECT_MAX_HZ:
            raise RadioError(f"{hz} Hz is outside direct-sampling range (0-{DIRECT_MAX_HZ} Hz)")
    elif not MIN_HZ <= hz <= MAX_HZ:
        raise RadioError(f"{hz} Hz is outside the tuner range ({MIN_HZ}-{MAX_HZ} Hz); use direct sampling for HF")


# R820T2 gain steps (dB); the rtl tools snap to the nearest one. None = tuner AGC.
GAIN_STEPS = (0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28.0,
              29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6)


def check_gain(gain_db: float | None) -> None:
    if gain_db is not None and not 0 <= gain_db <= 49.6:
        raise RadioError("gain_db must be 0-49.6 dB, or null for automatic")


def gain_args(gain_db: float | None) -> list[str]:
    return [] if gain_db is None else ["-g", f"{gain_db:g}"]


@dataclass(frozen=True)
class Sweep:
    start_hz: int
    stop_hz: int
    bin_hz: int
    seconds: float = 10.0
    gain_db: float | None = None
    direct: bool = False
    crop: float = 0.0     # fraction of each tuning step's edges to discard (0.2-0.5 removes roll-off artifacts)
    ppm: int = 0

    def validate(self) -> None:
        if self.stop_hz <= self.start_hz:
            raise RadioError("stop_hz must be above start_hz")
        check_range(self.start_hz, self.direct)
        check_range(self.stop_hz, self.direct)
        check_gain(self.gain_db)
        if not 0 <= self.crop <= 0.6:
            raise RadioError("crop must be 0-0.6")
        if not 1 <= self.bin_hz <= 2_800_000:
            raise RadioError("bin_hz must be 1 Hz-2.8 MHz")
        if (self.stop_hz - self.start_hz) / self.bin_hz > MAX_SWEEP_BINS:
            raise RadioError(f"More than {MAX_SWEEP_BINS} bins; widen bin_hz or narrow the range")
        if not 1 <= self.seconds <= 300:
            raise RadioError("seconds must be 1-300")

    def command(self, device: str) -> list[str]:
        cmd = ["rtl_power", "-f", f"{self.start_hz}:{self.stop_hz}:{self.bin_hz}", "-i", f"{self.seconds:g}",
               "-1", "-d", device, *gain_args(self.gain_db)]
        if self.crop:
            cmd += ["-c", f"{round(self.crop * 100)}%"]
        if self.ppm:
            cmd += ["-p", str(self.ppm)]
        if self.direct:
            cmd.append("-D")
        return cmd + ["-"]


def parse_rtl_power(text: str) -> list[tuple[int, float]]:
    """rtl_power CSV rows -> sorted (hz, dB) bins. Each row: date, time, low, high, step, samples, dB..."""
    bins: dict[int, float] = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            low, step = float(parts[2]), float(parts[4])
            values = [float(v) for v in parts[6:]]
        except ValueError:
            continue
        for i, db in enumerate(values):
            if db == db:  # NaN check
                bins[int(round(low + (i + 0.5) * step))] = db
    return sorted(bins.items())


def find_peaks(bins: list[tuple[int, float]], threshold_db: float = 8.0, max_peaks: int = 40) -> dict[str, Any]:
    """Noise floor (median) plus contiguous runs of bins at least threshold_db above it."""
    if not bins:
        return {"noise_floor_db": None, "peaks": []}
    floor = statistics.median(db for _, db in bins)
    runs: list[list[tuple[int, float]]] = []
    for hz, db in bins:
        if db >= floor + threshold_db:
            if runs and runs[-1] and hz - runs[-1][-1][0] <= 1.5 * _step(bins):
                runs[-1].append((hz, db))
            else:
                runs.append([(hz, db)])
    peaks = []
    for run in runs:
        top_hz, top_db = max(run, key=lambda b: b[1])
        peaks.append({"center_hz": top_hz, "power_db": round(top_db, 1), "snr_db": round(top_db - floor, 1),
                      "bandwidth_hz": max(_step(bins), run[-1][0] - run[0][0] + _step(bins))})
    peaks.sort(key=lambda p: p["snr_db"], reverse=True)
    return {"noise_floor_db": round(floor, 1), "peaks": peaks[:max_peaks]}


def _step(bins: list[tuple[int, float]]) -> int:
    return bins[1][0] - bins[0][0] if len(bins) > 1 else 1


@dataclass(frozen=True)
class AudioCapture:
    freq_hz: int
    mode: str = "nfm"
    seconds: float = 15.0
    gain_db: float | None = None
    squelch: int = 0
    direct: bool = False
    bandwidth_hz: int | None = None   # rtl_fm -s: channel filter width (and WAV rate); None = mode default
    ppm: int = 0

    def validate(self) -> None:
        if self.mode not in AUDIO_MODES:
            raise RadioError(f"mode must be one of {sorted(AUDIO_MODES)}")
        check_range(self.freq_hz, self.direct)
        check_gain(self.gain_db)
        if not 1 <= self.seconds <= 300:
            raise RadioError("seconds must be 1-300")
        if self.bandwidth_hz is not None and self.mode != "wfm" and not 4_000 <= self.bandwidth_hz <= 64_000:
            raise RadioError("bandwidth_hz must be 4-64 kHz (wfm is fixed at 170 kHz)")
        if not -100 <= self.ppm <= 100:
            raise RadioError("ppm must be -100..100")
        if not 0 <= self.squelch <= 2000:
            raise RadioError("squelch must be 0-2000 (0 = off)")

    @property
    def rate(self) -> int:
        default = AUDIO_MODES[self.mode][1]
        return default if self.mode == "wfm" or self.bandwidth_hz is None else self.bandwidth_hz

    @property
    def wav_rate(self) -> int:
        return AUDIO_MODES[self.mode][2] if self.mode == "wfm" else self.rate

    def command(self, device: str) -> list[str]:
        mod, _, _, extra = AUDIO_MODES[self.mode]
        cmd = ["rtl_fm", "-f", str(self.freq_hz), "-M", mod, "-s", str(self.rate),
               "-d", device, "-E", "dc", *gain_args(self.gain_db), *extra]
        if self.mode == "fm":
            cmd += ["-E", "deemp"]
        if self.ppm:
            cmd += ["-p", str(self.ppm)]
        if self.squelch:
            cmd += ["-l", str(self.squelch)]  # squelched audio is dropped, so the WAV holds only activity
        if self.direct:
            cmd += ["-E", "direct2"]
        return cmd + ["-"]


def write_wav(path: str, pcm_s16le: bytes, rate: int = AUDIO_RATE) -> float:
    """Wrap rtl_fm's raw mono s16le output in a WAV header. Returns duration in seconds."""
    pcm = pcm_s16le[: len(pcm_s16le) // 2 * 2]
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return len(pcm) / 2 / rate


@dataclass(frozen=True)
class IqCapture:
    freq_hz: int
    sample_rate: int = 2_048_000
    seconds: float = 5.0
    gain_db: float | None = None

    def validate(self) -> None:
        check_range(self.freq_hz, False)
        if not 225_001 <= self.sample_rate <= 3_200_000:
            raise RadioError("sample_rate must be 225 kHz-3.2 MHz")
        if not 0.1 <= self.seconds <= 60:
            raise RadioError("seconds must be 0.1-60 (IQ is ~4 MB/s at 2 Msps)")

    def command(self, device: str, path: str) -> list[str]:
        samples = int(self.sample_rate * self.seconds)
        return ["rtl_sdr", "-f", str(self.freq_hz), "-s", str(self.sample_rate), "-n", str(samples),
                "-d", device, *gain_args(self.gain_db), path]
