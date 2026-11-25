"""Streaming IQ -> channel audio for one SSTV channel: USB, LSB or FM.

The receiver is tuned `offset_hz` away from the channel (so its DC spike is off the signal); the stream is shifted
to put the channel at 0 Hz, low-passed and decimated to 4 x audio_rate (48 kHz) and then to audio_rate (12 kHz),
keeping the complex baseband (the transmission's IQ, saved with it). SSB audio is the real part of the baseband after
a complex band-pass on the right sideband (300-3000 Hz above the dial for USB, below it for LSB), so the other
sideband never folds in. FM is a discriminator at 48 kHz, then decimated.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.signal import firwin, lfilter

from ..scheduling.sstvdec import Decimator


def _stages(in_rate: int, audio_rate: int) -> list[int]:
    """Integer decimations from in_rate to audio_rate via 4 x audio_rate (e.g. 768 k -> 48 k -> 12 k)."""
    if in_rate % audio_rate:
        raise ValueError(f"the sample rate {in_rate} must be a multiple of the audio rate {audio_rate}")
    total = in_rate // audio_rate
    if total % 4 == 0 and total > 4:
        return [total // 4, 4]
    return [total]


class ChannelDemod:
    def __init__(self, in_rate: int, offset_hz: float, mode: str, audio_rate: int = 12_000,
                 low_hz: float = 300.0, high_hz: float = 3_000.0):
        if mode not in ("usb", "lsb", "fm"):
            raise ValueError(f"mode must be usb, lsb or fm, not {mode!r}")
        self.in_rate, self.mode, self.audio_rate = int(in_rate), mode, int(audio_rate)
        self.offset_hz = float(offset_hz)
        self.stages = _stages(self.in_rate, self.audio_rate)
        self.decs, rate = [], self.in_rate
        for i, m in enumerate(self.stages):
            out = rate / m
            # FM keeps ±8 kHz through the first stage (the discriminator runs at 48 kHz); SSB needs only ±3.5 kHz
            keep = 9_000.0 if (mode == "fm" and i == 0) else min(out * 0.4, 4_000.0)
            taps = int(6 * rate / max(out / 2 - keep, out * 0.08)) | 1
            self.decs.append(Decimator(firwin(taps, keep, fs=rate, window=("kaiser", 7.0)), m))
            rate = out
        self.mid_rate = self.in_rate / self.stages[0]
        self.n = 0
        self.phase = 0.0
        # sideband filter: a real low-pass of half the passband width, shifted to the passband's centre
        self.sb_shift = (low_hz + high_hz) / 2 * (1 if mode == "usb" else -1)
        self.sb_h = firwin(129, (high_hz - low_hz) / 2, fs=self.audio_rate, window=("kaiser", 6.0))
        self.sb_zi = np.zeros(len(self.sb_h) - 1, np.complex128)
        self.sb_n = 0
        self.fm_prev = np.complex64(0)
        self.fm_dec = Decimator(firwin(129, 3_000, fs=self.mid_rate), self.stages[1]) if (
            mode == "fm" and len(self.stages) > 1) else None

    def __call__(self, iq: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """One block of receiver IQ -> (channel baseband at audio_rate, complex64; audio at audio_rate, float32)."""
        x = np.asarray(iq, dtype=np.complex64)
        if self.offset_hz:
            step = -2 * math.pi * self.offset_hz / self.in_rate
            ph = self.phase + step * np.arange(len(x))
            x = x * np.exp(1j * ph).astype(np.complex64)
            self.phase = float((self.phase + step * len(x)) % (2 * math.pi))
        y = self.decs[0](x)
        if self.mode == "fm":
            d = np.angle(y * np.conj(np.concatenate([[self.fm_prev], y[:-1]]))) if len(y) else np.zeros(0)
            if len(y):
                self.fm_prev = y[-1]
            hz = (d * self.mid_rate / (2 * math.pi)).astype(np.float32)
            base = y
            for dec in self.decs[1:]:
                base = dec(base)
            audio = (self.fm_dec(hz.astype(np.complex64)).real if self.fm_dec else hz) / 8_000.0
            return base, np.clip(audio, -1, 1).astype(np.float32)
        for dec in self.decs[1:]:
            y = dec(y)
        if not len(y):
            return y, np.zeros(0, np.float32)
        t = (self.sb_n + np.arange(len(y))) / self.audio_rate
        rot = np.exp(-2j * math.pi * self.sb_shift * t)
        z, self.sb_zi = lfilter(self.sb_h, 1.0, y * rot, zi=self.sb_zi)
        audio = (z * np.conj(rot)).real * 2
        self.sb_n += len(y)
        return y, audio.astype(np.float32)
