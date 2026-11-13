"""SSTV decoding of a recording's raw IQ (the ISS's ARISS events), alongside SatDump.

Recordings choose this decoder with the built-in pipeline name `sstv` (the ISS catalog entry and its decoder
profile do), so bookings, rules, profiles and decode requests treat it like any SatDump pipeline.

DSP, streamed so a whole pass never sits in memory:
  1. read raw IQ (cu8/cs8/cs16/cf32 per the recording), shift the target frequency to 0 Hz;
  2. low-pass ±20 kHz and decimate to about 100 kS/s (wide enough that ±10 kHz Doppler at 437 MHz needs no
     correction), into a temporary audio-rate file;
  3. FM-demodulate by the angle between successive samples (quadrature discriminator), no de-emphasis;
  4. resample to 48 kHz mono, DC-block (this also removes the slowly drifting Doppler offset), write a WAV.

Decoding runs an SSTV library over the WAV and keeps every image of the pass: ARISS sends one every 2-3
minutes, each with its own VIS header. Two libraries are understood, whichever is installed (both install as
the `sstv` module, so only one can be):
  * `sstv` from PyPI (unexcellent/sstv-py, Rust): every Robot, Martin, Scottie, Pasokon and PD mode, and it
    returns every image in the file itself. This is the one Starlab installs.
  * colaclanth/sstv (GitHub): Martin, Scottie and Robot 36/72 only (no PD120/PD180), one image per call, so
    this module walks the audio header by header.

No image is a normal outcome: the ISS sends SSTV only during announced events.
"""

from __future__ import annotations

import datetime as dt
import math
import pathlib
import wave
from fractions import Fraction
from typing import Any

import numpy as np

PIPELINE = "sstv"
AUDIO_RATE = 48_000
CHANNEL_HALF_BW_HZ = 20_000          # ±10 kHz Doppler at 437 MHz plus the ~±5 kHz FM signal itself
IF_RATE_TARGET = 100_000             # the demodulator's rate; resampled to 48 kHz afterwards
FULL_SCALE_HZ = 8_000                # audio full scale: 8 kHz of deviation (after the DC block)
DC_POLE = 0.999                      # DC-block pole at 48 kHz: corner ~8 Hz
AUDIO_LOWPASS_HZ: float | None = 3_000   # SSTV tones are 1.1-2.3 kHz; FM noise rises with audio frequency
NO_IMAGES = "no SSTV images (normal when no SSTV event is running)"


# ---------------------------------------------------------------- which decoder library
def library() -> dict[str, Any] | None:
    """The installed SSTV decoder: {"name", "version", "modes"}, or None."""
    try:
        import sstv  # noqa: F401
    except Exception:  # noqa: BLE001 - not installed, or broken
        return None
    import importlib.metadata as md
    try:
        version = md.version("sstv")
    except md.PackageNotFoundError:
        version = None
    if hasattr(sstv, "decode_from_wav"):
        return {"name": "sstv (unexcellent/sstv-py)", "kind": "rust", "version": version,
                "modes": "Robot, Martin, Scottie, Wrasse, Pasokon, PD 50-290"}
    try:
        from sstv.decode import SSTVDecoder  # noqa: F401
    except Exception:  # noqa: BLE001
        return None
    return {"name": "sstv (colaclanth)", "kind": "colaclanth", "version": version,
            "modes": "Martin 1/2, Scottie 1/2/DX, Robot 36/72 (no PD modes)"}


def available() -> bool:
    return library() is not None


