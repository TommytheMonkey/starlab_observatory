"""Processing attempts (decode / analyze) over recordings, and the unified artifact index.

* Every attempt is immutable: its own id, parameters, exact command, output directory and
  result. Re-running with different parameters makes a new attempt; nothing is overwritten.
* Attempts run one at a time as `nice -n 19 ionice -c 3` processes. While any recording is
  preparing or recording, or one starts within `processing_gate_seconds`, no attempt is started,
  and a running one is paused with SIGSTOP and resumed with SIGCONT afterwards.
* The artifact index has one row per file (raw IQ, sidecar, capture log, waterfall,
  spectrum, diagnostics, SatDump images/products/logs). It is keyed by recording id and
  attempt id, so everything for a recording can be listed and fetched by artifact id.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import mimetypes
import os
import pathlib
import re
import secrets
import signal
import subprocess
import sys
import threading
from typing import Any, Callable

from sqlalchemy import and_, or_, select

from ..db.schema import artifacts, proc_attempts, sched_jobs
from ..logjson import log_event
from .postprocess import write_sidecar
from .store import Scheduler, SchedulerError

LOG = logging.getLogger("aobs.processing")
ANALYZE_TASKS = ("checksum", "waterfall", "spectrum", "stats")
SAFE_PARAM = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:+-]{0,64}$")
MAX_FETCH = 100 * 1024**2
TERMINAL = ("completed", "failed", "cancelled")


def _iso_val(v: Any) -> str:
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _now_id(prefix: str, now: dt.datetime) -> str:
    return f"{prefix}{now:%Y%m%d%H%M%S}{secrets.token_hex(3)}"


def _analysis_aliases(p: dict[str, Any]) -> dict[str, Any]:
    """Accept the short names (tasks, nfft, center_hz/span_hz) as well as the documented ones."""
    p = dict(p)
    if "tasks" in p:
        p["analysis"] = p.pop("tasks")
    if "nfft" in p:
        p["fft_size"] = p.pop("nfft")
    if "center_hz" in p or "span_hz" in p:
        c, s = p.pop("center_hz", None), p.pop("span_hz", None)
        if c is None or s is None:
            raise SchedulerError("zoom needs both center and span (or frequency_min_hz/frequency_max_hz)", 400)
        p["frequency_min_hz"], p["frequency_max_hz"] = int(c - s / 2), int(c + s / 2)
    if "stats" in (p.get("analysis") or []):
        p["analysis"] = [t for t in p["analysis"] if t != "stats"] + ["clipping", "signal_quality"]
    return p


class Processor:
    def __init__(self, scheduler: Scheduler, spawn: Callable[[list[str], str], Any] | None = None,
                 alive: Callable[[int, str], bool] | None = None, signal_group: Callable[[int, int], None] | None = None):
        self.s = scheduler
        self._spawn = spawn or _spawn
        self._alive = alive or _alive
        self._signal = signal_group or _signal_group
        self._lock = threading.RLock()
        self.current: dict[str, Any] | None = None   # {attempt_id, pid, handle, paused_at}
        self._stop = threading.Event()
        st = scheduler.settings
        self.gate_s = int(st.get("processing_gate_seconds", 120))
        self.root = pathlib.Path(st.get("processing_root", str(scheduler.config.data_root / "processing")))

    # ================================================================ inputs
    def inputs(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        spec = rec["spec"]
        radio, recd = spec.get("radio") or {}, spec.get("recording") or {}
        cap = (rec.get("result") or {}).get("capture") or {}
        gain = radio.get("gain_db") if radio.get("gain_db") is not None else (radio.get("gain_elements") or radio.get("gain_mode"))
        base = {"format": recd.get("format"), "sample_rate_hz": radio.get("sample_rate_hz"),
                "center_frequency_hz": radio.get("center_frequency_hz"),
                "target_frequency_hz": radio.get("target_frequency_hz"), "recording_id": rec["job_id"],
                "receiver_id": rec["receiver_id"], "record_start": rec["record_start"], "record_end": rec["record_end"],
                "gain": gain, "expected_bytes": cap.get("expected_bytes"), "capture_overflows": cap.get("overflows"),
                "metadata_bytes": cap.get("bytes"),
                # for the caption burned into every plot
                "target": rec.get("target") or (spec.get("target") or {}).get("name"),
                "antenna": (spec.get("receiver") or {}).get("antenna") or getattr(self.s.receivers.get(rec["receiver_id"]), "antenna", None),
                "display_tz": self.s.config.display_tz}
        if rec["source"] == "legacy":
            files = [f for f in (rec.get("result") or {}).get("legacy_files", [])
                     if f.get("status") in ("recorded", "completed") and pathlib.Path(f["raw"]).is_file()]
            return [{**base, "path": f["raw"], "metadata_bytes": f.get("bytes"), "expected_bytes": None,
                     "record_start": f.get("started_at"), "record_end": f.get("finished_at")} for f in files]
        raw = (rec.get("paths") or {}).get("raw")
        if not (raw and pathlib.Path(raw).is_file()):
            raw = cap.get("file")                       # a partial recording keeps its .part file
        return [{**base, "path": raw}] if raw and pathlib.Path(raw).is_file() else []

    def _recording(self, recording_id: str) -> dict[str, Any]:
        rec = self.s.get(recording_id)
        if rec is None:
            raise SchedulerError(f"no recording {recording_id}", 404)
        if rec["status"] not in ("recorded", "processing", "completed", "failed", "partial"):
            raise SchedulerError(f"{recording_id} is {rec['status']}; there is no recording to process yet")
        return rec

    # ================================================================ submit
    def submit(self, recording_id: str, kind: str, params: dict[str, Any], origin: str = "api",
               requested_by: str = "") -> dict[str, Any]:
        rec = self._recording(recording_id)
        ins = self.inputs(rec)
        if not ins:
            raise SchedulerError(f"{recording_id} has no raw IQ on disk any more (retention or retain_raw_iq=false)", 410)
        if any(i["center_frequency_hz"] is None for i in ins) and kind != "compare":
            raise SchedulerError(f"{recording_id}: center frequency unknown (imported without evidence); correct its metadata first", 400)
        if any(i["format"] is None or i["sample_rate_hz"] is None for i in ins):
            raise SchedulerError(f"{recording_id}: format or sample rate unknown; cannot process")
        if kind == "compare":
            others = [r for r in (params or {}).get("recordings", []) if r != recording_id]
            if not 1 <= len(others) <= 3:
                raise SchedulerError("compare needs 2-4 recordings", 400)
            for o in others:
                more = self.inputs(self._recording(o))
                if not more:
                    raise SchedulerError(f"{o} has no raw IQ on disk", 410)
                ins.append(more[0])
            ins = [ins[0]] + ins[len(ins) - len(others):]
        params = dict(params or {})
        if kind == "decode" and not params.get("pipeline"):
            sd = (rec["spec"].get("satdump") or {})
            if sd.get("pipeline"):
                params["pipeline"] = sd["pipeline"]
            elif "METEOR" in (rec.get("target") or "").upper():
                params["pipeline"] = "meteor_m2-x_lrpt"     # the recording's target is a METEOR satellite
            else:
                raise SchedulerError("no SatDump pipeline given and the recording does not name one; add pipeline=…", 400)
        resolved = self._validate(kind, params, ins[0])
        if kind == "compare":
            resolved["recordings"] = [i["recording_id"] for i in ins]
        now = self.s.clock()
        attempt_id = _now_id("p", now)
        out = self._out_dir(rec, attempt_id)
        with self.s.engine.begin() as conn:
            conn.execute(proc_attempts.insert().values(
                attempt_id=attempt_id, recording_id=recording_id, kind=kind, params=params or {}, resolved=resolved,
                status="queued", origin=origin, requested_by=requested_by, output_dir=str(out), queued_at=now,
                paused_seconds=0.0))
        log_event(LOG, "attempt queued", attempt_id=attempt_id, recording_id=recording_id, kind=kind, origin=origin)
        return self.attempt(attempt_id)

    def _out_dir(self, rec: dict[str, Any], attempt_id: str) -> pathlib.Path:
        if rec["source"] not in ("legacy", "import") and (rec.get("paths") or {}).get("directory"):
            return pathlib.Path(rec["paths"]["directory"]) / "processing" / rec["job_id"] / attempt_id
        return self.root / rec["job_id"] / attempt_id   # legacy outputs live in directories the old recorder prunes

    def _validate(self, kind: str, p: dict[str, Any], first: dict[str, Any]) -> dict[str, Any]:
        rate = int(first["sample_rate_hz"])
        for k in ("start_s", "end_s"):
            if p.get(k) is not None and (not isinstance(p[k], (int, float)) or p[k] < 0):
                raise SchedulerError(f"{k} must be a non-negative number of seconds", 400)
        if p.get("start_s") is not None and p.get("end_s") is not None and p["end_s"] <= p["start_s"]:
            raise SchedulerError("end_s must be after start_s", 400)
        if kind == "decode":
            from . import dsp
            allowed = {"pipeline", "target_frequency_hz", "frequency_offset_hz", "output_sample_rate_hz", "shift_method",
                       "search_offsets_hz",
                       "additional_parameters", "start_s", "end_s", "keep_intermediate", "input_type",
                       "baseband_format", "sample_rate_hz", "priority", "output_directory"}
            if set(p) - allowed:
                raise SchedulerError(f"unknown decode parameters {sorted(set(p) - allowed)}", 400)
            if not p.get("pipeline"):
                raise SchedulerError("decode needs a SatDump pipeline (see /radio-capabilities for installed ones)", 400)
            if p["pipeline"] not in self.s.pipelines:
                raise SchedulerError(f"SatDump pipeline {p['pipeline']!r} is not installed", 400)
            # The recording's own metadata is authoritative; overrides that contradict it are refused.
            for key, actual in (("baseband_format", first["format"]), ("sample_rate_hz", rate)):
                if p.get(key) is not None and p[key] != actual:
                    raise SchedulerError(f"{key} {p[key]} does not match the recording ({actual})", 400)
            if p.get("input_type", "baseband") != "baseband":
                raise SchedulerError("input_type must be baseband (raw IQ)", 400)
            if p.get("output_directory", "auto") != "auto":
                raise SchedulerError("output_directory must be auto: every attempt gets its own directory", 400)
            center = int(first["center_frequency_hz"])
            target, offset = p.get("target_frequency_hz"), p.get("frequency_offset_hz")
            if target is not None and offset is not None and int(target) - center != int(offset):
                raise SchedulerError(f"target_frequency_hz and frequency_offset_hz disagree (center {center})", 400)
            if offset is None:
                target = target if target is not None else first.get("target_frequency_hz")
                if target is None:
                    raise SchedulerError("this recording has no target frequency on record; give target_frequency_hz, "
                                         "or frequency_offset_hz=0 to decode at the center frequency", 400)
                offset = int(target) - center
            target = center + int(offset)
            method = p.get("shift_method") or ("internal" if offset else "none")
            if method not in ("internal", "satdump", "none"):
                raise SchedulerError("shift_method must be internal, satdump or none", 400)
            if method == "none" and offset:
                raise SchedulerError(f"the target is {offset / 1e3:+.0f} kHz from center; shift_method none would "
                                     "decode the wrong frequency", 400)
            plan = None
            if method == "internal":
                try:
                    plan = dsp.plan_channel(rate, center, target, p.get("output_sample_rate_hz"))
                except ValueError as exc:
                    raise SchedulerError(str(exc), 400) from None
            elif p.get("output_sample_rate_hz"):
                raise SchedulerError("output_sample_rate_hz needs shift_method internal", 400)
            elif abs(offset) >= rate / 2 * dsp.GUARD:
                raise SchedulerError("the target lies outside the recorded passband", 400)
            for k, v in (p.get("additional_parameters") or {}).items():
                if k in ("samplerate", "baseband_format", "freq_shift") or not SAFE_PARAM.match(k) or \
                        not isinstance(v, (bool, int, float, str)) or (isinstance(v, str) and not SAFE_VALUE.match(v)):
                    raise SchedulerError(f"additional parameter {k!r} is not allowed", 400)
            search = p.get("search_offsets_hz")
            if search is None and method == "internal" and p["pipeline"].startswith("meteor"):
                # Doppler (±3.3 kHz at 137.9 MHz) plus receiver error exceed SatDump's carrier lock range, which was
                # measured at about ±1 kHz on r202610130140f5031c (137.896 locked, 137.894 did not): 1 kHz steps
                search = sorted(range(-6000, 6001, 1000), key=lambda o: (abs(o), o))
            if search is not None:
                if method != "internal":
                    raise SchedulerError("search_offsets_hz needs shift_method internal", 400)
                if not (isinstance(search, list) and 1 <= len(search) <= 15 and
                        all(isinstance(o, int) and abs(o) <= 20_000 for o in search)):
                    raise SchedulerError("search_offsets_hz must be a list of 1-15 integers within ±20000 Hz", 400)
            return {"pipeline": p["pipeline"], "center_frequency_hz": center, "target_frequency_hz": target,
                    "search_offsets_hz": search,
                    "frequency_offset_hz": int(offset), "shift_method": method,
                    "input_sample_rate_hz": rate, "baseband_format": first["format"],
                    "output_sample_rate_hz": plan.output_sample_rate_hz if plan else rate,
                    "channel_plan": plan.as_dict() if plan else None,
                    "additional_parameters": p.get("additional_parameters") or {}, "keep_intermediate": bool(p.get("keep_intermediate")),
                    "start_s": p.get("start_s"), "end_s": p.get("end_s"), "priority": "nice 19, ionice idle"}
        if kind in ("analyze", "compare"):
            from . import analysis as an
            p = _analysis_aliases(p)
            allowed = {"analysis", "fft_size", "fft_window", "overlap", "averages", "rows", "frequency_min_hz",
                       "frequency_max_hz", "start_s", "end_s", "dynamic_range_db", "colormap", "width", "height",
                       "target_frequency_hz", "threshold_db", "search_hz", "output_format", "recordings",
                       "channel_bandwidth_hz"}
            if set(p) - allowed:
                raise SchedulerError(f"unknown analyze parameters {sorted(set(p) - allowed)}", 400)
            tasks = p.get("analysis") or list(an.DEFAULT_TASKS)
            if isinstance(tasks, str):
                tasks = [tasks]
            if tasks == ["all"]:
                tasks = list(an.TASKS)
            if not set(tasks) <= set(an.TASKS):
                raise SchedulerError(f"analysis must be among {list(an.TASKS)} (or all)", 400)
            nfft = int(p.get("fft_size", 2048))
            if nfft < 256 or nfft > 65536 or nfft & (nfft - 1):
                raise SchedulerError("fft_size must be a power of two, 256-65536", 400)
            rows = int(p.get("rows", 600))
            if not 10 <= rows <= 2000:
                raise SchedulerError("rows must be 10-2000", 400)
            if p.get("fft_window", "hann") not in an.WINDOWS:
                raise SchedulerError(f"fft_window must be one of {list(an.WINDOWS)}", 400)
            if not 0 <= float(p.get("overlap", 0)) < 0.95:
                raise SchedulerError("overlap must be 0-0.95", 400)
            if p.get("colormap", "viridis") not in an.COLORMAPS:
                raise SchedulerError(f"colormap must be one of {list(an.COLORMAPS)}", 400)
            if p.get("output_format", "png") != "png":
                raise SchedulerError("output_format png is the only one supported", 400)
            for k, lo, hi in (("width", 400, 4000), ("height", 300, 4000), ("dynamic_range_db", 10, 150)):
                if p.get(k) is not None and not lo <= float(p[k]) <= hi:
                    raise SchedulerError(f"{k} must be {lo}-{hi}", 400)
            c = int(first["center_frequency_hz"])
            for k in ("frequency_min_hz", "frequency_max_hz"):
                if p.get(k) is not None and abs(int(p[k]) - c) > rate / 2:
                    raise SchedulerError(f"{k} {p[k]} is outside the recorded band {c - rate // 2}-{c + rate // 2}", 400)
            if p.get("frequency_min_hz") is not None and p.get("frequency_max_hz") is not None \
                    and p["frequency_max_hz"] <= p["frequency_min_hz"]:
                raise SchedulerError("frequency_max_hz must be above frequency_min_hz", 400)
            return {**{k: v for k, v in p.items() if k != "recordings"}, "analysis": tasks, "fft_size": nfft, "rows": rows,
                    "priority": "nice 19, ionice idle"}
        raise SchedulerError("kind must be decode, analyze or compare", 400)

    def submit_post_recording(self, job: dict[str, Any]) -> list[str]:
        """The processing a job asked for in its request, as ordinary attempts (origin post-recording)."""
        spec = job["spec"]
        pp = spec.get("postprocess") or {}
        made = []
        tasks = [t for t, on in (("checksum", pp.get("checksum", True)), ("waterfall", pp.get("generate_waterfall")),
                                 ("spectrum", pp.get("generate_spectrum")),
                                 ("signal_detection", pp.get("analyze_signal_quality", True)),
                                 ("signal_quality", pp.get("analyze_signal_quality", True)), ("clipping", True),
                                 ("integrity", True), ("rf_test", pp.get("rf_test"))) if on]
        params: dict[str, Any] = {"analysis": tasks}
        if pp.get("rf_test"):          # signal-path test: fixed, documented settings so tests stay comparable
            from .testlab import ANALYSIS_PARAMS
            params.update(ANALYSIS_PARAMS)
            tgt = (spec.get("radio") or {}).get("target_frequency_hz")
            if tgt:
                params["target_frequency_hz"] = int(tgt)
            if pp.get("channel_bandwidth_hz"):
                params["channel_bandwidth_hz"] = int(pp["channel_bandwidth_hz"])
        made.append(self.submit(job["job_id"], "analyze", params, "post-recording", "scheduler")["attempt_id"])
        sd = spec.get("satdump") or {}
        if sd.get("enabled") and sd.get("operation") == "decode_after_recording" and sd.get("pipeline"):
            params: dict[str, Any] = {"pipeline": sd["pipeline"]}
            if sd.get("frequency_hz"):
                params["target_frequency_hz"] = int(sd["frequency_hz"])
            if sd.get("additional_parameters"):
                params["additional_parameters"] = sd["additional_parameters"]
            made.append(self.submit(job["job_id"], "decode", params, "post-recording", "scheduler")["attempt_id"])
        return made

    # ================================================================ queries
    def attempt(self, attempt_id: str) -> dict[str, Any]:
        with self.s.engine.connect() as conn:
            row = conn.execute(select(proc_attempts).where(proc_attempts.c.attempt_id == attempt_id)).mappings().first()
            if row is None:
                raise SchedulerError(f"no attempt {attempt_id}", 404)
            arts = conn.execute(select(artifacts).where(artifacts.c.attempt_id == attempt_id)
                                .order_by(artifacts.c.kind, artifacts.c.path)).mappings().all()
        return {**_iso(row), "artifacts": [_iso(a) for a in arts]}

    def attempts(self, recording_id: str) -> list[dict[str, Any]]:
        with self.s.engine.connect() as conn:
            rows = conn.execute(select(proc_attempts).where(proc_attempts.c.recording_id == recording_id)
                                .order_by(proc_attempts.c.queued_at)).mappings().all()
        return [_iso(r) for r in rows]

    def artifacts(self, recording_id: str, kind: str | None = None, attempt_id: str | None = None) -> list[dict[str, Any]]:
        q = select(artifacts).where(and_(artifacts.c.recording_id == recording_id, artifacts.c.deleted_at.is_(None)))
        if kind:
            q = q.where(artifacts.c.kind.in_(kind.split(",")))
        if attempt_id:
            q = q.where(artifacts.c.attempt_id == attempt_id)
        with self.s.engine.connect() as conn:
            rows = conn.execute(q.order_by(artifacts.c.created_at, artifacts.c.path)).mappings().all()
        return [_iso(r) for r in rows]

    ARTIFACT_ID = re.compile(r"^f\d{14}[0-9a-f]{6}$")

    def _artifact(self, artifact_id: str) -> tuple[dict[str, Any], pathlib.Path]:
        """Resolve an artifact id to its indexed file. Paths come only from the index, and the resolved
        file (symlinks followed) must still lie under an allowed output root."""
        if not self.ARTIFACT_ID.match(artifact_id or ""):
            raise SchedulerError(f"{artifact_id!r} is not an artifact id (expected f + 14 digits + 6 hex, "
                                 "from /radio-results)", 400)
        with self.s.engine.connect() as conn:
            row = conn.execute(select(artifacts).where(artifacts.c.artifact_id == artifact_id)).mappings().first()
        if row is None:
            raise SchedulerError(f"no artifact {artifact_id} in the index", 404)
        p = pathlib.Path(row["path"])
        if row["deleted_at"] is not None:
            raise SchedulerError(f"artifact {artifact_id} ({p.name}) was removed from disk "
                                 f"(noticed {_iso_val(row['deleted_at'])}); retention may have deleted it", 410)
        try:
            real = p.resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError):
            raise SchedulerError(f"artifact {artifact_id}: {p.name} no longer exists at {p.parent}", 410) from None
        roots = [pathlib.Path(r).resolve() for r in self.s.settings.get("allowed_output_roots", ["/data"])]
        if not any(real == r or r in real.parents for r in roots) or not real.is_file():
            raise SchedulerError(f"artifact {artifact_id} points outside the observatory data directories; refused", 403)
        return dict(row), real

    def artifact_file(self, artifact_id: str) -> tuple[pathlib.Path, str]:
        row, p = self._artifact(artifact_id)
        if row["kind"] == "raw_iq" or p.stat().st_size > MAX_FETCH:
            raise SchedulerError(f"{p.name} is too large to fetch here; it is at observatory:{p}", 413)
        return p, row["mime"] or "application/octet-stream"

    TEXT_SUFFIXES = (".log", ".json", ".txt", ".csv")
    TEXT_KINDS = ("log", "capture_log", "diagnostics", "metadata", "product")

    def artifact_text(self, artifact_id: str, tail_lines: int = 200, contains: str = "",
                      max_bytes: int = 60_000) -> dict[str, Any]:
        """Read-only view of a text artifact (logs, result JSON, diagnostics, metadata), by artifact id only."""
        row, p = self._artifact(artifact_id)
        if row["kind"] not in self.TEXT_KINDS or p.suffix.lower() not in self.TEXT_SUFFIXES:
            raise SchedulerError(f"{p.name} is not a text artifact ({row['kind']}); only logs, JSON and diagnostics can be read", 415)
        size = p.stat().st_size
        with p.open("rb") as fh:
            if size > 8 * 1024**2:                          # huge logs: only the end is ever useful
                fh.seek(size - 8 * 1024**2)
            text = fh.read().decode("utf-8", errors="replace")
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        lines = text.splitlines()
        if contains:
            needle = contains.lower()[:80]
            lines = [ln for ln in lines if needle in ln.lower()]
        total = len(lines)
        tail_lines = max(1, min(int(tail_lines or 200), 2000))
        lines = lines[-tail_lines:]
        body = "\n".join(lines)
        truncated = len(body.encode()) > max_bytes
        if truncated:
            body = body.encode()[-max_bytes:].decode("utf-8", errors="replace")
        return {"artifact_id": artifact_id, "recording_id": row["recording_id"], "attempt_id": row["attempt_id"],
                "kind": row["kind"], "file": p.name, "bytes": size, "matching_lines": total,
                "returned_lines": len(lines), "truncated": truncated, "text": body}

    # ================================================================ indexing
    def index(self, recording_id: str, attempt_id: str | None, items: list[dict[str, str]]) -> int:
        now = self.s.clock()
        n = 0
        with self.s.engine.begin() as conn:
            for it in items:
                p = pathlib.Path(it["path"])
                if not p.is_file():
                    continue
                exists = conn.execute(select(artifacts.c.artifact_id).where(artifacts.c.path == str(p))).first()
                values = {"bytes": p.stat().st_size, "deleted_at": None, "label": it.get("label")}
                if exists:
                    conn.execute(artifacts.update().where(artifacts.c.path == str(p)).values(**values))
                    continue
                conn.execute(artifacts.insert().values(
                    artifact_id=_now_id("f", now), recording_id=recording_id, attempt_id=attempt_id, kind=it["kind"],
                    path=str(p), mime=mimetypes.guess_type(p.name)[0] or "application/octet-stream", created_at=now,
                    **values))
                n += 1
        return n

    def index_capture(self, job: dict[str, Any]) -> None:
        paths = job.get("paths") or {}
        items = [{"kind": "raw_iq", "path": paths.get("raw", ""), "label": "raw IQ"},
                 {"kind": "capture_log", "path": paths.get("log", ""), "label": "capture log"},
                 {"kind": "metadata", "path": paths.get("sidecar", ""), "label": "recording metadata (JSON sidecar)"}]
        if job["source"] == "legacy":
            items = []
            for f in (job.get("result") or {}).get("legacy_files", []):
                items += [{"kind": "raw_iq", "path": f["raw"], "label": "raw IQ (legacy recorder)"},
                          {"kind": "metadata", "path": f["metadata"], "label": "legacy recorder metadata"}]
        self.index(job["job_id"], None, [i for i in items if i["path"]])

    def sync_legacy(self) -> int:
        """Index the files of legacy recordings the planner has reconciled, so they get recording ids too."""
        with self.s.engine.connect() as conn:
            indexed = {r[0] for r in conn.execute(select(artifacts.c.recording_id).distinct())}
            rows = conn.execute(select(sched_jobs.c.job_id).where(and_(
                sched_jobs.c.source == "legacy", sched_jobs.c.status.in_(("recorded", "failed"))))).scalars().all()
        n = 0
        for jid in rows:
            if jid not in indexed:
                job = self.s.get(jid)
                if job and (job.get("result") or {}).get("legacy_files"):
                    if getattr(self.s, "rf", None) is not None:
                        try:                  # the layout in effect when the legacy recorder started
                            self.s.rf.stamp(job, at_record_start=True)
                            job = self.s.get(jid) or job
                        except Exception:  # noqa: BLE001
                            LOG.exception("could not record the signal path of %s", jid)
                    self.index_capture(job)
                    n += 1
                    self._assess_legacy(job)
        return n

    def assess_capture(self, job: dict[str, Any], capture: dict[str, Any]) -> dict[str, Any] | None:
        from .rxhealth import assess_capture
        return assess_capture(self, job, capture)

    def _assess_legacy(self, job: dict[str, Any]) -> None:
        from .rxhealth import assess_legacy
        try:
            res = assess_legacy(self, job)
        except Exception:  # noqa: BLE001 - health is advisory; indexing must go on
            LOG.exception("IQ health assessment failed for %s", job["job_id"])
            return
        if res:
            fresh = self.s.get(job["job_id"]) or job
            self.s.update(job["job_id"], result={**(fresh.get("result") or {}), "iq_health": res["iq_health"],
                                                 "tuner": res.get("tuner"), "iq_health_files": res.get("files")})

    def mark_missing(self) -> int:
        """Retention deletes raw IQ; keep the index honest."""
        now = self.s.clock()
        gone = 0
        with self.s.engine.begin() as conn:
            for r in conn.execute(select(artifacts.c.artifact_id, artifacts.c.path).where(artifacts.c.deleted_at.is_(None))):
                if not os.path.exists(r.path):
                    conn.execute(artifacts.update().where(artifacts.c.artifact_id == r.artifact_id).values(deleted_at=now))
                    gone += 1
        return gone

    # ================================================================ execution
    def busy_recording(self) -> list[str]:
        """Recordings that are running now or start within the gate (any receiver, any source)."""
        now = self.s.clock()
        soon = now + dt.timedelta(seconds=self.gate_s)
        with self.s.engine.connect() as conn:
            rows = conn.execute(select(sched_jobs.c.job_id, sched_jobs.c.receiver_id, sched_jobs.c.status).where(or_(
                sched_jobs.c.status.in_(("preparing", "recording")),
                and_(sched_jobs.c.status == "scheduled", sched_jobs.c.record_start <= soon,
                     sched_jobs.c.record_end > now)))).all()
        return [f"{r.receiver_id}:{r.job_id}" for r in rows]

    def step(self) -> None:
        with self._lock:
            busy = self.busy_recording()
            cur = self.current
            if cur:
                done = cur["handle"].poll() is not None if cur.get("handle") is not None else not self._alive(cur["pid"], cur["attempt_id"])
                if done:
                    if cur.get("paused_at"):
                        self._resume(cur)
                    self._finish(cur["attempt_id"])
                    self.current = None
                elif busy and not cur.get("paused_at"):
                    self._signal(cur["pid"], signal.SIGSTOP)
                    cur["paused_at"] = self.s.clock()
                    self._set(cur["attempt_id"], status="paused", status_reason=f"paused for recording {busy[0]}")
                elif not busy and cur.get("paused_at"):
                    self._resume(cur)
                return
            if busy:
                return
            with self.s.engine.connect() as conn:
                nxt = conn.execute(select(proc_attempts).where(proc_attempts.c.status == "queued")
                                   .order_by(proc_attempts.c.queued_at).limit(1)).mappings().first()
            if nxt is not None:
                self._launch(dict(nxt))

    def _resume(self, cur: dict[str, Any]) -> None:
        self._signal(cur["pid"], signal.SIGCONT)
        paused = (self.s.clock() - cur.pop("paused_at")).total_seconds()
        with self.s.engine.begin() as conn:
            row = conn.execute(select(proc_attempts.c.paused_seconds).where(
                proc_attempts.c.attempt_id == cur["attempt_id"])).first()
            conn.execute(proc_attempts.update().where(proc_attempts.c.attempt_id == cur["attempt_id"]).values(
                status="running", status_reason="resumed", paused_seconds=(row.paused_seconds or 0) + paused))

    def _launch(self, att: dict[str, Any]) -> None:
        rec = self.s.get(att["recording_id"])
        out = pathlib.Path(att["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        params = {**att["resolved"]}
        ins = self.inputs(rec) if rec else []
        if att["kind"] == "compare":
            ins = [self.inputs(self.s.get(r))[0] for r in params.get("recordings", []) if self.s.get(r) and self.inputs(self.s.get(r))]
        if not ins:
            self._set(att["attempt_id"], status="failed", status_reason="raw IQ is gone", finished_at=self.s.clock())
            return
        spec_path = out / "attempt.json"
        spec_path.write_text(json.dumps({"attempt_id": att["attempt_id"], "kind": att["kind"], "inputs": ins,
                                         "params": params, "output_dir": str(out), "settings": self.s.settings},
                                        indent=2, default=str))
        argv = ["nice", "-n", "19", "ionice", "-c", "3", sys.executable, "-m", "aobs.scheduling.procworker", str(spec_path)]
        handle = self._spawn(argv, str(out / "worker.log"))
        self.current = {"attempt_id": att["attempt_id"], "pid": handle.pid, "handle": handle}
        self._set(att["attempt_id"], status="running", status_reason=None, pid=handle.pid, started_at=self.s.clock())
        log_event(LOG, "attempt started", attempt_id=att["attempt_id"], kind=att["kind"], pid=handle.pid)

    def _finish(self, attempt_id: str) -> None:
        att = self.attempt(attempt_id)
        out = pathlib.Path(att["output_dir"])
        try:
            result = json.loads((out / "result.json").read_text())
        except (OSError, ValueError):
            result = {"ok": False, "errors": ["worker ended without writing result.json"], "artifacts": []}
        items = list(result.get("artifacts", [])) + [
            {"kind": "diagnostics", "path": str(out / "result.json"), "label": "attempt result (JSON)"},
            {"kind": "log", "path": str(out / "worker.log"), "label": "worker log"}]
        self.index(att["recording_id"], attempt_id, items)
        status = "completed" if result.get("ok") else "failed"
        reason = "; ".join(result.get("errors", [])) or None
        if att["kind"] == "decode" and status == "completed" and not any(i.get("decoded") for i in result.get("inputs", [])):
            # SatDump ran, but produced no image with real content: that is a failed decode, not a success
            status = "failed"
            sync = [f"sync reports {i.get('synced_reports', 0)}/{i.get('nosync_reports', 0)} (synced/nosync), "
                    f"images {i.get('images', 0)} real + {i.get('images_blank', 0)} blank" for i in result.get("inputs", [])]
            reason = "no decoded images (no frame sync)" + (f": {'; '.join(sync)}" if sync else "") + \
                     (f"; {reason}" if reason else "")
        self._set(attempt_id, status=status, status_reason=reason, result=result, finished_at=self.s.clock())
        log_event(LOG, "attempt finished", attempt_id=attempt_id, status=status)
        summary = _summary(att["kind"], result)
        if att["origin"] != "post-recording":
            with self.s.engine.begin() as conn:
                self.s.notify(conn, f"attempt_{status}", att["recording_id"],
                              f"{att['kind'].capitalize()} {attempt_id} of {att['recording_id']} {status}: {summary}"
                              f"{'; ' + reason if reason else ''}. `/radio-results {att['recording_id']} {attempt_id}`",
                              {"attempt_id": attempt_id})
        else:
            self._maybe_complete_job(att["recording_id"])

    def _maybe_complete_job(self, job_id: str) -> None:
        job = self.s.get(job_id)
        if job is None or job["status"] != "processing":
            return
        post = [a for a in self.attempts(job_id) if a["origin"] == "post-recording"]
        if any(a["status"] not in TERMINAL for a in post):
            return
        capture = (job.get("result") or {}).get("capture") or {}
        summary: dict[str, Any] = {"attempts": [{k: a[k] for k in ("attempt_id", "kind", "status", "status_reason", "output_dir")}
                                                for a in post], "errors": []}
        for a in post:
            res = a.get("result") or {}
            first = (res.get("inputs") or [{}])[0]
            if a["kind"] == "analyze":
                summary.update({k: first[k] for k in ("sha256", "spectrum", "target", "signals", "overload", "integrity")
                                if k in first})
            else:
                summary["satdump"] = {k: first.get(k) for k in ("decoded", "images", "snr_db_max", "return_code", "command")}
            summary["errors"] += res.get("errors", [])
        spec = job["spec"]
        raw = pathlib.Path((job.get("paths") or {}).get("raw", ""))
        if (spec.get("postprocess") or {}).get("retain_raw_iq") is False and (summary.get("satdump") or {}).get("decoded") \
                and raw.is_file():
            raw.unlink()
            summary["raw_deleted"] = "retain_raw_iq=false and the decode produced images"
            self.mark_missing()
        if job.get("paths", {}).get("sidecar"):
            write_sidecar(job, capture, summary)
            self.index(job_id, None, [{"kind": "metadata", "path": job["paths"]["sidecar"], "label": "recording metadata (JSON sidecar)"}])
        sd = summary.get("satdump")
        text = "raw IQ kept" if not sd else (f"SatDump decoded {sd.get('images')} image(s)" if sd.get("decoded")
                                             else "SatDump produced no images")
        reason = text + (f"; problems: {'; '.join(summary['errors'])}" if summary["errors"] else "")
        iq = capture.get("iq_health") or {}
        unhealthy = bool(iq.get("applicable")) and not iq.get("healthy")
        if iq.get("applicable"):
            reason += f"; IQ health: {iq['label']}" + (f" ({iq.get('summary')})" if unhealthy else "")
        self.s.transition(job_id, "completed", reason, expect=("processing",),
                          result={"capture": capture, "postprocess": summary},
                          notify=(f"{'Your job' if job['source'] == 'manual' else 'Automatic recording'} {job['job_name']} "
                                  f"({job_id}) on {job['receiver_id']} "
                                  f"{'finished recording, but the IQ is NOT healthy' if unhealthy else 'completed'}: "
                                  f"{capture.get('bytes', 0) / 1e9:.2f} GB, {reason}. `/radio-results {job_id}`")
                          if job["source"] == "manual" or summary["errors"] or unhealthy else None)

    def _set(self, attempt_id: str, **values: Any) -> None:
        with self.s.engine.begin() as conn:
            conn.execute(proc_attempts.update().where(proc_attempts.c.attempt_id == attempt_id).values(**values))

    def cancel(self, attempt_id: str) -> dict[str, Any]:
        with self._lock:
            att = self.attempt(attempt_id)
            if att["status"] != "queued":
                raise SchedulerError(f"{attempt_id} is {att['status']}; only queued attempts can be cancelled")
            self._set(attempt_id, status="cancelled", status_reason="cancelled", finished_at=self.s.clock())
        return {"attempt_id": attempt_id, "status": "cancelled"}

    def reclassify_decodes(self) -> int:
        """Earlier code called a decode attempt `completed` when SatDump merely ran. Re-label those that produced
        nothing as `failed` (results untouched) so history and Slack agree with reality."""
        with self.s.engine.begin() as conn:
            rows = conn.execute(select(proc_attempts.c.attempt_id, proc_attempts.c.result, proc_attempts.c.status_reason)
                                .where(and_(proc_attempts.c.kind == "decode", proc_attempts.c.status == "completed"))).all()
            n = 0
            for r in rows:
                if not any(i.get("decoded") for i in (r.result or {}).get("inputs", [])):
                    conn.execute(proc_attempts.update().where(proc_attempts.c.attempt_id == r.attempt_id).values(
                        status="failed", status_reason="reclassified: no decoded images (was reported completed); "
                                                       + (r.status_reason or "")))
                    n += 1
        if n:
            log_event(LOG, "reclassified decode attempts", count=n)
        return n

    def recover(self) -> None:
        with self._lock, self.s.engine.connect() as conn:
            rows = conn.execute(select(proc_attempts).where(proc_attempts.c.status.in_(("running", "paused")))).mappings().all()
        for r in rows:
            if r["pid"] and self._alive(r["pid"], r["attempt_id"]):
                self._signal(r["pid"], signal.SIGCONT)         # the gate pauses it again if needed
                self.current = {"attempt_id": r["attempt_id"], "pid": r["pid"], "handle": None}
                self._set(r["attempt_id"], status="running", status_reason="adopted after broker restart")
                continue
            self._set(r["attempt_id"], status="failed", status_reason="broker restarted while it ran",
                      finished_at=self.s.clock())
            if r["origin"] == "post-recording":
                self.submit(r["recording_id"], r["kind"], r["params"], "post-recording", "scheduler")

    def start(self, tick_s: float = 2.0) -> None:
        self.recover()
        self.reclassify_decodes()

        def loop() -> None:
            n = 0
            while not self._stop.wait(tick_s):
                try:
                    self.step()
                    n += 1
                    if n % 30 == 0:
                        self.sync_legacy()
                    if n % 150 == 0:
                        self.mark_missing()
                except Exception:  # noqa: BLE001
                    LOG.exception("processing step failed")
        threading.Thread(target=loop, name="processing", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def _summary(kind: str, result: dict[str, Any]) -> str:
    first = (result.get("inputs") or [{}])[0]
    if kind == "decode":
        ins = result.get("inputs", [])
        imgs = sum(int(i.get("images") or 0) for i in ins)
        frames = sum(int(i.get("cadu_frames") or 0) for i in ins)
        if not any(i.get("decoded") for i in ins):
            return f"NOT decoded ({frames} frames, {imgs} images with content)"
        best = first.get("best_target_frequency_hz")
        return f"decoded: {frames} frames, {imgs} image(s)" + (f", best centre {best / 1e6:.4f} MHz" if best else "")
    n = len(result.get("artifacts", []))
    tgt = first.get("target")
    return f"{n} artifact(s)" + (f", target {tgt['peak_over_floor_db']} dB over floor" if tgt else "")


def _iso(row: Any) -> dict[str, Any]:
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, dt.datetime):
            d[k] = v.isoformat()
    return d


def _spawn(argv: list[str], log_path: str) -> Any:
    log = open(log_path, "ab")
    return subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED="1"))


def _alive(pid: int, attempt_id: str) -> bool:
    try:
        cmd = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return False
    return "procworker" in cmd and attempt_id in cmd


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)                 # the worker and its satdump child
    except ProcessLookupError:
        pass
