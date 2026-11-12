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


SCHEMA_VERSION = 1   # 1: detected_at fixed (versions before took rtl_power's local-time stamp for UTC)


def iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z" if t else None


def hour_key(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H")


def local_stamp_to_utc(stamp: str) -> str:
    """A stamp written as UTC that was really this computer's local time (rtl_power), corrected."""
    naive = dt.datetime.fromisoformat(stamp.replace("Z", "")).replace(tzinfo=None)
    return iso(naive.astimezone(UTC)) or stamp


class Store:
    def __init__(self, path: pathlib.Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
        self.migrated_local_stamps = self._migrate()

    def _migrate(self) -> bool:
        """Bring an older hit log up to SCHEMA_VERSION. True when detected_at had to be moved from local time."""
        with self._lock, self._db:
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if version >= SCHEMA_VERSION:
                return False
            rows = self._db.execute("SELECT id, detected_at FROM hits").fetchall()
            for r in rows:
                self._db.execute("UPDATE hits SET detected_at=? WHERE id=?", (local_stamp_to_utc(r[1]), r[0]))
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return bool(rows)

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

    def expired_no_speech(self, cutoff: str) -> list[dict[str, Any]]:
        """Non-speech hits whose kept audio is older than `cutoff` (ISO UTC)."""
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM hits WHERE status='no_speech' AND audio_path IS NOT NULL "
                "AND COALESCE(end_utc, start_utc, detected_at)<? ORDER BY id", (cutoff,)).fetchall()]

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

    MARKER = "stamps-utc"     # present = every .ts value is a true UTC timestamp

    def __init__(self, root: pathlib.Path, days: float = 14.0):
        self.root = root
        self.days = days
        self._layout: tuple[str, str] | None = None   # (layout hash, day)
        self.fixed_files = self._fix_local_stamps()

    def _fix_local_stamps(self) -> int:
        """History written before the marker took rtl_power's local-time stamps for UTC: correct it once."""
        marker = self.root / self.MARKER
        if marker.exists():
            return 0
        n = 0
        for ts_path in sorted(self.root.glob("????-??-??-*.ts")) if self.root.is_dir() else []:
            ts = np.fromfile(ts_path, dtype=np.float64)
            fixed = np.array([dt.datetime.fromtimestamp(v, UTC).replace(tzinfo=None).astimezone(UTC).timestamp()
                              for v in ts], dtype=np.float64)
            tmp = ts_path.with_suffix(".ts.tmp")
            fixed.tofile(tmp)
            tmp.replace(ts_path)
            n += 1
        self.root.mkdir(parents=True, exist_ok=True)
        marker.write_text("History timestamps are UTC.\n")
        return n

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


class Reader:
    """Read-only view of the hit log for the web page (control endpoint): its own connection, never writes."""

    def __init__(self, path: pathlib.Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def hits(self, speech_only: bool = True, min_hz: int | None = None, max_hz: int | None = None,
             since: str | None = None, until: str | None = None, channel_hz: int | None = None,
             hour: int | None = None, before_id: int | None = None, limit: int = 50) -> dict[str, Any]:
        """Newest first. Times are ISO UTC; `hour` is a UTC hour of day (heatmap cell). `before_id` pages."""
        if not self.path.exists():
            return {"hits": [], "more": False}
        where, args = ["1=1"], []
        if speech_only:
            where.append("status='speech'")
        if min_hz is not None:
            where.append("freq_hz>=?")
            args.append(int(min_hz))
        if max_hz is not None:
            where.append("freq_hz<=?")
            args.append(int(max_hz))
        when = "COALESCE(start_utc, detected_at)"
        if since:
            where.append(f"{when}>=?")
            args.append(since)
        if until:
            where.append(f"{when}<?")
            args.append(until)
        if channel_hz is not None:
            where.append("channel_hz=?")
            args.append(int(channel_hz))
        if hour is not None:
            where.append(f"CAST(substr({when}, 12, 2) AS INTEGER)=?")
            args.append(int(hour))
        if before_id is not None:
            where.append("id<?")
            args.append(int(before_id))
        limit = max(1, min(int(limit), 200))
        db = self._connect()
        try:
            rows = [dict(r) for r in db.execute(f"SELECT * FROM hits WHERE {' AND '.join(where)} ORDER BY id DESC "
                                                f"LIMIT ?", [*args, limit + 1]).fetchall()]
        finally:
            db.close()
        more = len(rows) > limit
        rows = rows[:limit]
        for r in rows:
            r["has_audio"] = bool(r.get("audio_path")) and pathlib.Path(r["audio_path"]).is_file()
            r.pop("audio_path", None)
        return {"hits": rows, "more": more}

    def hit(self, hit_id: int) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM hits WHERE id=?", (int(hit_id),)).fetchone()
        finally:
            db.close()
        return dict(row) if row else None

    def activity(self, since_hour: str | None = None, until_hour: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        q, args = "SELECT * FROM activity WHERE 1=1", []
        if since_hour:
            q += " AND hour_utc>=?"
            args.append(since_hour)
        if until_hour:
            q += " AND hour_utc<?"
            args.append(until_hour)
        db = self._connect()
        try:
            return [dict(r) for r in db.execute(q + " ORDER BY channel_hz, hour_utc", args).fetchall()]
        finally:
            db.close()

    def counts(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        db = self._connect()
        try:
            return {r[0]: r[1] for r in db.execute("SELECT status, COUNT(*) FROM hits GROUP BY status")}
        finally:
            db.close()
