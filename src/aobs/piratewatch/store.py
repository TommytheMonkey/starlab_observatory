"""Pirate watch storage: the hit log, per-channel activity, and per-bin power history.

  hits      one row per detection: frequency, UTC times, duration, peak dB, transcript, audio path,
            and a status (pending -> speech | no_speech | missed | error).
  activity  per channel and UTC hour: detections, seconds on the air, speech clips. The heatmap.
  history/  every sweep, one uint8 row per sweep (0.5 dB steps from -80 dB), one file per UTC day
            and bin layout, plus a float64 timestamp per row. Pruned after `history_days`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import threading
from typing import Any

import numpy as np

from .spectrum import Sweep

UTC = dt.timezone.utc
SCHEMA = """
CREATE TABLE IF NOT EXISTS hits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  detected_at TEXT NOT NULL,
  start_utc TEXT,
  end_utc TEXT,
  freq_hz INTEGER NOT NULL,
  channel_hz INTEGER NOT NULL,
  width_hz REAL,
  duration_s REAL,
  clip_s REAL,
  peak_db REAL,
  snr_db REAL,
  audio_path TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  language TEXT,
  language_prob REAL,
  transcript TEXT,
  translation TEXT,
  speech_prob REAL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS hits_status ON hits(status);
CREATE INDEX IF NOT EXISTS hits_channel ON hits(channel_hz);
CREATE TABLE IF NOT EXISTS activity (
  channel_hz INTEGER NOT NULL,
  hour_utc TEXT NOT NULL,
  detections INTEGER NOT NULL DEFAULT 0,
  seconds REAL NOT NULL DEFAULT 0,
  speech INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (channel_hz, hour_utc)
);
"""
HIT_FIELDS = ("id", "detected_at", "start_utc", "end_utc", "freq_hz", "channel_hz", "width_hz", "duration_s",
              "clip_s", "peak_db", "snr_db", "audio_path", "status", "language", "language_prob", "transcript",
              "translation", "speech_prob", "note")


def iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z" if t else None


def hour_key(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H")


class Store:
    def __init__(self, path: pathlib.Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------ hits
    def add_hit(self, **fields: Any) -> int:
        cols = [k for k in fields if k in HIT_FIELDS and k != "id"]
        with self._lock, self._db:
            cur = self._db.execute(f"INSERT INTO hits ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                   [fields[c] for c in cols])
            return int(cur.lastrowid or 0)

    def update_hit(self, hit_id: int, **fields: Any) -> None:
        cols = [k for k in fields if k in HIT_FIELDS and k != "id"]
        if not cols:
            return
        with self._lock, self._db:
            self._db.execute(f"UPDATE hits SET {','.join(f'{c}=?' for c in cols)} WHERE id=?",
                             [fields[c] for c in cols] + [hit_id])

    def hit(self, hit_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM hits WHERE id=?", (hit_id,)).fetchone()
        return dict(row) if row else None

    def hits(self, status: str | None = "speech", limit: int = 50, since: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM hits WHERE 1=1", []
        if status:
            q += " AND status=?"
            args.append(status)
        if since:
            q += " AND detected_at>=?"
            args.append(since)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._db.execute(q, args).fetchall()]

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM hits WHERE status='pending' AND audio_path IS NOT NULL ORDER BY id").fetchall()]

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {r[0]: r[1] for r in self._db.execute("SELECT status, COUNT(*) FROM hits GROUP BY status")}

    # ------------------------------------------------------------ activity / heatmap
    def add_activity(self, channel_hz: int, at: dt.datetime, seconds: float, detections: int = 1) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO activity (channel_hz, hour_utc, detections, seconds) VALUES (?,?,?,?) "
                "ON CONFLICT(channel_hz, hour_utc) DO UPDATE SET detections=detections+excluded.detections, "
                "seconds=seconds+excluded.seconds", (channel_hz, hour_key(at), detections, seconds))

    def add_speech(self, channel_hz: int, at: dt.datetime) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO activity (channel_hz, hour_utc, speech) VALUES (?,?,1) "
                "ON CONFLICT(channel_hz, hour_utc) DO UPDATE SET speech=speech+1", (channel_hz, hour_key(at)))

    def activity(self, since_hour: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM activity", []
        if since_hour:
            q += " WHERE hour_utc>=?"
            args.append(since_hour)
        with self._lock:
            return [dict(r) for r in self._db.execute(q + " ORDER BY channel_hz, hour_utc", args).fetchall()]


class History:
    """Append-only per-bin power history (see module docstring)."""

    def __init__(self, root: pathlib.Path, days: float = 14.0):
        self.root = root
        self.days = days
        self._layout: tuple[str, str] | None = None   # (layout hash, day)

    @staticmethod
    def quantize(db: np.ndarray) -> np.ndarray:
        return np.clip(np.round((np.nan_to_num(db, nan=-80.0) + 80.0) * 2), 0, 255).astype(np.uint8)

    @staticmethod
    def dequantize(q: np.ndarray) -> np.ndarray:
        return q.astype(np.float32) / 2 - 80.0

    def _base(self, sweep: Sweep) -> pathlib.Path:
        layout = hashlib.sha1(sweep.freqs.astype("<f8").tobytes()).hexdigest()[:8]
        day = sweep.at.astimezone(UTC).strftime("%Y-%m-%d")
        base = self.root / f"{day}-{layout}"
        if self._layout != (layout, day):
            self.root.mkdir(parents=True, exist_ok=True)
            meta = base.with_suffix(".json")
            if not meta.exists():
                meta.write_text(json.dumps({"start_hz": float(sweep.freqs[0]), "bin_hz": sweep.bin_hz,
                                            "nbins": int(len(sweep.freqs)), "encoding": "uint8 dB=(q/2)-80"}))
                np.save(base.with_suffix(".freqs.npy"), sweep.freqs)
            self._layout = (layout, day)
            self.prune(sweep.at)
        return base

    def append(self, sweep: Sweep) -> None:
        base = self._base(sweep)
        with open(base.with_suffix(".u8"), "ab") as f:
            f.write(self.quantize(sweep.db).tobytes())
        with open(base.with_suffix(".ts"), "ab") as f:
            f.write(np.float64(sweep.at.timestamp()).tobytes())

    def prune(self, now: dt.datetime) -> None:
        cutoff = (now - dt.timedelta(days=self.days)).astimezone(UTC).strftime("%Y-%m-%d")
        for path in self.root.glob("????-??-??-*"):
            if path.name[:10] < cutoff:
                path.unlink(missing_ok=True)

    @staticmethod
    def read(base: pathlib.Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(timestamps, freqs, dB[rows, bins]) of one history file set (path without suffix)."""
        freqs = np.load(base.with_suffix(".freqs.npy"))
        ts = np.fromfile(base.with_suffix(".ts"), dtype=np.float64)
        q = np.fromfile(base.with_suffix(".u8"), dtype=np.uint8)
        rows = min(len(ts), len(q) // len(freqs))
        return ts[:rows], freqs, History.dequantize(q[: rows * len(freqs)].reshape(rows, len(freqs)))
