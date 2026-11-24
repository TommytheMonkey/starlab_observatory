"""The SSTV watch's archive: one row per transmission (good decodes and the ones discarded as noise), files beside.

<data_dir>/sstvwatch.sqlite3
<data_dir>/images/YYYY/MM/DD/<id>.png, <id>.thumb.jpg, <id>.wav (audio), <id>.cf32 (channel IQ, 12 kHz complex)
Imported ISS pass images (decoded by the broker) keep their files in the broker's archive; their rows point there.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import threading
from typing import Any

UTC = dt.timezone.utc
SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,            -- watch | iss
  status TEXT NOT NULL,            -- decoded | noise | failed
  start_utc TEXT NOT NULL,
  end_utc TEXT,
  freq_hz INTEGER NOT NULL,
  band TEXT NOT NULL,
  channel TEXT,
  sideband TEXT,                   -- usb | lsb | fm
  receiver TEXT,
  vis INTEGER,
  mode TEXT,                       -- the mode the VIS header names
  decoded_mode TEXT,               -- the mode the decoder read (should agree)
  snr_db REAL,
  quality REAL,                    -- decode-quality score 0-1
  width INTEGER, height INTEGER,
  image_path TEXT, thumb_path TEXT, audio_path TEXT, iq_path TEXT,
  recording_id TEXT,               -- ISS: the broker recording (link back to its archive page)
  attempt_id TEXT,
  error TEXT,
  notified_at TEXT,
  extra TEXT
);
CREATE INDEX IF NOT EXISTS images_start ON images (start_utc);
CREATE UNIQUE INDEX IF NOT EXISTS images_iss ON images (attempt_id, image_path) WHERE source = 'iss';
"""
FIELDS = ("id", "source", "status", "start_utc", "end_utc", "freq_hz", "band", "channel", "sideband", "receiver", "vis",
          "mode", "decoded_mode", "snr_db", "quality", "width", "height", "image_path", "thumb_path", "audio_path",
          "iq_path", "recording_id", "attempt_id", "error", "notified_at", "extra")


def iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z") if t else None


class Store:
    def __init__(self, path: pathlib.Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add(self, **f: Any) -> int:
        if isinstance(f.get("extra"), dict):
            f["extra"] = json.dumps(f["extra"])
        cols = [k for k in f if k in FIELDS and k != "id"]
        with self.lock, self.db:
            cur = self.db.execute(f"INSERT OR IGNORE INTO images ({', '.join(cols)}) VALUES "
                                  f"({', '.join('?' for _ in cols)})", [f[k] for k in cols])
            return int(cur.lastrowid or 0) if cur.rowcount else 0

    def update(self, image_id: int, **f: Any) -> None:
        if isinstance(f.get("extra"), dict):
            f["extra"] = json.dumps(f["extra"])
        cols = [k for k in f if k in FIELDS and k != "id"]
        with self.lock, self.db:
            self.db.execute(f"UPDATE images SET {', '.join(k + ' = ?' for k in cols)} WHERE id = ?",
                            [f[k] for k in cols] + [image_id])

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["extra"] = json.loads(d["extra"]) if d.get("extra") else {}
        return d

    def get(self, image_id: int) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        return self._row(r) if r else None

    def images(self, status: str | None = "decoded", band: str | None = None, mode: str | None = None,
               since: str | None = None, until: str | None = None, source: str | None = None,
               limit: int = 60, before_id: int | None = None) -> list[dict[str, Any]]:
        """Newest first, filtered; `before_id` pages back."""
        q, a = "SELECT * FROM images WHERE 1=1", []
        for col, v in (("status", status), ("band", band), ("mode", mode), ("source", source)):
            if v:
                q += f" AND {col} = ?"
                a.append(v)
        if since:
            q += " AND start_utc >= ?"
            a.append(since)
        if until:
            q += " AND start_utc < ?"
            a.append(until)
        if before_id:
            q += " AND id < ?"
            a.append(before_id)
        q += " ORDER BY start_utc DESC, id DESC LIMIT ?"
        a.append(max(1, min(500, limit)))
        return [self._row(r) for r in self.db.execute(q, a)]

    def facets(self) -> dict[str, Any]:
        """What the gallery filters offer: the bands and modes seen, and counts by status."""
        bands = [r[0] for r in self.db.execute("SELECT DISTINCT band FROM images WHERE status='decoded' ORDER BY band")]
        modes = [r[0] for r in self.db.execute("SELECT DISTINCT mode FROM images WHERE status='decoded' ORDER BY mode")]
        counts = {r[0]: r[1] for r in self.db.execute("SELECT status, COUNT(*) FROM images GROUP BY status")}
        return {"bands": bands, "modes": modes, "counts": counts}

    def unnotified(self) -> list[dict[str, Any]]:
        return [self._row(r) for r in self.db.execute(
            "SELECT * FROM images WHERE status='decoded' AND source='watch' AND notified_at IS NULL ORDER BY id")]
