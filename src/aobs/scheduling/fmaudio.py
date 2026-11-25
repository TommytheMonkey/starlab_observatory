"""Listenable NBFM audio from a recording's raw IQ: one track per FM channel with activity (brief: audio extraction).

Runs as its own processing attempt (kind=audio, procworker), after the recording and from the backfill script. It only
adds outputs: the SSTV and APRS decoders keep making their own audio and results.

Which channels (tracks):
  * the recording's target frequency and every downlink booked with it (spec metadata.downlinks: on the ISS's 437 MHz
    group capture that is 437.550 SSTV, 437.800 repeater and 437.825 APRS), plus the catalog's FM downlinks for the
    satellite and any `known_channels_hz` from the processing settings, when they are inside the recorded band;
  * channels found by scanning the whole recorded band (scan_band): a coarse per-second spectrum, Doppler-corrected for
    satellite targets, in which a 12.5 kHz channel stood clearly above the noise for long enough. Known channels are
    always extracted (an empty one says "no audio activity"); detected ones only when they had activity.

DSP per track, streamed in chunks (a 2.6-8 GB recording never sits in memory) and run in parallel processes:
  1. frequency shift to 0 Hz, phase-continuous across chunks; for satellites the shift follows the Doppler curve from
     the pass's orbital elements (doppler_curve: the scheduler's Skyfield elements, range rate every second);
  2. decimate to an IF of at least IF_MIN_RATE (sstvdec.Decimator), measure the carrier SNR every BLOCK_S there
     (channel power over the median noise around it);
  3. channel filter (±CHANNEL_HALF_BW_HZ) and resample to 48 kHz in one polyphase stage (Resampler);
  4. FM discriminator, de-emphasis (DEEMPHASIS_US, unity gain at 1 kHz), 300-3000 Hz band-pass, ±DEVIATION_HZ full
     scale with headroom, 16-bit mono WAV; ffmpeg encodes an Opus copy for the browser (MP3 when there is no libopus).
Activity: hysteresis squelch on the block SNR (open SQUELCH_OPEN_DB, close SQUELCH_CLOSE_DB), segments closer than
MERGE_GAP_S merged, shorter than MIN_SEGMENT_S dropped. A trimmed WAV/Opus keeps only the segments, PAD_S each side.

Outputs in <attempt>/: audio.json (every track: files, Doppler, segments, per-block SNR and level timeline) and per track
<label>.wav, <label>.ogg, <label>-trimmed.wav, <label>-trimmed.ogg.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import math
import pathlib
import shutil
import subprocess
import wave
from fractions import Fraction
from typing import Any, Callable

import numpy as np

AUDIO_RATE = 48_000
DEVIATION_HZ = 5_000                 # NBFM: ±5 kHz is full deviation
CHANNEL_HALF_BW_HZ = 6_250           # 12.5 kHz channel filter
CHANNEL_TRANSITION_HZ = 2_500
DEEMPHASIS_US = 530.0                # 6 dB/octave from 300 Hz (TIA-603 NBFM), unity gain at 1 kHz
AUDIO_BAND_HZ = (300.0, 3_000.0)
HEADROOM = 0.7                       # full deviation at 1 kHz -> 0.7 of full scale
IF_MIN_RATE = 150_000                # first decimation keeps at least this rate (noise reference around the channel)
BLOCK_S = 0.1                        # SNR / squelch resolution
SQUELCH_OPEN_DB = 6.0
SQUELCH_CLOSE_DB = 3.0
MERGE_GAP_S = 1.0
MIN_SEGMENT_S = 0.3
PAD_S = 0.5
NOISE_INNER_HZ = 15_000              # the noise reference is the median PSD from here out to 80% of the IF Nyquist
CHUNK = 1 << 20
SPEED_OF_LIGHT = 299_792_458.0
# band scan
SCAN_BIN_HZ = 250.0
SCAN_ACTIVE_DB = 6.0                 # a channel second counts as active above this SNR
SCAN_MIN_ACTIVE_S = 10.0             # ... and a detected channel needs this many active seconds
SCAN_MIN_PEAK_DB = 10.0
SCAN_MIN_IDLE = 0.25                 # ... and must be idle (below 0 dB) this much of the time: always-on carriers are
                                     # receiver spurs or interference (measured on m2026112118302d55eb: spurs idle
                                     # 2-11% of the seconds, the ISS repeater 55-82%)
SCAN_SEPARATION_HZ = 15_000
SCAN_DC_GUARD_HZ = 20_000            # the receiver's DC spike
MAX_DETECTED = 4
MAX_WORKERS = 4                      # channels demodulated in parallel (processes)
PARALLEL_MIN_BYTES = 256 << 20
NO_ACTIVITY = "no audio activity"


# ================================================================ Doppler
def elements_record(e: Any) -> dict[str, Any]:
    """predict.Elements as JSON (stored in the attempt so the Doppler curve can be recomputed exactly)."""
    return {"norad": e.norad, "name": e.name, "format": e.format, "line1": e.line1, "line2": e.line2, "omm": e.omm,
            "epoch": e.epoch.isoformat(), "source": e.source}


def _satellite(rec: dict[str, Any], ts: Any) -> Any:
    from skyfield.api import EarthSatellite
    if rec.get("omm"):
        return EarthSatellite.from_omm(ts, rec["omm"])
    return EarthSatellite(rec["line1"], rec["line2"], rec.get("name") or "", ts)


def range_rate_curve(rec: dict[str, Any], lat: float, lon: float, elevation_m: float, start: dt.datetime,
                     duration_s: float, step_s: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Seconds from start, and the satellite's range rate seen from the station (m/s, positive = receding)."""
    from skyfield.api import load, wgs84
    ts = load.timescale(builtin=True)
    sat = _satellite(rec, ts)
    obs = wgs84.latlon(lat, lon, elevation_m=elevation_m)
    t_s = np.arange(0.0, duration_s + step_s, step_s)
    base = ts.from_datetime(start)
    times = ts.tt_jd(base.tt + t_s / 86400.0)
    *_, rr = (sat - obs).at(times).frame_latlon_and_rates(obs)
    return t_s, np.asarray(rr.km_per_s, dtype=np.float64) * 1000.0


