"""Receiver health from finished recordings: IQ statistics + tuner start-up log, stored and alerted.

Runs after a capture ends (broker recordings) and when a legacy recorder's file is imported.
It reads ~8 MB per file, so it is safe to run while other receivers record. Results go to
  * the job result (capture.iq_health / capture.tuner, or iq_health for legacy rows),
  * <raw>.health.json next to the recording, indexed as a `diagnostics` artifact,
  * a `health` Slack notification when the IQ is suspiciously flat or the capture failed.

A replayed buffer (repeating_buffer) is a state of the receiver, not of one recording: it is alerted once when it
appears (from a recording or from a pass's pre-pass check), reminded at most every REMIND_HOURS while it lasts, and
"recovered" is sent when a pre-pass check or recording is clean again. The receiver's health notifications are that
state (data.fault / data.fault_event), so it survives broker restarts without a table of its own.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
from typing import Any, Callable

from sqlalchemy import Text, cast, or_, select

from ..db.schema import sched_jobs, sched_notifications
from . import iqhealth, rtllog

LOG = logging.getLogger(__name__)
HISTORY_HOURS = 24
PRETEST_SKIPPED = '%"skip_code": "repeating_buffer"%'   # a pass skipped by its pre-pass check (JSONText, sorted keys)
REMIND_HOURS = 4.0              # a receiver still in a latched fault: at most one reminder this often
LATCHED = ("repeating_buffer",)  # faults alerted once per receiver until they clear


def _health_of(result: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    r = result or {}
    cap = r.get("capture") or {}
    h = cap.get("iq_health") or r.get("iq_health") or iqhealth.from_pretest(cap.get("pretest"))
    return h, cap.get("tuner") or r.get("tuner")


def history(s: Any, receiver_id: str, exclude: str | None = None, hours: float = HISTORY_HOURS,
            limit: int = 30) -> list[dict[str, Any]]:
    """Most recent first: [{recording_id, at, iq_health, tuner}] for one receiver."""
    since = s.clock() - dt.timedelta(hours=hours)
    with s.engine.connect() as conn:
        rows = conn.execute(select(sched_jobs.c.job_id, sched_jobs.c.record_end, sched_jobs.c.result)
                            .where(sched_jobs.c.receiver_id == receiver_id, sched_jobs.c.record_end >= since,
                                   sched_jobs.c.status.notin_(("scheduled", "shadow", "cancelled")),
                                   or_(sched_jobs.c.status != "skipped",          # planner skips never ran
                                       cast(sched_jobs.c.result, Text).like(PRETEST_SKIPPED)))
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
    at = x.get("at") or h[0]["at"]                 # a pre-pass check: when it ran, not when the pass would have ended
    rp = x.get("repeat") or {}
    return {"recording_id": h[0]["recording_id"], "at": at.isoformat() if hasattr(at, "isoformat") else at,
            "source": x.get("source", "recording"), "period_bytes": rp.get("period_bytes"),
            "period_ms": rp.get("period_ms"), "first_offset_s": rp.get("first_offset_s"),
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
    if notify and h.get("repeat") is not None:      # the repeat check ran: latch or clear the receiver's fault
        latch(s, rx, "repeating_buffer", job, h["status"] == "repeating_buffer", "recording",
              lambda nj: iqhealth.alert_text(rx.id, rx.serial, job["job_id"], h, tuner, nj))
    iq_alert = h.get("alert") and h["status"] not in LATCHED
    if notify and (iq_alert or alert_tuner):
        nj = _next_job(s, rx.id)
        text = iqhealth.alert_text(rx.id, rx.serial, job["job_id"], h, tuner, nj) if iq_alert else \
            (f":warning: *WARNING: {rx.id} — TUNER PROBLEM* in {job['job_id']}: {tuner['status']}; "
             + "; ".join(tuner["evidence"]))
        if iq_alert and alert_tuner:
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


def _next_job(s: Any, receiver_id: str) -> str | None:
    nxt = s.jobs(receiver=receiver_id, status=["scheduled"], start=s.clock(), end=s.clock() + dt.timedelta(hours=12))
    return f"{nxt[0]['job_name']} at {nxt[0]['record_start']}" if nxt else None


def fault_state(s: Any, receiver_id: str, fault: str, days: float = 30) -> dict[str, Any] | None:
    """The receiver's latest notification about `fault` ({at, data: {fault_event: raised|reminder|recovered, since,
    ...}}), or None. Active unless the last event is "recovered"."""
    since = s.clock() - dt.timedelta(days=days)
    with s.engine.connect() as conn:
        rows = conn.execute(select(sched_notifications.c.at, sched_notifications.c.data)
                            .where(sched_notifications.c.kind == "health", sched_notifications.c.at >= since)
                            .order_by(sched_notifications.c.id.desc())).all()
    for r in rows:
        d = r.data or {}
        if d.get("receiver_id") == receiver_id and d.get("fault") == fault and d.get("fault_event"):
            return {"at": r.at, "data": d}
    return None


def latch(s: Any, rx: Any, fault: str, job: dict[str, Any], bad: bool, source: str,
          raised_text: Callable[[str | None], str]) -> str | None:
    """One alert per receiver per fault: "raised" when it appears, "reminder" at most every REMIND_HOURS while it
    lasts, "recovered" the first time it is clean again. Returns the event sent, or None."""
    last = fault_state(s, rx.id, fault)
    active = last is not None and last["data"]["fault_event"] != "recovered"
    now = s.clock()
    if bad and not active:
        event, since, text = "raised", now, raised_text(_next_job(s, rx.id))
    elif bad and now - last["at"] >= dt.timedelta(hours=REMIND_HOURS):
        since = dt.datetime.fromisoformat(last["data"]["since"])
        n = _affected(s, rx.id, fault, since)
        event, text = "reminder", iqhealth.fault_reminder_text(rx.id, rx.serial, fault, since, now, n + 1, job,
                                                               _next_job(s, rx.id))     # + this one (not saved yet)
    elif not bad and active:
        since = dt.datetime.fromisoformat(last["data"]["since"])
        n = _affected(s, rx.id, fault, since)
        event, text = "recovered", iqhealth.fault_recovered_text(rx.id, fault, since, n, job, source)
    else:
        return None
    with s.engine.begin() as conn:
        s.notify(conn, "health", job["job_id"], text,
                 {"receiver_id": rx.id, "iq_status": fault if bad else "healthy", "fault": fault, "fault_event": event,
                  "since": since.isoformat(), "source": source})
    return event


def _affected(s: Any, receiver_id: str, fault: str, since: dt.datetime) -> int:
    hours = (s.clock() - since).total_seconds() / 3600 + 1
    return sum(1 for p in history(s, receiver_id, hours=hours, limit=500)
               if p["iq_health"].get("status") == fault)


def pretest_outcome(s: Any, job: dict[str, Any], capture: dict[str, Any]) -> str | None:
    """A pass's pre-pass check (capture.pretest): latches the receiver's repeating_buffer fault, or clears it."""
    rx = s.receivers.get(job["receiver_id"])
    pre = capture.get("pretest") or {}
    if rx is None or not pre.get("conclusive") or (capture.get("device") or {}).get("confirmed") is False:
        return None                       # a stream from the wrong device says nothing about this receiver
    return latch(s, rx, "repeating_buffer", job, bool(pre.get("repeat")), "pre-pass check",
                 lambda nj: iqhealth.pretest_alert_text(rx.id, rx.serial, job["job_id"], job["job_name"], pre, nj))


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
    looped = next((x for x in results if x["iq_health"]["status"] == "repeating_buffer"), None)
    if rx is not None and any(x["iq_health"].get("repeat") is not None for x in results):
        latch(proc.s, rx, "repeating_buffer", job, looped is not None, "recording",
              lambda nj: iqhealth.alert_text(rx.id, rx.serial, job["job_id"], looped["iq_health"], looped.get("tuner"), nj))
    if h.get("alert") and h["status"] not in LATCHED and rx is not None:
        with proc.s.engine.begin() as conn:
            proc.s.notify(conn, "health", job["job_id"],
                          iqhealth.alert_text(rx.id, rx.serial, job["job_id"], h, worst.get("tuner")),
                          {"receiver_id": rx.id, "iq_status": h["status"]})
    return worst
