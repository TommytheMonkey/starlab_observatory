"""Post-pass reconciliation against the legacy recorder's segment metadata.

record.py writes <serial>_<UTC>.json next to each .cu8 segment, including the
pass window it served. The recorder deletes segments after `retention_hours`
(6 h today) when the next pass starts, so outcomes must be captured promptly
and persisted in our database.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

from ..timeutil import iso, parse_utc
from . import FAILED, MISSED, RECORDED, RECORDING, WARNING

SEGMENT_JSON = re.compile(r"^(OBS-R\d+)_\d{8}T\d{6}\.\d{6}Z\.json$")


@dataclass
class SegmentRecord:
    base: str
    metadata_path: pathlib.Path
    raw_path: pathlib.Path
    started_at: dt.datetime
    finished_at: dt.datetime
    status: str
    raw_bytes: int
    expected_bytes: int
    raw_exists: bool
    raw_size_now: int
    meta: dict[str, Any]


@dataclass
class Outcome:
    status: str
    reason: str
    segments: list[SegmentRecord] = field(default_factory=list)
    actual_start: dt.datetime | None = None
    actual_end: dt.datetime | None = None
    recorded_bytes: int = 0
    coverage: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status, "reason": self.reason, "coverage": round(self.coverage, 3),
            "actual_start": iso(self.actual_start), "actual_end": iso(self.actual_end),
            "recorded_bytes": self.recorded_bytes,
            "segments": [{
                "base": s.base, "status": s.status, "started_at": iso(s.started_at), "finished_at": iso(s.finished_at),
                "raw_bytes": s.raw_bytes, "expected_bytes": s.expected_bytes, "raw_path": str(s.raw_path),
                "raw_exists": s.raw_exists, "error": s.meta.get("error"), "rtl_sdr_return_code": s.meta.get("rtl_sdr_return_code"),
            } for s in self.segments],
        }


def load_segments(directory: pathlib.Path) -> list[SegmentRecord]:
    out = []
    for path in sorted(directory.glob("*.json")):
        if not SEGMENT_JSON.fullmatch(path.name) or path.is_symlink():
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            raw = directory / str(meta["raw_file"])
            out.append(SegmentRecord(
                base=path.name.removesuffix(".json"),
                metadata_path=path,
                raw_path=raw,
                started_at=parse_utc(meta["started_at"]),
                finished_at=parse_utc(meta["finished_at"]),
                status=str(meta.get("status")),
                raw_bytes=int(meta.get("raw_bytes") or 0),
                expected_bytes=int(meta.get("expected_bytes") or 0),
                raw_exists=raw.is_file(),
                raw_size_now=raw.stat().st_size if raw.is_file() else 0,
                meta=meta,
            ))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return out


def segments_for(segments: list[SegmentRecord], norad_id: int, threshold_rise: dt.datetime,
                 capture_start: dt.datetime, capture_end: dt.datetime) -> list[SegmentRecord]:
    """Segments whose embedded pass metadata names this satellite pass (rise within 5 min),
    falling back to time overlap for segments without pass metadata."""
    found = []
    for seg in segments:
        sats = (seg.meta.get("pass") or {}).get("satellites") or []
        if sats:
            for s in sats:
                try:
                    if int(s["norad_id"]) == norad_id and abs((parse_utc(s["rise"]) - threshold_rise).total_seconds()) < 300:
                        found.append(seg)
                        break
                except (KeyError, ValueError):
                    continue
        elif seg.started_at < capture_end and seg.finished_at > capture_start:
            found.append(seg)
    return sorted(found, key=lambda s: s.started_at)


def reconcile(norad_id: int, threshold_rise: dt.datetime, capture_start: dt.datetime, capture_end: dt.datetime,
              directory: pathlib.Path, now: dt.datetime, grace: dt.timedelta, min_coverage: float,
              lease_owner: str | None, part_files: list[pathlib.Path],
              retention: dt.timedelta = dt.timedelta(hours=6)) -> Outcome | None:
    """Return the current outcome, or None while the window has not opened yet."""
    if now < capture_start:
        return None
    segs = segments_for(load_segments(directory), norad_id, threshold_rise, capture_start, capture_end)
    window = (capture_end - capture_start).total_seconds()

    if now < capture_end + grace and (now < capture_end or part_files):
        if part_files or lease_owner:
            return Outcome(RECORDING, f"Capture in progress (lease={lease_owner}, active={[p.name for p in part_files]})", segs)
        if now < capture_end:
            return Outcome(WARNING, "Recording window is open but no active capture or recorder lease is visible", segs)

    if now < capture_end + grace:
        return Outcome(RECORDING, "Window closed; waiting for recorder metadata", segs)

    if not segs:
        return Outcome(MISSED, "No recorder segment metadata exists for this pass")

    covered = sum(max(0.0, (min(s.finished_at, capture_end) - max(s.started_at, capture_start)).total_seconds()) for s in segs)
    coverage = covered / window if window > 0 else 0.0
    total = sum(s.raw_bytes for s in segs)
    bad = [s for s in segs if s.status != "recorded" or s.raw_bytes != s.expected_bytes]
    outcome = Outcome(FAILED, "", segs, segs[0].started_at, segs[-1].finished_at, total, coverage)
    if bad:
        outcome.reason = "; ".join(f"{s.base}: {s.status} ({s.raw_bytes}/{s.expected_bytes} bytes, error={s.meta.get('error')})" for s in bad)
    elif total <= 0:
        outcome.reason = "Segments contain no data"
    elif coverage < min_coverage:
        outcome.reason = f"Only {coverage:.0%} of the capture window was recorded"
    elif vanished := [s for s in segs if (not s.raw_exists or s.raw_size_now != s.raw_bytes) and now - s.finished_at < retention]:
        outcome.reason = "Raw file missing or truncated: " + ", ".join(f"{s.base} ({s.raw_size_now}/{s.raw_bytes} bytes)" for s in vanished)
    else:
        outcome.status = RECORDED
        missing = [s.base for s in segs if not s.raw_exists]
        outcome.reason = (f"{len(segs)} segment(s), {total / 1e9:.2f} GB, {coverage:.0%} window coverage"
                          + (f"; raw files since removed by recorder retention: {missing}" if missing else ""))
    return outcome
