"""Carrier-present detection on rtl_fm NFM audio (a noise squelch) and the clip state machine.

rtl_fm's FM discriminator outputs the phase step between samples, so with no carrier its
output is wideband noise of nearly constant level, whatever the gain (about 6300 above 5 kHz
on OBS-R05 at 25 kHz, measured 2026-11-11; theory without rtl_fm's filtering gives 16384/sqrt(3)). A carrier quiets
that noise, and voice sits below ~3.4 kHz. The squelch therefore watches the audio power
above `hf_cut_hz`: low = carrier present, high = open channel noise. The noise reference
adapts to what this receiver actually produces on an empty channel.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass, field

import numpy as np

BLOCK_S = 0.1


class NoiseSquelch:
    def __init__(self, rate: int, hf_cut_hz: float = 5_000.0, quiet_ratio: float = 0.5,
                 noise_ref: float = 6_300.0):
        self.rate = rate
        self.block = int(rate * BLOCK_S)
        self.hf_cut_hz = hf_cut_hz
        self.quiet_ratio = quiet_ratio
        self.noise_ref = noise_ref
        freqs = np.fft.rfftfreq(self.block, 1 / rate)
        self._hf = freqs >= hf_cut_hz
        self._window = np.hanning(self.block)
        self._norm = np.sqrt(np.sum(self._window ** 2) * self.block / 2)

    def hf_rms(self, block: np.ndarray) -> float:
        spec = np.fft.rfft(block.astype(np.float64) * self._window)
        hf_power = np.sum(np.abs(spec[self._hf]) ** 2)
        frac = self._hf.mean()
        return float(np.sqrt(hf_power / max(frac, 1e-9)) / self._norm)

    def present(self, block: np.ndarray) -> tuple[bool, float]:
        level = self.hf_rms(block)
        quiet = level < self.noise_ref * self.quiet_ratio
        if level > self.noise_ref * self.quiet_ratio * 1.3:   # clearly open-channel noise: track it, up or down
            self.noise_ref = 0.95 * self.noise_ref + 0.05 * level
        return quiet, level


@dataclass
class ClipState:
    """Feeds 0.1 s blocks; done when `silence_s` passes with no carrier (or `max_s` is hit)."""
    squelch: NoiseSquelch
    silence_s: float = 10.0
    max_s: float = 300.0
    elapsed: float = 0.0
    first_present: float | None = None
    last_present: float | None = None
    present_blocks: int = 0
    levels: list[float] = field(default_factory=list)
    _buf: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int16))

    def feed(self, pcm: np.ndarray) -> list[np.ndarray]:
        """Returns the whole blocks consumed (to write to the WAV)."""
        self._buf = np.concatenate([self._buf, pcm])
        n = self.squelch.block
        blocks = []
        while len(self._buf) >= n and not self.done:
            block, self._buf = self._buf[:n], self._buf[n:]
            present, level = self.squelch.present(block)
            self.levels.append(level)
            if present:
                if self.first_present is None:
                    self.first_present = self.elapsed
                self.last_present = self.elapsed + BLOCK_S
                self.present_blocks += 1
            self.elapsed += BLOCK_S
            blocks.append(block)
        return blocks

    @property
    def quiet_for(self) -> float:
        return self.elapsed - (self.last_present if self.last_present is not None else 0.0)

    @property
    def done(self) -> bool:
        return self.quiet_for >= self.silence_s or self.elapsed >= self.max_s

    @property
    def hit_max(self) -> bool:
        return self.elapsed >= self.max_s and self.quiet_for < self.silence_s

    @property
    def active_s(self) -> float:
        if self.first_present is None or self.last_present is None:
            return 0.0
        return self.last_present - self.first_present


def write_wav(path: str, pcm: np.ndarray, rate: int) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype("<i2").tobytes())
