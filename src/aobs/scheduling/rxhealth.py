"""Receiver health from finished recordings: IQ statistics + tuner start-up log, stored and alerted.

Runs after a capture ends (broker recordings) and when a legacy recorder's file is imported.
It reads ~8 MB per file, so it is safe to run while other receivers record. Results go to
  * the job result (capture.iq_health / capture.tuner, or iq_health for legacy rows),
  * <raw>.health.json next to the recording, indexed as a `diagnostics` artifact,
  * a `health` Slack notification when the IQ is suspiciously flat or the capture failed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
from typing import Any

from sqlalchemy import select

from ..db.schema import sched_jobs
from . import iqhealth, rtllog

LOG = logging.getLogger(__name__)
HISTORY_HOURS = 24


def _health_of(result: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    r = result or {}
    cap = r.get("capture") or {}
    return cap.get("iq_health") or r.get("iq_health"), cap.get("tuner") or r.get("tuner")


def history(s: Any, receiver_id: str, exclude: str | None = None, hours: float = HISTORY_HOURS,
            limit: int = 30) -> list[dict[str, Any]]:
    """Most recent first: [{recording_id, at, iq_health, tuner}] for one receiver."""
    since = s.clock() - dt.timedelta(hours=hours)
    with s.engine.connect() as conn:
        rows = conn.execute(select(sched_jobs.c.job_id, sched_jobs.c.record_end, sched_jobs.c.result)
                            .where(sched_jobs.c.receiver_id == receiver_id, sched_jobs.c.record_end >= since,
                                   sched_jobs.c.status.notin_(("scheduled", "shadow", "cancelled", "skipped")))
                            .order_by(sched_jobs.c.record_end.desc()).limit(limit * 3)).all()
    out = []
    for r in rows:
        if r.job_id == exclude:
            continue
        h, t = _health_of(r.result)
        if h and h.get("applicable"):
            out.append({"recording_id": r.job_id, "at": r.record_end, "iq_health": h, "tuner": t})
        if len(out) >= limit:
            break
    return out


def latest(s: Any, receiver_id: str) -> dict[str, Any] | None:
    """The newest assessed recording of a receiver, in the compact form status/health show."""
    h = history(s, receiver_id, hours=24 * 14, limit=1)
    if not h:
        return None
    x, t = h[0]["iq_health"], h[0]["tuner"] or {}
    at = h[0]["at"]
    return {"recording_id": h[0]["recording_id"], "at": at.isoformat() if hasattr(at, "isoformat") else at,
            "status": x["status"], "label": x["label"], "summary": x.get("summary"),
            "rms_dbfs": x.get("rms_dbfs"), "sample_spread_lsb": x.get("sample_spread_lsb"),
            "unique_codes": [x.get("i_unique_codes"), x.get("q_unique_codes")],
            "clipped_fraction": x.get("clipped_fraction"), "requested_gain_db": x.get("requested_gain_db"),
            "gain_response": x.get("gain_response"), "tuner_status": t.get("status"), "alert": x.get("alert")}


def assess(proc: Any, job: dict[str, Any], raw: str, expected: int | None, radio: dict[str, Any],
           log_text: str | None, notify: bool = True) -> dict[str, Any] | None:
    s = proc.s
    rx = s.receivers.get(job["receiver_id"])
    if rx is None or rx.driver != "rtlsdr" or not raw.endswith(".cu8"):
        return _basic(proc, job, raw, expected, rx)
    tuner = rtllog.parse(log_text, rx.serial) if log_text else None
    h = iqhealth.assess(raw, "cu8", radio.get("sample_rate_hz"), expected, radio.get("gain_db"), radio.get("gain_mode"))
    past = history(s, rx.id, exclude=job["job_id"])
    if h["status"] == "suspicious_flat":
        flat = [{"recording_id": p["recording_id"], **{k: p["iq_health"].get(k) for k in ("requested_gain_db", "rms_dbfs")}}
                for p in past if p["iq_health"].get("status") == "suspicious_flat"]
        gr = iqhealth.gain_response(h, flat)
        if gr:
            h["gain_response"] = gr
            if not gr["responds_to_gain"]:
                h["evidence"].append(f"confirmed: {gr['note']}")
    if tuner is not None:
        tuner["persistent_failure"] = rtllog.persistent_tuning_failure(
            [p["tuner"] for p in reversed(past)] + [tuner])
    record = {"recording_id": job["job_id"], "receiver_id": rx.id, "serial": rx.serial,
              "assessed_at": s.clock().isoformat(), "iq_health": h, "tuner": tuner}
    out = pathlib.Path(raw + ".health.json")
    try:
        tmp = out.with_name("." + out.name + ".tmp")
        tmp.write_text(json.dumps(record, indent=2, default=str) + "\n")
        os.replace(tmp, out)
        proc.index(job["job_id"], None, [{"kind": "diagnostics", "path": str(out), "label": "IQ health and tuner diagnostics"}])
    except OSError:
        LOG.exception("could not write %s", out)
    alert_tuner = tuner is not None and (tuner["status"] in ("wrong_device", "open_failed") or tuner["persistent_failure"])
    if notify and (h.get("alert") or alert_tuner):
        nxt = s.jobs(receiver=rx.id, status=["scheduled"], start=s.clock(), end=s.clock() + dt.timedelta(hours=12))
        nj = f"{nxt[0]['job_name']} at {nxt[0]['record_start']}" if nxt else None
        text = iqhealth.alert_text(rx.id, rx.serial, job["job_id"], h, tuner, nj) if h.get("alert") else \
            (f":warning: *WARNING: {rx.id} — TUNER PROBLEM* in {job['job_id']}: {tuner['status']}; "
             + "; ".join(tuner["evidence"]))
        if h.get("alert") and alert_tuner:
            text += f"\nTuner: {tuner['status']}; " + "; ".join(tuner["evidence"])
        with s.engine.begin() as conn:
            s.notify(conn, "health", job["job_id"], text, {"receiver_id": rx.id, "iq_status": h["status"],
                                                           "tuner_status": tuner and tuner["status"]})
    return {"iq_health": h, "tuner": tuner}


def _basic(proc: Any, job: dict[str, Any], raw: str, expected: int | None, rx: Any) -> dict[str, Any] | None:
    """Every other completed recording gets the simple check (iqhealth.basic): stored and shown, never alerted."""
    fmt = ((job.get("spec") or {}).get("recording") or {}).get("format") or pathlib.Path(raw).suffix.lstrip(".")
    if not pathlib.Path(raw).is_file():
        return None
    h = iqhealth.basic(raw, fmt, expected)
    if not h.get("applicable"):
        return None
    out = pathlib.Path(raw + ".health.json")
    try:
        tmp = out.with_name("." + out.name + ".tmp")
        tmp.write_text(json.dumps({"recording_id": job["job_id"], "receiver_id": job["receiver_id"],
                                   "serial": getattr(rx, "serial", None), "assessed_at": proc.s.clock().isoformat(),
                                   "iq_health": h, "tuner": None}, indent=2, default=str) + "\n")
        os.replace(tmp, out)
        proc.index(job["job_id"], None, [{"kind": "diagnostics", "path": str(out), "label": "IQ health (basic check)"}])
    except OSError:
        LOG.exception("could not write %s", out)
    return {"iq_health": h, "tuner": None}


def read_log(path: str | None, limit: int = 256_000) -> str | None:
    if not path:
        return None
    p = pathlib.Path(path)
    if not p.is_file():
        return None
    with p.open("rb") as fh:
        return fh.read(limit).decode("utf-8", errors="replace")


def assess_capture(proc: Any, job: dict[str, Any], capture: dict[str, Any]) -> dict[str, Any] | None:
    raw = capture.get("file") or (job.get("paths") or {}).get("raw")
    if not raw:
        return None
    radio = (job.get("spec") or {}).get("radio") or {}
    return assess(proc, job, raw, capture.get("expected_bytes"), radio, read_log((job.get("paths") or {}).get("log")))


def assess_legacy(proc: Any, job: dict[str, Any]) -> dict[str, Any] | None:
    """Legacy recorder files (OBS-R01 until cutover): one assessment per file, the worst one summarized."""
    files = [f for f in (job.get("result") or {}).get("legacy_files", []) if str(f.get("raw", "")).endswith(".cu8")]
    if not files:
        return None
    radio = dict((job.get("spec") or {}).get("radio") or {})
    results = []
    for f in files:
        meta: dict[str, Any] = {}
        try:
            meta = json.loads(pathlib.Path(f["metadata"]).read_text())
        except (OSError, ValueError, KeyError, TypeError):
            pass
        r = dict(radio)
        if meta.get("gain_db") is not None:
            r["gain_db"] = meta["gain_db"]
        if meta.get("sample_rate"):
            r["sample_rate_hz"] = meta["sample_rate"]
        log = str(f["raw"])[:-4] + ".rtl.log"
        res = assess(proc, job, f["raw"], meta.get("expected_bytes"), r, read_log(log), notify=False)
        if res:
            results.append(res)
    if not results:
        return None
    worst = min(results, key=lambda x: iqhealth.CLASSES.index(x["iq_health"]["status"]))
    worst["files"] = [{"file": x["iq_health"]["file"], "status": x["iq_health"]["status"]} for x in results]
    h = worst["iq_health"]
    rx = proc.s.receivers.get(job["receiver_id"])
    if h.get("alert") and rx is not None:
        with proc.s.engine.begin() as conn:
            proc.s.notify(conn, "health", job["job_id"],
                          iqhealth.alert_text(rx.id, rx.serial, job["job_id"], h, worst.get("tuner")),
                          {"receiver_id": rx.id, "iq_status": h["status"]})
    return worst