# ---------------------------------------------------------------- streaming DSP
class Decimator:
    """Stateful FIR low-pass + decimate-by-m that computes only the kept outputs (scipy upfirdn per chunk).

    The filter history is a multiple of m long (h is zero-padded), so every chunk's output grid lines up with
    the global one and chunk edges leave no trace."""

    def __init__(self, h: np.ndarray, m: int):
        self.m = int(m)
        q = self.m * math.ceil((len(h) - 1) / self.m)
        self.h = np.concatenate([h, np.zeros(q + 1 - len(h))]).astype(np.float32)
        self.q = q
        self.hist = np.zeros(q, np.complex64)
        self.n = 0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        from scipy.signal import upfirdn
        z = np.concatenate([self.hist, x.astype(np.complex64)])
        i0 = (-self.n) % self.m                     # first new sample on the global output grid
        want = max(0, math.ceil((len(x) - i0) / self.m))
        out = upfirdn(self.h, z[i0:], down=self.m)[self.q // self.m: self.q // self.m + want] if want else \
            np.zeros(0, np.complex64)
        self.hist = z[len(z) - self.q:] if self.q else self.hist
        self.n += len(x)
        return out.astype(np.complex64)


def channel_plan(in_rate: int, center_hz: int, target_hz: int) -> dict[str, Any]:
    """Where the target is and how it is brought down to the demodulator's rate. Raises ValueError if the
    ±20 kHz channel is not inside the recording."""
    offset = int(target_hz) - int(center_hz)
    usable = in_rate / 2 * 0.9
    if abs(offset) + CHANNEL_HALF_BW_HZ > usable:
        raise ValueError(f"the SSTV channel ({target_hz / 1e6:.4f} MHz ±{CHANNEL_HALF_BW_HZ / 1e3:.0f} kHz) is not inside "
                         f"the recorded band ({center_hz / 1e6:.4f} MHz ±{usable / 1e3:.0f} kHz)")
    m = max(1, round(in_rate / IF_RATE_TARGET))
    if_rate = in_rate / m
    if if_rate < 2.2 * CHANNEL_HALF_BW_HZ:
        raise ValueError(f"sample rate {in_rate} S/s is too low for a ±{CHANNEL_HALF_BW_HZ / 1e3:.0f} kHz SSTV channel")
    ratio = Fraction(AUDIO_RATE / if_rate).limit_denominator(2000)
    return {"input_sample_rate_hz": int(in_rate), "center_frequency_hz": int(center_hz), "target_frequency_hz": int(target_hz),
            "frequency_offset_hz": offset, "channel_half_bandwidth_hz": CHANNEL_HALF_BW_HZ, "decimation": m,
            "if_sample_rate_hz": if_rate, "audio_sample_rate_hz": AUDIO_RATE,
            "resample": [ratio.numerator, ratio.denominator], "de_emphasis": False}


def iq_to_wav(src: pathlib.Path, fmt: str, plan: dict[str, Any], wav: pathlib.Path, start_s: float | None = None,
              end_s: float | None = None, chunk: int = 1 << 20, progress: Any = None) -> dict[str, Any]:
    """Raw IQ -> FM-demodulated 48 kHz mono WAV (int16). Returns sample counts and levels."""
    from scipy.signal import firwin, lfilter, resample_poly

    from .dsp import DTYPES, to_complex
    rate, m = plan["input_sample_rate_hz"], plan["decimation"]
    if_rate = plan["if_sample_rate_hz"]
    dtype, comps = DTYPES[fmt]
    bps = np.dtype(dtype).itemsize * comps
    total = src.stat().st_size // bps
    a = int((start_s or 0) * rate)
    b = min(total, int(end_s * rate)) if end_s else total
    if not 0 <= a < b:
        raise ValueError("time range is outside the recording")
    # stage 1: shift, low-pass ±20 kHz (stop band by the IF Nyquist), decimate; FM-demodulate at the IF rate
    trans = max(4_000.0, min(if_rate / 2 - CHANNEL_HALF_BW_HZ, 25_000.0))
    ntaps = int(4.0 * rate / trans) | 1
    h = firwin(ntaps, CHANNEL_HALF_BW_HZ + trans / 2, fs=rate, window=("kaiser", 7.0)) if m > 1 else np.array([1.0])
    dec = Decimator(h, m)
    step = -2.0 * np.pi * plan["frequency_offset_hz"] / rate
    demod_path = wav.with_name("." + wav.stem + ".fm.f32")
    prev = np.complex64(0)
    n_in = n_if = 0
    peak_iq = 0.0
    try:
        with src.open("rb") as fi, demod_path.open("wb") as fo:
            fi.seek(a * bps)
            left = b - a
            while left > 0:
                raw = fi.read(min(left, chunk) * bps)
                if not raw:
                    break
                x = to_complex(raw, fmt)
                if plan["frequency_offset_hz"]:
                    idx = np.arange(n_in, n_in + len(x), dtype=np.float64)
                    x = x * np.exp(1j * np.mod(step * idx, 2 * np.pi))
                y = dec(x)
                if len(y):
                    peak_iq = max(peak_iq, float(np.abs(y).max()))
                    d = np.angle(y * np.conj(np.concatenate([[prev], y[:-1]])))    # rad/sample
                    prev = y[-1]
                    fo.write((d * (if_rate / (2 * np.pi))).astype(np.float32).tobytes())   # instantaneous Hz
                    n_if += len(y)
                n_in += len(x)
                left -= len(x)
                if progress:
                    progress(n_in / max(1, b - a))
        # stage 2: resample to 48 kHz in overlapping segments (aligned to the polyphase grid), DC-block, WAV
        up, down = plan["resample"]
        fm = np.memmap(demod_path, dtype=np.float32, mode="r") if n_if else np.zeros(0, np.float32)
        seg = down * max(1, (int(if_rate * 30)) // down)           # ~30 s per segment
        pad = down * max(1, int(if_rate * 0.05) // down)           # 50 ms of context on each side
        zi = np.zeros(1)
        lp = firwin(255, AUDIO_LOWPASS_HZ, fs=AUDIO_RATE) if AUDIO_LOWPASS_HZ else None
        lzi = np.zeros(254) if lp is not None else None
        n_audio = 0
        clipped = 0
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(AUDIO_RATE)
            for s0 in range(0, len(fm), seg):
                lo, hi = max(0, s0 - pad), min(len(fm), s0 + seg + pad)
                r = resample_poly(np.asarray(fm[lo:hi], dtype=np.float64), up, down)
                keep_from = (s0 - lo) * up // down
                keep = (min(len(fm), s0 + seg) - s0) * up // down
                r = r[keep_from: keep_from + keep]
                r, zi = lfilter([1.0, -1.0], [1.0, -DC_POLE], r, zi=zi)
                if lp is not None:
                    r, lzi = lfilter(lp, 1.0, r, zi=lzi)
                pcm = np.clip(r / FULL_SCALE_HZ, -1.0, 1.0)
                clipped += int(np.count_nonzero(np.abs(r) >= FULL_SCALE_HZ))
                w.writeframes((pcm * 32767).astype("<i2").tobytes())
                n_audio += len(pcm)
        del fm
    finally:
        demod_path.unlink(missing_ok=True)
    return {"input_samples": n_in, "if_samples": n_if, "audio_samples": n_audio,
            "audio_seconds": round(n_audio / AUDIO_RATE, 2), "channel_peak": round(peak_iq, 4),
            "audio_clipped_samples": clipped, "filter_taps": int(ntaps)}


# ---------------------------------------------------------------- decoding
def decode_wav(wav: pathlib.Path) -> list[dict[str, Any]]:
    """Every SSTV image in the WAV: [{"image": PIL image, "mode": name, "complete": bool, "at_s": seconds or None}]."""
    lib = library()
    if lib is None:
        raise RuntimeError("no SSTV decoder is installed (pip install sstv)")
    import sstv
    if lib["kind"] == "rust":
        out = []
        for im in sstv.decode_from_wav(str(wav)):
            mode = im.info.get("sstv_mode")
            out.append({"image": im, "mode": _mode_name(mode), "complete": bool(im.info.get("sstv_complete", True)),
                        "at_s": None})
        return out
    return _decode_colaclanth(wav)


def _mode_name(mode: Any) -> str:
    text = str(mode or "unknown").split(".")[-1]          # Mode.PD_120 -> PD 120
    return text.replace("_", " ").title().replace("Pd ", "PD ") if text.isupper() else text


def _decode_colaclanth(wav: pathlib.Path) -> list[dict[str, Any]]:
    """colaclanth/sstv decodes the first image after `skip`; walk the audio one VIS header at a time."""
    from sstv import decode as cola, spec
    from sstv.decode import SSTVDecoder
    cola.log_message = cola.progress_bar = lambda *a, **k: None   # its progress output needs a terminal; the worker has none
    out: list[dict[str, Any]] = []
    with wav.open("rb") as fh, SSTVDecoder(fh) as d:        # it wants an open file (it closes it itself)
        full, sr = d._samples, d._sample_rate
        if full.ndim > 1:
            full = full.mean(axis=1)
        pos = 0
        while pos < len(full) - int(sr * spec.HDR_SIZE):
            d._samples = full[pos:]
            hdr = d._find_header()
            if hdr is None:
                break
            vis_end = hdr + round(spec.VIS_BIT_SIZE * 9 * sr)
            try:
                d.mode = d._decode_vis(hdr)
            except ValueError as exc:       # bad parity or a mode this library lacks (PD120/PD180): move on
                out.append({"image": None, "mode": None, "complete": False, "at_s": round((pos + hdr) / sr, 1),
                            "error": str(exc)})
                pos += vis_end
                continue
            data = d._decode_image_data(vis_end)
            im = d._draw_image(data)
            out.append({"image": im, "mode": d.mode.NAME, "complete": True, "at_s": round((pos + hdr) / sr, 1)})
            pos += vis_end + round(d.mode.LINE_TIME * d.mode.LINE_COUNT * sr)
    return out


def decode(inp: dict[str, Any], p: dict[str, Any], out: pathlib.Path, tag: str,
           progress: Any = None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """procworker entry: one input file -> WAV (audio artifact) + every decoded image (image artifacts)."""
    from .procworker import _image_content
    t0 = dt.datetime.now(dt.timezone.utc)
    src = pathlib.Path(inp["path"])
    plan = channel_plan(int(inp["sample_rate_hz"]), int(inp["center_frequency_hz"]), int(p["target_frequency_hz"]))
    wav = out / f"sstv{tag}.wav"
    if progress:
        progress("demodulating FM to audio", 0, 2)
    stage = iq_to_wav(src, inp["format"], plan, wav, p.get("start_s"), p.get("end_s"))
    arts: list[dict[str, str]] = [{"kind": "audio", "path": str(wav), "label": "FM audio, 48 kHz mono (WAV)"}]
    if progress:
        progress("decoding SSTV", 1, 2)
    lib = library() or {}
    found = decode_wav(wav)
    checks, errors = [], []
    for n, f in enumerate(found, 1):
        if f["image"] is None:
            where = f" at {f['at_s']} s" if f.get("at_s") is not None else ""
            errors.append(f"transmission {n}{where}: {f.get('error') or 'not decoded'}")
            continue
        name = f"sstv{tag}_{n:02d}_{(f['mode'] or 'unknown').replace(' ', '')}.png"
        path = out / name
        f["image"].save(path)
        q = _image_content(path)
        checks.append({"file": name, "mode": f["mode"], "complete": f["complete"], "at_s": f.get("at_s"), **q})
        label = f"SSTV image {n} ({f['mode']})" + ("" if f["complete"] else " (incomplete)") + \
            ("" if q.get("has_content", True) else " (blank)")
        arts.append({"kind": "image", "path": str(path), "label": label})
    real = [c for c in checks if c.get("has_content")]
    res = {"input": str(src), "decoder": PIPELINE, "library": lib.get("name"), "library_version": lib.get("version"),
           "channel_plan": plan, "audio": {**stage, "wav": str(wav)}, "transmissions": len(found),
           "images": len(real), "images_blank": len(checks) - len(real), "image_checks": checks[:40],
           "modes": sorted({c["mode"] for c in checks if c.get("mode")}), "decoded": bool(real),
           "cadu_frames": 0, "errors": errors, "note": None if real else NO_IMAGES,
           "duration_s": round((dt.datetime.now(dt.timezone.utc) - t0).total_seconds(), 1)}
    return res, arts
