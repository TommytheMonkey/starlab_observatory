"""How good a decoded SSTV image and its audio are.

Decode-quality score (0-1): a real picture changes slowly from one scan line to the next, noise does not. The score
is the median correlation of each line with the next (after removing each line's mean), over the image's luminance,
scaled down when the image is nearly flat (a blank frame correlates perfectly but shows nothing). Received noise
decodes to ~0, a clean picture to ~0.9, a picture through QRM somewhere between. Images below min_quality
(default 0.35) are "mostly noise" and are not kept as decodes.

SNR (dB): SSTV lives in 1100-2300 Hz; the noise reference is the audio just outside it (400-900 and 2500-2900 Hz,
both inside an SSB filter), per Hz.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def image_quality(img: Any) -> dict[str, float]:
    """{"score", "line_correlation", "pixel_std"} for a PIL image."""
    g = np.asarray(img.convert("L"), dtype=np.float64)
    if g.ndim != 2 or g.shape[0] < 8 or g.shape[1] < 8:
        return {"score": 0.0, "line_correlation": 0.0, "pixel_std": 0.0}
    rows = g - g.mean(axis=1, keepdims=True)
    a, b = rows[:-1], rows[1:]
    den = np.sqrt((a * a).sum(axis=1) * (b * b).sum(axis=1))
    ok = den > 1e-9
    corr = float(np.median((a * b).sum(axis=1)[ok] / den[ok])) if ok.any() else 0.0
    std = float(g.std())
    flat = min(1.0, std / 20.0)                    # under ~20 grey levels of variation the picture is (nearly) blank
    score = max(0.0, corr) * flat
    return {"score": round(score, 3), "line_correlation": round(corr, 3), "pixel_std": round(std, 2)}


def audio_snr_db(audio: np.ndarray, rate: int) -> float | None:
    """In-band (1100-2300 Hz) over out-of-band noise density, in dB. None for too little audio."""
    x = np.asarray(audio, dtype=np.float64)
    n = 4096
    if len(x) < n or rate < 6000:
        return None
    segs = x[: (len(x) // n) * n].reshape(-1, n) * np.hanning(n)
    p = (np.abs(np.fft.rfft(segs, axis=1)) ** 2).mean(axis=0)
    f = np.fft.rfftfreq(n, 1 / rate)
    sig = p[(f >= 1100) & (f <= 2300)]
    noise = p[((f >= 400) & (f <= 900)) | ((f >= 2500) & (f <= 2900))]
    if not len(sig) or not len(noise) or noise.mean() <= 0:
        return None
    return round(float(10 * np.log10(max(sig.mean() / noise.mean() - 1.0, 1e-3))), 1)
