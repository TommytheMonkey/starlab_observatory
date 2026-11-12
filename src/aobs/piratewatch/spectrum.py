"""Sweep assembly, baseline learning and carrier detection for the satcom pirate watch.

Pure numpy, no subprocesses: the daemon feeds rtl_power's CSV lines in and gets
sweeps, baseline state and candidate channels out.

  * rtl_power (continuous, no -1) prints one row per tuning hop every integration
    interval, all hops of one interval with the same timestamp. SweepAssembler
    joins the rows of one pass across the band into a single (freqs, dB) sweep.
  * Baseline learns, from the first `learn_sweeps` sweeps, a per-bin noise floor
    (a low percentile, so intermittent traffic does not raise it) and a per-bin
    duty cycle. Bins that sit above the floor most of the time are steady
    carriers or spurs and are masked out. The baseline persists across runs.
  * detect() finds runs of unmasked bins more than `threshold_db` above the floor
    whose width matches a voice channel (15-25 kHz by default).
"""

from __future__ import annotations

import datetime as dt
import pathlib
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

UTC = dt.timezone.utc


@dataclass
class Sweep:
    at: dt.datetime
    freqs: np.ndarray     # bin centres, Hz (float64)
    db: np.ndarray        # power per bin, dB (float32)

    @property
    def bin_hz(self) -> float:
        return float(self.freqs[1] - self.freqs[0]) if len(self.freqs) > 1 else 0.0


def _row(line: str) -> tuple[dt.datetime, float, float, float, list[float]] | None:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 7:
        return None
    try:
        # rtl_power stamps rows in the computer's local time (localtime()), not UTC
        at = dt.datetime.strptime(f"{parts[0]} {parts[1]}", "%Y-%m-%d %H:%M:%S").astimezone(UTC)
        low, high, step = float(parts[2]), float(parts[3]), float(parts[4])
        values = [float(v) for v in parts[6:]]
    except ValueError:
        return None
    return at, low, high, step, values


class SweepAssembler:
    """Joins rtl_power rows into complete sweeps. A sweep is emitted as soon as its last hop arrives
    (the row reaching stop_hz), or when the next pass starts (a row that does not continue upward)."""

    def __init__(self, stop_hz: float):
        self.stop_hz = stop_hz
        self._rows: list[tuple[dt.datetime, float, float, float, list[float]]] = []

    def feed(self, line: str) -> Sweep | None:
        row = _row(line)
        if row is None:
            return None
        out = None
        if self._rows and row[1] <= self._rows[-1][1]:
            out = self._emit()
        self._rows.append(row)
        if row[2] >= self.stop_hz - row[3]:
            out = self._emit() or out
        return out

    def _emit(self) -> Sweep | None:
        rows, self._rows = self._rows, []
        if not rows:
            return None
        freqs: list[float] = []
        db: list[float] = []
        for _, low, _, step, values in rows:
            freqs.extend(low + (i + 0.5) * step for i in range(len(values)))
            db.extend(values)
        f = np.asarray(freqs, dtype=np.float64)
        d = np.asarray(db, dtype=np.float32)
        order = np.argsort(f, kind="stable")
        f, d = f[order], d[order]
        d = np.where(np.isfinite(d), d, np.nan).astype(np.float32)
        return Sweep(rows[0][0], f, d)


@dataclass(frozen=True)
class Candidate:
    center_hz: int        # power-weighted centroid of the run, rounded to 100 Hz
    width_hz: float
    peak_db: float        # absolute power of the strongest bin (rtl_power dB)
    snr_db: float         # strongest bin above the learned floor
    lo_bin: int
    hi_bin: int