def doppler_hz(range_rate_m_s: np.ndarray | float, freq_hz: float) -> np.ndarray:
    """Received minus transmitted frequency: an approaching satellite (negative range rate) is heard higher."""
    return -np.asarray(range_rate_m_s, dtype=np.float64) / SPEED_OF_LIGHT * float(freq_hz)


def doppler_curve(rec: dict[str, Any], lat: float, lon: float, elevation_m: float, start: dt.datetime,
                  duration_s: float, freq_hz: float, step_s: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Seconds from start and the Doppler shift (Hz) of a downlink at freq_hz."""
    t, rr = range_rate_curve(rec, lat, lon, elevation_m, start, duration_s, step_s)
    return t, doppler_hz(rr, freq_hz)


class Mixer:
    """Phase-continuous shift by -offset(t): offset is linear within a chunk (interpolated from a per-second curve),
    so the phase is its exact integral and chunk edges leave no trace."""

    def __init__(self, rate: int, offset_hz: float, curve: tuple[np.ndarray, np.ndarray] | None = None):
        self.rate, self.offset = float(rate), float(offset_hz)
        self.curve = curve
        self.n = 0
        self.phase = 0.0

    def freq(self, t: float | np.ndarray) -> Any:
        if self.curve is None:
            return self.offset + 0.0 * np.asarray(t)
        return self.offset + np.interp(t, self.curve[0], self.curve[1])

    def __call__(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        f0, f1 = float(self.freq(self.n / self.rate)), float(self.freq((self.n + n) / self.rate))
        k = np.arange(n, dtype=np.float64)
        ph = self.phase + 2 * np.pi / self.rate * (f0 * k + (f1 - f0) * k * k / (2 * n))
        self.phase = math.fmod(self.phase + 2 * np.pi / self.rate * (f0 * n + (f1 - f0) * n / 2), 2 * np.pi)
        self.n += n
        if f0 == 0.0 and f1 == 0.0:
            return x.astype(np.complex64)
        return (x * np.exp(-1j * ph).astype(np.complex64)).astype(np.complex64)


# ================================================================ streaming filters
class Resampler:
    """Stateful rational polyphase resampler (up/down, FIR h designed at in_rate*up), complex or real input.

    The buffered history always starts at a multiple of `down` input samples, so upfirdn's output grid over the
    buffer lines up with the global one: output m is upfirdn output m - n0*up/down."""

    def __init__(self, h: np.ndarray, up: int, down: int):
        self.up, self.down = int(up), int(down)
        self.h = (np.asarray(h, dtype=np.float64) * self.up).astype(np.float32)
        self.buf: np.ndarray | None = None
        self.n0 = 0                      # global input index of buf[0] (a multiple of down)
        self.next_m = 0                  # next output to produce

    def __call__(self, x: np.ndarray) -> np.ndarray:
        from scipy.signal import upfirdn
        self.buf = x if self.buf is None or not len(self.buf) else np.concatenate([self.buf, x])
        end = self.n0 + len(self.buf)
        m_last = ((end - 1) * self.up) // self.down
        if m_last < self.next_m:
            return np.zeros(0, x.dtype)
        y = upfirdn(self.h, self.buf, self.up, self.down)
        j0 = self.next_m - self.n0 * self.up // self.down
        out = y[j0: j0 + (m_last - self.next_m + 1)].astype(x.dtype)
        self.next_m = m_last + 1
        need = max(0, -(-(self.next_m * self.down - (len(self.h) - 1)) // self.up))   # oldest input the next output uses
        keep_from = (need // self.down) * self.down
        if keep_from > self.n0:
            self.buf = self.buf[keep_from - self.n0:]
            self.n0 = keep_from
        return out


def plan_rates(in_rate: int) -> dict[str, Any]:
    """Integer decimation to an IF of at least IF_MIN_RATE, then a rational resample to 48 kHz."""
    m = max(1, int(in_rate // IF_MIN_RATE))
    while m > 1 and in_rate % m:
        m -= 1
    if_rate = in_rate / m
    ratio = Fraction(AUDIO_RATE / if_rate).limit_denominator(4000)
    return {"input_sample_rate_hz": int(in_rate), "decimation": m, "if_sample_rate_hz": if_rate,
            "resample": [ratio.numerator, ratio.denominator], "audio_sample_rate_hz": AUDIO_RATE}


def _deemphasis() -> tuple[np.ndarray, np.ndarray]:
    """First-order de-emphasis (bilinear), scaled to unity gain at 1 kHz."""
    from scipy.signal import bilinear, freqz
    tau = DEEMPHASIS_US * 1e-6
    b, a = bilinear([1.0], [tau, 1.0], fs=AUDIO_RATE)
    _, g = freqz(b, a, worN=[1000.0], fs=AUDIO_RATE)
    return np.asarray(b) / abs(g[0]), np.asarray(a)


# ================================================================ one track
def _snr_block(seg: np.ndarray, if_rate: float, nfft: int, win: np.ndarray) -> float:
    """Carrier SNR (dB) of one block at the IF: power within ±CHANNEL_HALF_BW_HZ over the median noise around it."""
    n = len(seg) // nfft
    if n < 1:
        return float("nan")
    s = seg[: n * nfft].reshape(n, nfft) * win
    p = np.mean(np.abs(np.fft.fft(s, axis=1)) ** 2, axis=0)
    f = np.abs(np.fft.fftfreq(nfft, 1.0 / if_rate))
    ch = f <= CHANNEL_HALF_BW_HZ
    ref = (f >= NOISE_INNER_HZ) & (f <= 0.8 * if_rate / 2)
    noise = float(np.median(p[ref])) if ref.any() else float(np.median(p))
    if noise <= 0:
        return float("nan")
    snr = (float(p[ch].sum()) - noise * int(ch.sum())) / (noise * int(ch.sum()))
    return 10 * math.log10(max(snr, 1e-3))


def segments_from_snr(snr_db: np.ndarray, block_s: float = BLOCK_S, open_db: float = SQUELCH_OPEN_DB,
                      close_db: float = SQUELCH_CLOSE_DB, merge_gap_s: float = MERGE_GAP_S,
                      min_s: float = MIN_SEGMENT_S) -> list[dict[str, float]]:
    """Hysteresis squelch over per-block SNR -> [{"start_s", "end_s", "peak_snr_db"}]."""
    raw: list[list[int]] = []
    on, a = False, 0
    for i, v in enumerate(np.nan_to_num(snr_db, nan=-99.0)):
        if not on and v >= open_db:
            on, a = True, i
        elif on and v < close_db:
            raw.append([a, i])
            on = False
    if on:
        raw.append([a, len(snr_db)])
    merged: list[list[int]] = []
    for s in raw:
        if merged and (s[0] - merged[-1][1]) * block_s < merge_gap_s:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    out = []
    for a, b in merged:
        if (b - a) * block_s >= min_s:
            out.append({"start_s": round(a * block_s, 2), "end_s": round(b * block_s, 2),
                        "peak_snr_db": round(float(np.nanmax(snr_db[a:b])), 1)})
    return out


def padded_spans(segments: list[dict[str, float]], duration_s: float, pad_s: float = PAD_S) -> list[tuple[float, float]]:
    spans: list[list[float]] = []
    for s in segments:
        a, b = max(0.0, s["start_s"] - pad_s), min(duration_s, s["end_s"] + pad_s)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return [(a, b) for a, b in spans]


def extract_track(job: dict[str, Any]) -> dict[str, Any]:
    """One channel of one recording -> its WAV and per-block SNR/level (runs in a worker process)."""
    from scipy.signal import butter, firwin, lfilter, lfilter_zi, sosfilt, sosfilt_zi

    from .dsp import DTYPES, to_complex
    from .sstvdec import Decimator

    src, fmt = pathlib.Path(job["path"]), job["format"]
    rate, rp = int(job["sample_rate_hz"]), job["rates"]
    m, if_rate = rp["decimation"], rp["if_sample_rate_hz"]
    up, down = rp["resample"]
    curve = (np.asarray(job["doppler"]["t_s"]), np.asarray(job["doppler"]["shift_hz"])) if job.get("doppler") else None
    mix = Mixer(rate, job["frequency_hz"] - job["center_frequency_hz"], curve)
    # stage 1: just enough to protect the channel and the noise reference from aliasing
    # (pass band to the noise reference's outer edge, 0.4 * IF; what aliases lands beyond it)
    if m > 1:
        ha = firwin(int(4.0 * rate / (0.2 * if_rate)) | 1, 0.5 * if_rate, fs=rate, window=("kaiser", 7.0))
        dec = Decimator(ha, m)
    else:
        dec = None
    # stage 2: the 12.5 kHz channel filter and the rate change to 48 kHz together
    fs_up = if_rate * up
    nb = int(4.0 * fs_up / CHANNEL_TRANSITION_HZ) | 1
    hb = firwin(nb, CHANNEL_HALF_BW_HZ, fs=fs_up, window=("kaiser", 7.0))
    rs = Resampler(hb, up, down)
    db, da = _deemphasis()
    dzi = lfilter_zi(db, da) * 0.0
    sos = butter(4, AUDIO_BAND_HZ, btype="bandpass", fs=AUDIO_RATE, output="sos")
    szi = sosfilt_zi(sos) * 0.0
    scale = HEADROOM / DEVIATION_HZ
    dtype, comps = DTYPES[fmt]
    bps = np.dtype(dtype).itemsize * comps
    total = src.stat().st_size // bps
    blk = int(round(if_rate * BLOCK_S))
    nfft = 1 << max(8, int(math.log2(max(256, if_rate / 125.0))))
    nfft = min(nfft, blk)
    win = np.hanning(nfft).astype(np.float32)
    lvl_blk = int(AUDIO_RATE * BLOCK_S)
    pend_if = np.zeros(0, np.complex64)
    pend_au = np.zeros(0, np.float32)
    snr: list[float] = []
    level: list[float] = []
    prev = np.complex64(0)
    n_audio = clipped = 0
    wav = pathlib.Path(job["wav"])
    with src.open("rb") as fi, wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        left = total
        while left > 0:
            raw = fi.read(min(left, CHUNK) * bps)
            if not raw:
                break
            x = to_complex(raw, fmt).astype(np.complex64)
            left -= len(x)
            x = mix(x)
            y = dec(x) if dec is not None else x
            if len(y):
                pend_if = np.concatenate([pend_if, y])
                nb_ = len(pend_if) // blk
                for i in range(nb_):
                    snr.append(_snr_block(pend_if[i * blk:(i + 1) * blk], if_rate, nfft, win))
                pend_if = pend_if[nb_ * blk:]
            z = rs(y)
            if not len(z):
                continue
            d = np.angle(z * np.conj(np.concatenate([[prev], z[:-1]]))) * (AUDIO_RATE / (2 * np.pi))   # Hz
            prev = z[-1]
            d, dzi = lfilter(db, da, d, zi=dzi)
            d, szi = sosfilt(sos, d, zi=szi)
            a = (d * scale).astype(np.float32)
            clipped += int(np.count_nonzero(np.abs(a) >= 1.0))
            w.writeframes((np.clip(a, -1.0, 1.0) * 32767).astype("<i2").tobytes())
            n_audio += len(a)
            pend_au = np.concatenate([pend_au, a])
            k = len(pend_au) // lvl_blk
            if k:
                blocks = pend_au[: k * lvl_blk].reshape(k, lvl_blk)
                level.extend(np.abs(blocks).max(axis=1).tolist())
                pend_au = pend_au[k * lvl_blk:]
    snr_a = np.asarray(snr, dtype=np.float64)
    segs = segments_from_snr(snr_a)
    return {"key": job["key"], "audio_samples": n_audio, "audio_seconds": round(n_audio / AUDIO_RATE, 2),
            "audio_clipped_samples": clipped, "snr_db": [None if not np.isfinite(v) else round(float(v), 1) for v in snr_a],
            "level": [round(float(v), 3) for v in level], "segments": segs,
            "active_s": round(sum(s["end_s"] - s["start_s"] for s in segs), 1),
            "peak_snr_db": round(float(np.nanmax(snr_a)), 1) if len(snr_a) and np.isfinite(snr_a).any() else None,
            "filters": {"stage1_taps": int(len(ha)) if dec is not None else 0, "channel_taps": int(nb)}}


def write_trimmed(wav: pathlib.Path, dst: pathlib.Path, spans: list[tuple[float, float]]) -> list[dict[str, float]]:
    """Copy only the spans of wav into dst. Returns where each span landed in the trimmed file."""
    out, at = [], 0.0
    with wave.open(str(wav), "rb") as r, wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(r.getframerate())
        fr = r.getframerate()
        for a, b in spans:
            i0, i1 = int(a * fr), min(r.getnframes(), int(b * fr))
            if i1 <= i0:
                continue
            r.setpos(i0)
            w.writeframes(r.readframes(i1 - i0))
            out.append({"start_s": round(a, 2), "end_s": round(i1 / fr, 2), "trimmed_start_s": round(at, 2)})
            at += (i1 - i0) / fr
    return out


def encode(wav: pathlib.Path, ffmpeg: str | None = None) -> tuple[pathlib.Path | None, str | None]:
    """A browser copy of wav: Opus in Ogg, else MP3. (None, why) when ffmpeg cannot."""
    exe = ffmpeg or shutil.which("ffmpeg")
    if not exe:
        return None, "ffmpeg is not installed (no Opus/MP3 copy; the WAV plays too)"
    for codec, ext, extra in (("libopus", ".ogg", ["-b:a", "32k", "-application", "voip"]),
                              ("libmp3lame", ".mp3", ["-b:a", "48k"])):
        dst = wav.with_suffix(ext)
        r = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-ac", "1", "-c:a", codec,
                            *extra, str(dst)], capture_output=True, text=True, timeout=900, check=False)
        if r.returncode == 0 and dst.is_file():
            return dst, None
        dst.unlink(missing_ok=True)
    return None, f"ffmpeg could not encode Opus or MP3: {(r.stderr or '').strip()[-200:]}"


# ================================================================ band scan
def scan_band(src: pathlib.Path, fmt: str, rate: int, center_hz: int, curve_center: tuple[np.ndarray, np.ndarray] | None,
              exclude_hz: list[float], reads_per_s: int = 4) -> dict[str, Any]:
    """Coarse per-second spectrum of the whole recording (a few short reads each second), searched for FM channels
    with intermittent activity. With a Doppler curve (at the center frequency) the search is done in the satellite's
    frame, where a downlink stays in one place. Returns {"candidates": [...], "bin_hz", "seconds"}."""
    from .dsp import DTYPES, to_complex
    dtype, comps = DTYPES[fmt]
    bps = np.dtype(dtype).itemsize * comps
    total = src.stat().st_size // bps
    nfft = 1 << int(round(math.log2(rate / SCAN_BIN_HZ)))
    binw = rate / nfft
    seconds = int(total // rate)
    win = np.hanning(nfft).astype(np.float32)
    rows = np.zeros((seconds, nfft), np.float32)
    span = 2 * nfft
    with src.open("rb") as fi:
        for s in range(seconds):
            acc = np.zeros(nfft, np.float64)
            for r_ in range(reads_per_s):
                at = s * rate + int((r_ + 0.5) * rate / reads_per_s) - span // 2
                fi.seek(max(0, at) * bps)
                x = to_complex(fi.read(span * bps), fmt)
                if len(x) < span:
                    continue
                seg = x.reshape(2, nfft) * win
                acc += (np.abs(np.fft.fft(seg, axis=1)) ** 2).sum(axis=0)
            rows[s] = np.fft.fftshift(acc)
    freqs = (np.arange(nfft) - nfft // 2) * binw                  # baseband offset of each bin
    usable = np.abs(freqs) <= rate / 2 * 0.85 - CHANNEL_HALF_BW_HZ
    med = np.median(rows[:, usable], axis=1, keepdims=True)
    med[med <= 0] = 1.0
    rel = rows / med
    ch_bins = max(1, int(round(2 * CHANNEL_HALF_BW_HZ / binw)))
    kern = np.ones(ch_bins, np.float32) / ch_bins

    def channel_snr(r: np.ndarray) -> np.ndarray:
        c = np.apply_along_axis(lambda v: np.convolve(v, kern, mode="same"), 1, r)
        return 10 * np.log10(np.maximum(c - 1.0, 1e-3))

    frames: dict[str, np.ndarray] = {}
    if curve_center is not None:          # first: a tie between the frames goes to the satellite
        shift = np.round(np.interp(np.arange(seconds) + 0.5, *curve_center) / binw).astype(int)
        sat = np.stack([np.roll(rel[i], -shift[i]) for i in range(seconds)])
        frames["satellite"] = channel_snr(sat)
    frames["fixed"] = channel_snr(rel)
    found: list[dict[str, Any]] = []
    for frame, snr in frames.items():
        active = (snr >= SCAN_ACTIVE_DB).sum(axis=0).astype(float)
        peak = snr.max(axis=0)
        score = np.where(usable & (np.abs(freqs) > SCAN_DC_GUARD_HZ), active, 0.0)
        for k in np.argsort(-score):
            if score[k] < SCAN_MIN_ACTIVE_S:
                break
            # the channel is the middle of the contiguous run of bins about as active as this one (a strong
            # carrier keeps a channel-wide window active over more than its own width)
            a_, b_ = k, k
            while a_ > 0 and score[a_ - 1] >= 0.9 * score[k] and k - a_ < 8 * ch_bins:
                a_ -= 1
            while b_ < nfft - 1 and score[b_ + 1] >= 0.9 * score[k] and b_ - k < 8 * ch_bins:
                b_ += 1
            kc = (a_ + b_) // 2
            f = float(freqs[kc])
            if any(abs(f - (x - center_hz)) < SCAN_SEPARATION_HZ for x in exclude_hz) or \
                    any(abs(f - (c["frequency_hz"] - center_hz)) < SCAN_SEPARATION_HZ for c in found):
                continue
            duty = active[kc] / max(1, seconds)
            idle = float((snr[:, kc] < 0.0).mean())
            if peak[kc] < SCAN_MIN_PEAK_DB or idle < SCAN_MIN_IDLE:
                continue
            # a signal is in the frame where it is sharpest: a fixed-frequency carrier smears out in the satellite
            # frame and a downlink smears in the fixed one
            other = [o for o in frames if o != frame]
            if other:
                lo, hi = max(0, kc - ch_bins), min(nfft, kc + ch_bins + 1)
                there = (frames[other[0]][:, lo:hi] >= SCAN_ACTIVE_DB).sum(axis=0).max()
                if there > active[kc] or (there == active[kc] and frame == "fixed"):
                    continue
            found.append({"frequency_hz": int(round(center_hz + f, -2)), "frame": frame, "active_s": int(active[kc]),
                          "duty": round(float(duty), 3), "idle": round(idle, 3), "peak_snr_db": round(float(peak[kc]), 1)})
            if len(found) >= 3 * MAX_DETECTED:
                break
    found.sort(key=lambda c: -c["active_s"])
    return {"candidates": found[:MAX_DETECTED], "considered": found, "bin_hz": binw, "seconds": seconds}


# ================================================================ what to extract
def plan(rec: dict[str, Any], first: dict[str, Any], p: dict[str, Any], norad: int | None, elements: Any,
         location: tuple[float, float, float] | None, known_channels_hz: list[int]) -> dict[str, Any]:
    """The resolved parameters of an audio attempt (Processor kind=audio, and the offline backfill): its tracks and
    whether and how Doppler is tracked. Raises ValueError for parameters it cannot honour.

    params: doppler (default: on for a satellite with orbital elements and a station location), scan (default on),
    frequencies_hz (more channels to extract)."""
    from ..satellites import predict
    from .aprsparse import recording_start
    allowed = {"doppler", "scan", "frequencies_hz", "priority"}
    if set(p) - allowed:
        raise ValueError(f"audio extraction does not take {sorted(set(p) - allowed)}")
    for k in ("doppler", "scan"):
        if p.get(k) is not None and not isinstance(p[k], bool):
            raise ValueError(f"{k} must be true or false")
    extra = p.get("frequencies_hz") or []
    if not (isinstance(extra, list) and len(extra) <= 8 and all(isinstance(f, int) and f > 0 for f in extra)):
        raise ValueError("frequencies_hz must be a list of up to 8 frequencies in Hz")
    spec = rec.get("spec") or {}
    meta, radio = spec.get("metadata") or {}, spec.get("radio") or {}
    rate, center = int(first["sample_rate_hz"]), int(first["center_frequency_hz"])
    usable = rate / 2 * 0.9 - CHANNEL_HALF_BW_HZ
    tracks: list[dict[str, Any]] = []

    def add(hz: Any, source: str, label: str | None, downlink: str | None = None) -> None:
        if not hz or abs(int(hz) - center) > usable or any(abs(int(hz) - t["frequency_hz"]) < 2_000 for t in tracks):
            return
        tracks.append({"frequency_hz": int(hz), "source": source, "label": label, "downlink": downlink})
    tgt = radio.get("target_frequency_hz") or first.get("target_frequency_hz")
    dl = meta.get("downlink") or {}
    add(tgt, "target", dl.get("mode") if dl.get("hz") == tgt else None, dl.get("id") if dl.get("hz") == tgt else None)
    for d in meta.get("downlinks") or []:                       # booked together with the target
        add(d.get("hz"), "known", d.get("mode"), d.get("id"))
    for d in (predict.downlinks(norad) if norad else []) or []:  # the satellite's FM downlinks in the catalog
        if "FM" in str(d.get("mode") or ""):
            add(d.get("hz"), "known", d.get("mode"), d.get("id"))
    for hz in [*known_channels_hz, *extra]:                     # the station's own list, and this request's
        add(hz, "known", None)
    if not tracks and p.get("scan") is False:
        raise ValueError(f"no FM channel of this recording is inside its recorded band ({center / 1e6:.4f} MHz "
                         f"±{usable / 1e3:.0f} kHz); give frequencies_hz or scan=true")
    satellite = (spec.get("target") or {}).get("type") == "satellite" or bool(norad)
    doppler: dict[str, Any] = {"enabled": False, "why": None}
    want = p.get("doppler")
    if want is False:
        doppler["why"] = "turned off for this attempt"
    elif not satellite:
        doppler["why"] = "not a satellite recording"
    elif elements is None:
        doppler["why"] = f"no orbital elements for NORAD {norad}"
    elif location is None:
        doppler["why"] = "the station location is not set (Settings → Observatory)"
    else:
        # sample 0 is the time in the file name, as for the APRS packet times
        start = recording_start(first.get("path")) or dt.datetime.fromisoformat(str(rec.get("record_start")))
        doppler = {"enabled": True, "why": None, "norad": int(norad), "elements": elements_record(elements),
                   "latitude": location[0], "longitude": location[1], "elevation_m": location[2],
                   "start": start.isoformat()}
    if want is True and not doppler["enabled"]:
        raise ValueError(f"Doppler tracking is not possible: {doppler['why']}")
    return {"tracks": tracks, "scan": p.get("scan", True), "doppler": doppler, "center_frequency_hz": center,
            "input_sample_rate_hz": rate, "baseband_format": first["format"], "priority": "nice 19, ionice idle"}


# ================================================================ the attempt
def _label(hz: float) -> str:
    return f"track-{hz / 1e6:.4f}MHz"


def run(inp: dict[str, Any], p: dict[str, Any], out: pathlib.Path, tag: str = "",
        progress: Callable[[str, int, int], None] | None = None, workers: int | None = None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """procworker entry (kind=audio): one input -> every track's audio, trimmed audio and activity (audio.json)."""
    t0 = dt.datetime.now(dt.timezone.utc)
    src = pathlib.Path(inp["path"])
    fmt, rate, center = inp["format"], int(inp["sample_rate_hz"]), int(inp["center_frequency_hz"])
    from .dsp import DTYPES
    dtype, comps = DTYPES[fmt]
    duration = src.stat().st_size // (np.dtype(dtype).itemsize * comps) / rate
    rates = plan_rates(rate)
    errors: list[str] = []
    dop = p.get("doppler") or {}
    rr: tuple[np.ndarray, np.ndarray] | None = None
    if dop.get("enabled"):
        try:
            start = dt.datetime.fromisoformat(dop["start"])
            rr = range_rate_curve(dop["elements"], dop["latitude"], dop["longitude"], dop.get("elevation_m") or 0.0,
                                  start, duration + 2)
        except Exception as exc:  # noqa: BLE001 - audio without Doppler tracking is still audio
            errors.append(f"Doppler not tracked: {type(exc).__name__}: {exc}")
    tracks = [dict(t) for t in p.get("tracks") or []]
    usable = rate / 2 * 0.9 - CHANNEL_HALF_BW_HZ
    tracks = [t for t in tracks if abs(t["frequency_hz"] - center) <= usable]
    scan: dict[str, Any] | None = None
    if p.get("scan", True):
        if progress:
            progress("scanning the recorded band for FM channels", 0, 3)
        try:
            cc = (rr[0], doppler_hz(rr[1], center)) if rr is not None else None
            scan = scan_band(src, fmt, rate, center, cc, [t["frequency_hz"] for t in tracks])
            for c in scan["candidates"]:
                tracks.append({"frequency_hz": c["frequency_hz"], "source": "detected", "label": None,
                               "doppler": c["frame"] == "satellite", "scan": c})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"band scan failed: {type(exc).__name__}: {exc}")
    jobs = []
    for t in tracks:
        hz = int(t["frequency_hz"])
        key = _label(hz) + tag
        dshift = None
        if rr is not None and t.get("doppler", True):
            d = doppler_hz(rr[1], hz)
            dshift = {"t_s": rr[0].tolist(), "shift_hz": d.tolist()}
            t["doppler_hz"] = {"min": round(float(d.min())), "max": round(float(d.max())),
                               "at_start": round(float(d[0])), "at_end": round(float(d[-1]))}
        t["doppler_tracked"] = dshift is not None
        jobs.append({"key": key, "path": str(src), "format": fmt, "sample_rate_hz": rate, "center_frequency_hz": center,
                     "frequency_hz": hz, "rates": rates, "doppler": dshift, "wav": str(out / f"{key}.wav")})
    if progress:
        progress(f"demodulating {len(jobs)} FM channel(s)", 1, 3)
    done: dict[str, dict[str, Any]] = {}
    # one process per channel for real passes (each streams the whole file); small files are not worth the start-up
    big = src.stat().st_size >= PARALLEL_MIN_BYTES
    n_workers = max(1, min(len(jobs), workers or (MAX_WORKERS if big else 1)))
    if jobs:
        if n_workers == 1:
            for j in jobs:
                done[j["key"]] = extract_track(j)
        else:
            import multiprocessing as mp
            with cf.ProcessPoolExecutor(n_workers, mp_context=mp.get_context("forkserver")) as ex:
                futs = {ex.submit(extract_track, j): j for j in jobs}
                for f in cf.as_completed(futs):
                    j = futs[f]
                    try:
                        done[j["key"]] = f.result()
                    except Exception as exc:  # noqa: BLE001 - one channel must not lose the others
                        errors.append(f"{j['frequency_hz'] / 1e6:.4f} MHz: {type(exc).__name__}: {exc}")
    if progress:
        progress("writing trimmed audio and browser copies", 2, 3)
    arts: list[dict[str, str]] = []
    out_tracks = []
    for t, j in zip(tracks, jobs):
        r = done.get(j["key"])
        if r is None:
            continue
        if t["source"] == "detected" and not r["segments"]:
            pathlib.Path(j["wav"]).unlink(missing_ok=True)       # the scan's guess did not hold up: no track
            continue
        wav = pathlib.Path(j["wav"])
        name = t.get("label") or ("detected FM channel" if t["source"] == "detected" else "FM channel")
        mhz = f"{t['frequency_hz'] / 1e6:.4f} MHz"
        enc, why = encode(wav)
        if why:
            errors.append(why)
        trimmed = trimmed_enc = None
        spans: list[dict[str, float]] = []
        if r["segments"]:
            trimmed = wav.with_name(wav.stem + "-trimmed.wav")
            spans = write_trimmed(wav, trimmed, padded_spans(r["segments"], r["audio_seconds"]))
            trimmed_enc, _ = encode(trimmed)
        arts.append({"kind": "audio", "path": str(wav), "label": f"{mhz} {name}: FM audio, 48 kHz (WAV)"})
        if enc:
            arts.append({"kind": "audio", "path": str(enc), "label": f"{mhz} {name}: FM audio ({enc.suffix[1:].upper()})"})
        if trimmed:
            arts.append({"kind": "audio", "path": str(trimmed), "label": f"{mhz} {name}: active segments only (WAV)"})
        if trimmed_enc:
            arts.append({"kind": "audio", "path": str(trimmed_enc),
                         "label": f"{mhz} {name}: active segments only ({trimmed_enc.suffix[1:].upper()})"})
        out_tracks.append({**{k: v for k, v in t.items() if k != "doppler"}, "key": j["key"], "label": name,
                           "files": {"wav": str(wav), "browser": str(enc) if enc else None,
                                     "trimmed_wav": str(trimmed) if trimmed else None,
                                     "trimmed_browser": str(trimmed_enc) if trimmed_enc else None},
                           "trimmed_spans": spans, **{k: r[k] for k in ("audio_seconds", "audio_clipped_samples",
                                                                        "segments", "active_s", "peak_snr_db", "filters")},
                           "timeline": {"block_s": BLOCK_S, "snr_db": r["snr_db"], "level": r["level"]}})
    out_tracks.sort(key=lambda t: (t["source"] == "detected", t["frequency_hz"]))
    doc = {"recording": str(src), "center_frequency_hz": center, "sample_rate_hz": rate, "duration_s": round(duration, 2),
           "rates": rates, "settings": {"deviation_hz": DEVIATION_HZ, "channel_bandwidth_hz": 2 * CHANNEL_HALF_BW_HZ,
                                        "deemphasis_us": DEEMPHASIS_US, "audio_band_hz": list(AUDIO_BAND_HZ),
                                        "squelch_open_db": SQUELCH_OPEN_DB, "squelch_close_db": SQUELCH_CLOSE_DB,
                                        "pad_s": PAD_S},
           "doppler": {k: v for k, v in dop.items() if k != "elements"} | (
               {"elements_epoch": dop["elements"].get("epoch"), "elements_source": dop["elements"].get("source")}
               if dop.get("elements") else {}) | {"tracked": rr is not None},
           "scan": {k: v for k, v in (scan or {}).items()} if scan else None, "tracks": out_tracks}
    js = out / f"audio{tag}.json"
    js.write_text(json.dumps(doc, indent=1))
    arts.append({"kind": "product", "path": str(js), "label": "FM audio tracks, activity and SNR timeline (JSON)"})
    summary_tracks = [{k: t[k] for k in ("key", "frequency_hz", "label", "source", "doppler_tracked", "active_s",
                                         "peak_snr_db", "segments", "audio_seconds")} for t in out_tracks]
    active = round(sum(t["active_s"] for t in out_tracks), 1)
    res = {"input": str(src), "decoder": "fm_audio", "tracks": summary_tracks, "track_count": len(out_tracks),
           "active_tracks": sum(1 for t in out_tracks if t["segments"]), "active_s": active,
           "doppler_tracked": rr is not None, "audio_file": str(js), "errors": errors,
           "note": None if active else NO_ACTIVITY,
           "duration_s": round((dt.datetime.now(dt.timezone.utc) - t0).total_seconds(), 1)}
    return res, arts