class Baseline:
    """Per-bin floor, duty cycle and steady-carrier mask, learned from the first sweeps of the job."""

    def __init__(self, learn_sweeps: int = 600, threshold_db: float = 8.0, duty_mask: float = 0.5,
                 floor_percentile: float = 25.0, min_provisional: int = 20, dilate_bins: int = 1,
                 floor_window_bins: int = 61, static_ranges: Iterable[tuple[float, float]] = ()):
        self.floor_window_bins = floor_window_bins
        self.static_ranges = [(float(lo), float(hi)) for lo, hi in static_ranges]   # configured (comb), never learned
        self._static: tuple[int, np.ndarray] | None = None
        self.learn_sweeps = learn_sweeps
        self.threshold_db = threshold_db
        self.duty_mask = duty_mask
        self.floor_percentile = floor_percentile
        self.min_provisional = min_provisional
        self.dilate_bins = dilate_bins
        self.freqs: np.ndarray | None = None
        self.floor: np.ndarray | None = None
        self.duty: np.ndarray | None = None
        self.mask: np.ndarray | None = None          # True = excluded (steady carrier / spur)
        self.extra_mask: np.ndarray | None = None    # channels excluded later (stuck carriers)
        self.learned = 0
        self._samples: list[np.ndarray] = []

    # ------------------------------------------------------------ state
    @property
    def complete(self) -> bool:
        return self.learned >= self.learn_sweeps

    @property
    def ready(self) -> bool:
        return self.floor is not None

    def _reset(self, freqs: np.ndarray) -> None:
        self.freqs = freqs.copy()
        self.floor = self.duty = self.mask = None
        self.extra_mask = np.zeros(len(freqs), dtype=bool)
        self.learned = 0
        self._samples = []

    def matches(self, sweep: Sweep) -> bool:
        return self.freqs is not None and len(self.freqs) == len(sweep.freqs) and \
            bool(np.allclose(self.freqs, sweep.freqs, atol=1.0))

    def add(self, sweep: Sweep) -> None:
        """Learning step. A sweep with a different bin layout (new settings) restarts learning."""
        if not self.matches(sweep):
            self._reset(sweep.freqs)
        if self.complete:
            return
        self._samples.append(np.nan_to_num(sweep.db, nan=np.nanmedian(sweep.db)))
        self.learned += 1
        n = len(self._samples)
        if self.complete or (n >= self.min_provisional and n % 10 == 0) or (n == self.min_provisional):
            self._compute()
        if self.complete:
            self._samples = []

    def _compute(self) -> None:
        stack = np.vstack(self._samples)
        # A bin's own low percentile would rise with a steady carrier on it, hiding it from the duty test.
        # The floor is that percentile smoothed by a running median across frequency: it follows the
        # tuner/hop shape (MHz scale) but steps over carriers far narrower than the window.
        floor = _running_median(np.percentile(stack, self.floor_percentile, axis=0), self.floor_window_bins)
        floor = floor.astype(np.float32)
        # Drift-correct each sweep against the floor before measuring duty, as detect() does.
        offsets = np.median(stack - floor, axis=1, keepdims=True)
        duty = ((stack - floor - offsets) > self.threshold_db).mean(axis=0).astype(np.float32)
        mask = duty >= self.duty_mask
        if self.dilate_bins:
            grown = mask.copy()
            for k in range(1, self.dilate_bins + 1):
                grown[k:] |= mask[:-k]
                grown[:-k] |= mask[k:]
            mask = grown
        self.floor, self.duty, self.mask = floor, duty, mask

    def relearn(self) -> None:
        if self.freqs is not None:
            self._reset(self.freqs)

    def exclude(self, lo_hz: float, hi_hz: float) -> None:
        """Mask a channel found later to be a steady carrier (a clip that never went quiet)."""
        if self.freqs is not None and self.extra_mask is not None:
            self.extra_mask |= (self.freqs >= lo_hz) & (self.freqs <= hi_hz)

    @property
    def static_mask(self) -> np.ndarray:
        """Bins inside a configured range (a bin counts when any part of it overlaps)."""
        assert self.freqs is not None
        if self._static is None or self._static[0] != id(self.freqs):
            half = (float(self.freqs[1] - self.freqs[0]) if len(self.freqs) > 1 else 0.0) / 2
            m = np.zeros(len(self.freqs), dtype=bool)
            for lo, hi in self.static_ranges:
                m |= (self.freqs + half >= lo) & (self.freqs - half <= hi)
            self._static = (id(self.freqs), m)
        return self._static[1]

    @property
    def excluded(self) -> np.ndarray:
        assert self.mask is not None and self.extra_mask is not None
        return self.mask | self.extra_mask | self.static_mask

    def excluded_ranges(self) -> list[tuple[int, int]]:
        if not self.ready or self.freqs is None:
            return []
        out = []
        for lo, hi in _runs(self.excluded, gap=0):
            out.append((int(self.freqs[lo]), int(self.freqs[hi])))
        return out

    def ignore_list(self) -> dict[str, Any]:
        """What detection skips, for people: learned steady carriers/spurs, and channels masked later because a
        clip on them never went quiet. Hz edges are bin edges."""
        if not self.ready or self.freqs is None or self.mask is None or self.extra_mask is None:
            return {"learned": [], "auto": [], "complete": False, "learned_sweeps": self.learned,
                    "of": self.learn_sweeps,
                    "configured": [{"lo_hz": int(lo), "hi_hz": int(hi), "width_hz": int(hi - lo)}
                                   for lo, hi in self.static_ranges]}
        half = (float(self.freqs[1] - self.freqs[0]) if len(self.freqs) > 1 else 0.0) / 2

        def ranges(flags: np.ndarray, with_duty: bool) -> list[dict[str, Any]]:
            out = []
            for lo, hi in _runs(flags, gap=0):
                r: dict[str, Any] = {"lo_hz": int(self.freqs[lo] - half), "hi_hz": int(self.freqs[hi] + half),
                                     "bins": int(hi - lo + 1)}
                r["width_hz"] = r["hi_hz"] - r["lo_hz"]
                if with_duty and self.duty is not None:
                    r["duty_pct"] = round(float(np.max(self.duty[lo:hi + 1])) * 100, 1)
                out.append(r)
            return out

        static = self.static_mask
        return {"learned": ranges(self.mask & ~static, True), "auto": ranges(self.extra_mask & ~self.mask & ~static, False),
                "configured": ranges(static, False),
                "complete": self.complete, "learned_sweeps": self.learned, "of": self.learn_sweeps,
                "threshold_db": self.threshold_db, "duty_mask_pct": round(self.duty_mask * 100)}

    # ------------------------------------------------------------ persistence
    def save(self, path: pathlib.Path) -> None:
        if not self.ready or self.freqs is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.npz")
        np.savez(tmp, freqs=self.freqs, floor=self.floor, duty=self.duty, mask=self.mask,
                 extra_mask=self.extra_mask, learned=np.int64(self.learned),
                 samples=np.vstack(self._samples) if self._samples else np.zeros((0, len(self.freqs)), np.float32))
        tmp.replace(path)

    def load(self, path: pathlib.Path) -> bool:
        try:
            with np.load(path) as z:
                self.freqs = z["freqs"]
                self.floor, self.duty, self.mask = z["floor"], z["duty"], z["mask"].astype(bool)
                self.extra_mask = z["extra_mask"].astype(bool)
                self.learned = int(z["learned"])
                samples = z["samples"]
        except (OSError, KeyError, ValueError):
            return False
        self._samples = [] if self.complete else [s for s in samples]
        return True

    # ------------------------------------------------------------ detection
    def detect(self, sweep: Sweep, min_width_hz: float = 15_000, max_width_hz: float = 25_000,
               gap_bins: int = 1) -> list[Candidate]:
        if not self.ready or not self.matches(sweep):
            return []
        assert self.floor is not None
        db = np.nan_to_num(sweep.db, nan=-200.0)
        keep = ~self.excluded
        offset = float(np.median((db - self.floor)[keep])) if keep.any() else 0.0
        excess = db - self.floor - offset
        above = (excess > self.threshold_db) & keep
        bin_hz = sweep.bin_hz
        out = []
        for lo, hi in _runs(above, gap=gap_bins):
            width = float(sweep.freqs[hi] - sweep.freqs[lo] + bin_hz)
            if not min_width_hz <= width <= max_width_hz:
                continue
            sel = slice(lo, hi + 1)
            weights = np.where(above[sel], 10 ** (excess[sel] / 10), 0.0)
            center = float(np.sum(sweep.freqs[sel] * weights) / np.sum(weights))
            top = lo + int(np.argmax(np.where(above[sel], excess[sel], -np.inf)))
            out.append(Candidate(int(round(center / 100) * 100), width, round(float(db[top]), 1),
                                 round(float(excess[top]), 1), lo, hi))
        out.sort(key=lambda c: c.snr_db, reverse=True)
        return out


def _running_median(x: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or len(x) < window:
        return x.copy()
    half = window // 2
    padded = np.pad(x, half, mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(padded, window), axis=1)


def _runs(flags: np.ndarray, gap: int) -> Iterable[tuple[int, int]]:
    """(first, last) index of each run of True, bridging up to `gap` False bins inside a run."""
    idx = np.flatnonzero(flags)
    if len(idx) == 0:
        return []
    runs = []
    start = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev > gap + 1:
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    return runs


def channel_of(hz: float, raster_hz: int) -> int:
    return int(round(hz / raster_hz) * raster_hz)
