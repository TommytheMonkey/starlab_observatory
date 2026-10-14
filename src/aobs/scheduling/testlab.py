"""Signal Path Test Lab: short controlled recordings that measure how a receiver's RF path performs.

A test is an ordinary broker job (source "manual"), so it reuses everything the broker already does:
serial-selected receivers, exclusive locking, conflict checks against every scheduled and legacy pass,
the ANTSW-01 lease for OBS-R05, the streaming capture process, IQ health, storage checks, the
signal-path snapshot at capture start, and the low-priority analysis queue (paused while anything
records). What a test adds:
  * conflict_policy.on_automatic_job = "reject": a test never displaces a satellite pass;
  * metadata.quiet: progress shows in the web UI, not in Slack (receiver-health alerts still go);
  * a short lead (test_min_lead_seconds, default 20 s) instead of the 2 minutes manual jobs need;
  * the `rf_test` analysis task (testmetrics.py) with fixed settings, so tests stay comparable;
  * a row in rf_tests with what the test was for (name, tags, baseline, comparison group).
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import json
import logging
import pathlib
import re
import secrets
from typing import Any, Callable

from sqlalchemy import and_, select

from ..db.schema import rf_test_presets, rf_tests, sched_jobs
from .hardware import R820T_GAINS_DB, SDRPLAY_BANDWIDTHS_HZ
from .store import HOLDING, SchedulerError

LOG = logging.getLogger("aobs.testlab")

# Fixed analysis settings for every test (documented in the report's methodology section).
ANALYSIS_PARAMS = {"fft_size": 2048, "rows": 600, "averages": 8, "fft_window": "hann", "overlap": 0.0,
                   "threshold_db": 8.0, "width": 1400, "height": 800}
DURATIONS = (10, 30, 60, 120)
MAX_DURATION_S = 600
SIGNAL_TYPES = {"general": ("General RF / unknown", 25_000), "fm_voice": ("FM voice", 12_500),
                "am_voice": ("AM voice", 8_000), "digital": ("Digital transmission", 25_000),
                "weather_satellite": ("Weather satellite (APT)", 40_000), "meteor_lrpt": ("METEOR LRPT", 150_000),
                "other": ("Other", 25_000)}
VARIABLE_TYPES = {"weather_satellite", "meteor_lrpt"}
FREE_MARGIN_S = 120          # a test must finish this long before the receiver's next reservation
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,31}$")


def _now_id(prefix: str, t: dt.datetime) -> str:
    return f"{prefix}{t:%Y%m%d%H%M%S}{secrets.token_hex(3)}"


def _iso(t: Any) -> Any:
    return t.isoformat() if hasattr(t, "isoformat") else t


def parse_hz(v: Any, what: str) -> int:
    """'137.9', '137.9 MHz', '137900000', '137900000 Hz', '145.8MHz' -> Hz. Bare numbers below 10 000 are MHz."""
    if v is None or v == "":
        raise SchedulerError(f"{what} is required", 400)
    if isinstance(v, (int, float)):
        x = float(v)
        return int(round(x * 1e6 if x < 10_000 else x))
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(ghz|mhz|khz|hz)?\s*", str(v), re.I)
    if not m:
        raise SchedulerError(f"{what}: could not read {v!r}; use e.g. 137.9 MHz or 137900000", 400)
    x, unit = float(m[1]), (m[2] or "").lower()
    mult = {"ghz": 1e9, "mhz": 1e6, "khz": 1e3, "hz": 1}.get(unit) or (1e6 if x < 10_000 else 1)
    return int(round(x * mult))


class TestLab:
    def __init__(self, svc: Any, broker_state: Callable[[], dict[str, Any]] | None = None):
        self.svc = svc
        self.s = svc.s
        self.broker_state = broker_state or (lambda: {})

    # ================================================================ receiver facts and defaults
    def _rx(self, rid: str) -> Any:
        rx = self.s.receivers.get(rid)
        if rx is None:
            raise SchedulerError(f"{rid} is not run by the broker, so it cannot be tested here "
                                 "(OBS-R03 belongs to satnogs-client)", 400)
        return rx

    def _last_request(self, rid: str) -> dict[str, Any] | None:
        with self.s.engine.connect() as conn:
            r = conn.execute(select(rf_tests.c.request).where(rf_tests.c.receiver_id == rid)
                             .order_by(rf_tests.c.created_at.desc()).limit(1)).scalar()
        return r

    def options(self, rid: str) -> dict[str, Any]:
        rx = self._rx(rid)
        caps = rx.capabilities(self.s.switch_ports)
        a = rx.auto or {}
        last = self._last_request(rid)
        if last:
            defaults = {k: last.get(k) for k in ("frequency_hz", "center_frequency_hz", "sample_rate_hz", "gain_db",
                                                  "gain_mode", "ifgr_db", "rfgr", "agc", "bandwidth_hz", "duration_s",
                                                  "antenna", "signal_type", "channel_bandwidth_hz")}
            defaults["source"] = f"your last test on {rid}"
        else:
            tgt = a.get("target_frequency_hz") or a.get("center_frequency_hz") or 137_900_000
            defaults = {"frequency_hz": tgt, "center_frequency_hz": a.get("center_frequency_hz"),
                        "sample_rate_hz": a.get("sample_rate_hz") or (2_400_000 if rx.driver == "rtlsdr" else 2_000_000),
                        "gain_db": a.get("gain_db") if rx.driver == "rtlsdr" else None, "gain_mode": "manual",
                        "ifgr_db": a.get("ifgr_db", 40), "rfgr": a.get("rfgr", 4), "agc": False,
                        "bandwidth_hz": a.get("bandwidth_hz") or (1_536_000 if rx.driver == "sdrplay" else None),
                        "duration_s": 60, "antenna": None, "signal_type": "general", "channel_bandwidth_hz": None,
                        "source": f"{rid}'s automatic-pass settings" if a else "built-in defaults"}
        if rx.driver == "rtlsdr" and defaults.get("gain_db") is None:
            defaults["gain_db"] = 29.7
        view = self.s.rf.view() if getattr(self.s, "rf", None) else None
        chain = []
        if view and rid in view["layout"]["receivers"]:
            comps = view["layout"]["components"]
            chain = [comps[c] for c in view["layout"]["receivers"][rid]["chain"] if c in comps]
        return {"receiver_id": rid, "hardware": rx.hardware, "driver": rx.driver, "serial": rx.serial, "mode": rx.mode,
                "capabilities": caps, "defaults": defaults, "signal_path": rx.signal_path, "chain": chain,
                "switched": rx.switched, "switch_ports": {n: p["antenna"] for n, p in sorted(self.s.switch_ports.items())},
                "bias_tee": {"fixed": rx.bias_tee_fixed, "note": "permanently on (EEPROM); every test records with it on"
                             if rx.bias_tee_fixed is True else "kept off; tests never turn it on"},
                "signal_types": {k: {"label": v[0], "channel_bandwidth_hz": v[1]} for k, v in SIGNAL_TYPES.items()},
                "durations": list(DURATIONS), "max_duration_s": MAX_DURATION_S,
                "availability": self.availability(rid, int(defaults.get("duration_s") or 60)),
                "presets": self.presets(rid)}

    def availability(self, rid: str, duration_s: int = 60) -> dict[str, Any]:
        """Can a test of this length start now? Never assumes 'on USB' means 'free'."""
        rx = self._rx(rid)
        now = self.s.clock()
        lead = int(self.s.settings.get("test_min_lead_seconds", 20))
        start = now + dt.timedelta(seconds=lead + 5)
        end = start + dt.timedelta(seconds=duration_s)
        reasons: list[str] = []
        if not self.s._present(rx):
            reasons.append(f"{rid} (serial {rx.serial}) is not on USB")
        hw = getattr(getattr(self.svc, "runner", None), "hw", None)
        if hw is not None and self.s._present(rx):
            try:
                busy = hw.busy(rx)
            except Exception as exc:  # noqa: BLE001
                busy = [f"could not check ({exc})"]
            if busy:
                reasons.append(f"{rid} is open in another program right now: {', '.join(map(str, busy))}")
        if rx.driver == "sdrplay":
            from .status import _sdrplay_writing
            try:
                if _sdrplay_writing(rx):
                    reasons.append(f"{rx.legacy_unit or 'a recorder'} is writing an RSP1B recording right now")
            except Exception:  # noqa: BLE001
                pass
        lease = (self.broker_state().get("leases") or {}).get(rid) if callable(self.broker_state) else None
        if lease:
            reasons.append(f"{rid} is leased to {lease.get('owner')} ({lease.get('purpose') or 'no purpose given'})")
        with self.s.engine.connect() as conn:
            nxt = conn.execute(select(sched_jobs).where(and_(
                sched_jobs.c.device_serial == rx.serial, sched_jobs.c.status.in_(HOLDING),
                sched_jobs.c.reservation_end > now)).order_by(sched_jobs.c.reservation_start).limit(1)).mappings().first()
        next_res = None
        if nxt is not None:
            next_res = {"job_id": nxt["job_id"], "name": nxt["target"] or nxt["job_name"], "source": nxt["source"],
                        "status": nxt["status"], "reservation_start": nxt["reservation_start"].isoformat(),
                        "reservation_end": nxt["reservation_end"].isoformat()}
            if nxt["reservation_start"] <= end + dt.timedelta(seconds=FREE_MARGIN_S):
                what = f"{next_res['name']} ({nxt['source']}, {nxt['status']})"
                if nxt["reservation_start"] <= now:
                    reasons.append(f"{rid} is reserved by {what} until {nxt['reservation_end']:%H:%M:%S}Z")
                else:
                    reasons.append(f"{what} reserves {rid} from {nxt['reservation_start']:%H:%M:%S}Z; a {duration_s} s "
                                   f"test would end too close to it. Try after {nxt['reservation_end']:%H:%M}Z")
        return {"available": not reasons, "reasons": reasons, "next_reservation": next_res,
                "would_start": start.isoformat(), "would_end": end.isoformat(), "checked_at": now.isoformat()}

    # ================================================================ starting a test
    def _job(self, rx: Any, req: dict[str, Any], test_id: str, start: dt.datetime) -> dict[str, Any]:
        radio: dict[str, Any] = {
            "center_frequency_hz": req["center_frequency_hz"], "target_frequency_hz": req["frequency_hz"],
            "sample_rate_hz": req["sample_rate_hz"], "ppm_correction": 0,
            "bias_tee": rx.bias_tee_fixed is True, "direct_sampling": False, "offset_tuning": False,
            "iq_correction": False, "dc_offset_correction": False, "doppler_tracking": False}
        if rx.driver == "rtlsdr":
            radio.update(bandwidth_hz=None, gain_mode=req["gain_mode"], agc=False,
                         gain_db=req["gain_db"] if req["gain_mode"] == "manual" else None)
        else:
            radio.update(bandwidth_hz=req["bandwidth_hz"], gain_db=None, agc=bool(req["agc"]),
                         gain_mode="auto" if req["agc"] else "manual",
                         gain_elements={"IFGR": int(req["ifgr_db"]), "RFGR": int(req["rfgr"])})
        root = self.s.settings.get("test_output_root") or str(
            pathlib.PurePosixPath(self.s.settings.get("default_output_root", "/data/observatory/jobs")).parent / "tests")
        receiver = {"receiver_id": rx.id, "driver": rx.driver, "serial": rx.serial, "exclusive_lock": True}
        if rx.switched:
            receiver["antenna"] = req["antenna"]
        return {
            "job_name": f"rftest-{rx.id.lower()}-{start:%Y%m%d%H%M%S}",
            "target": {"type": "frequency", "name": (req.get("name") or f"test {req['frequency_hz'] / 1e6:.4f} MHz")[:80]},
            "schedule": {"start": start.isoformat(), "duration_seconds": int(req["duration_s"]), "timezone": "UTC",
                         "pre_roll_seconds": 0, "post_roll_seconds": 0},
            "receiver": receiver,
            "radio": radio,
            "recording": {"enabled": True, "engine": rx.engines()[0], "format": "cu8" if rx.driver == "rtlsdr" else "cs16",
                          "output_directory": f"{root}/{{job_name}}", "minimum_free_space_gb": 1},
            "satdump": {"enabled": False, "operation": "none"},
            "postprocess": {"generate_waterfall": True, "generate_spectrum": True, "checksum": True,
                            "analyze_signal_quality": True, "rf_test": True, "retain_raw_iq": True, "decode": False,
                            "record_metadata": True, "channel_bandwidth_hz": int(req["channel_bandwidth_hz"])},
            "conflict_policy": {"on_receiver_conflict": "reject", "on_storage_warning": "reject",
                                "on_missing_device": "reject", "on_automatic_job": "reject"},
            "metadata": {"rf_test": test_id, "quiet": True, "experiment": "signal-path test",
                         "notes": req.get("description") or None},
        }

    def normalize(self, rid: str, body: dict[str, Any]) -> dict[str, Any]:
        rx = self._rx(rid)
        freq = parse_hz(body.get("frequency"), "frequency")
        rate = int(body.get("sample_rate_hz") or 0)
        if not rate:
            raise SchedulerError("sample_rate_hz is required", 400)
        if body.get("center_frequency"):
            center = parse_hz(body["center_frequency"], "center frequency")
        else:      # keep the receiver's DC spike out of the measured channel: tune 1/8 of the rate below
            center = freq - rate // 8
        if abs(freq - center) > rate * 0.45:
            raise SchedulerError(f"the target {freq / 1e6:.4f} MHz is outside the usable band around "
                                 f"{center / 1e6:.4f} MHz at {rate / 1e6:.3f} MS/s", 400)
        dur = int(body.get("duration_s") or 60)
        if not 5 <= dur <= MAX_DURATION_S:
            raise SchedulerError(f"duration must be 5-{MAX_DURATION_S} s", 400)
        st = body.get("signal_type") or "general"
        if st not in SIGNAL_TYPES:
            raise SchedulerError(f"signal_type must be one of {list(SIGNAL_TYPES)}", 400)
        ch_bw = int(body.get("channel_bandwidth_hz") or SIGNAL_TYPES[st][1])
        if not 500 <= ch_bw <= rate * 0.9:
            raise SchedulerError("channel bandwidth must be between 500 Hz and 90% of the sample rate", 400)
        tags = [str(t).strip() for t in (body.get("tags") or []) if str(t).strip()]
        bad = [t for t in tags if not _TAG.match(t)]
        if bad or len(tags) > 12:
            raise SchedulerError("tags: up to 12, each 1-32 letters, digits, spaces or . _ + -", 400)
        req: dict[str, Any] = {
            "frequency_hz": freq, "center_frequency_hz": center, "sample_rate_hz": rate, "duration_s": dur,
            "signal_type": st, "channel_bandwidth_hz": ch_bw, "tags": tags,
            "name": str(body.get("name") or "").strip()[:120] or None,
            "description": str(body.get("description") or "").strip()[:4000] or None,
            "baseline_test_id": body.get("baseline_test_id") or None, "antenna": None}
        if rx.driver == "rtlsdr":
            mode = body.get("gain_mode") or "manual"
            if mode not in ("manual", "auto"):
                raise SchedulerError("gain_mode must be manual or auto", 400)
            g = body.get("gain_db")
            if mode == "manual":
                if g is None or float(g) not in R820T_GAINS_DB:
                    raise SchedulerError(f"gain_db must be one of the tuner's steps: {list(R820T_GAINS_DB)}", 400)
                g = float(g)
            req.update(gain_mode=mode, gain_db=g if mode == "manual" else None, agc=False, bandwidth_hz=None)
        else:
            agc = bool(body.get("agc"))
            ifgr, rfgr = body.get("ifgr_db"), body.get("rfgr")
            if rfgr is None or (not agc and ifgr is None):
                raise SchedulerError("SDRplay tests need RFGR (LNA state) and, without AGC, IFGR", 400)
            bw = int(body.get("bandwidth_hz") or 0)
            if bw not in SDRPLAY_BANDWIDTHS_HZ:
                raise SchedulerError(f"bandwidth_hz must be one of {list(SDRPLAY_BANDWIDTHS_HZ)}", 400)
            req.update(agc=agc, ifgr_db=int(ifgr if ifgr is not None else 40), rfgr=int(rfgr), bandwidth_hz=bw,
                       gain_db=None, gain_mode="auto" if agc else "manual")
        if rx.switched:
            ant = body.get("antenna")
            if not ant:
                raise SchedulerError(f"{rid} is fed through ANTSW-01: choose the antenna (switch port) to test", 400)
            if str(ant) not in {p["antenna"] for p in self.s.switch_ports.values()}:
                raise SchedulerError(f"{ant!r} is not on ANTSW-01", 400)
            req["antenna"] = str(ant)
        if req["baseline_test_id"] and self._row(req["baseline_test_id"]) is None:
            raise SchedulerError(f"no test {req['baseline_test_id']}", 404)
        return req

    def start(self, rid: str, body: dict[str, Any], author: str, dry_run: bool = False) -> dict[str, Any]:
        rx = self._rx(rid)
        req = self.normalize(rid, body)
        av = self.availability(rid, req["duration_s"])
        if not av["available"]:
            raise SchedulerError("This test cannot run safely right now: " + "; ".join(av["reasons"])
                                 + ". Nothing was started; try again later.", 409)
        now = self.s.clock()
        start = now + dt.timedelta(seconds=int(self.s.settings.get("test_min_lead_seconds", 20)) + 5)
        test_id = _now_id("t", now)
        job = self._job(rx, req, test_id, start)
        res = self.s.submit([job], "all_or_nothing", f"rf-test:{author}", dry_run=dry_run)
        r0 = res["results"][0]
        if not r0.get("ok"):
            raise SchedulerError("The broker refused this test: " + "; ".join(r0.get("errors") or ["unknown reason"]), 409)
        if dry_run:
            return {"ok": True, "dry_run": True, "warnings": r0.get("warnings", []),
                    "estimated_bytes": (r0.get("job") or {}).get("estimated_bytes"), "request": req}
        port = None
        if rx.switched:
            port = next((n for n, p in self.s.switch_ports.items() if p["antenna"] == req["antenna"]), None)
        snap = self.s.rf.snapshot(rid, None, req["antenna"], port) if getattr(self.s, "rf", None) else None
        base = self._row(req["baseline_test_id"]) if req["baseline_test_id"] else None
        group = (base["group_id"] or base["test_id"]) if base else None
        with self.s.engine.begin() as conn:
            conn.execute(rf_tests.insert().values(
                test_id=test_id, job_id=r0["job_id"], receiver_id=rid, name=req["name"], description=req["description"],
                signal_type=req["signal_type"], tags=req["tags"], baseline_test_id=req["baseline_test_id"],
                group_id=group, request={**req, "switch_port": port}, rf_path=snap, notes=None, result=None,
                created_by=author, created_at=now, updated_at=now))
            if base and not base["group_id"]:
                conn.execute(rf_tests.update().where(rf_tests.c.test_id == base["test_id"]).values(group_id=group))
        LOG.info("rf test %s on %s queued as job %s by %s", test_id, rid, r0["job_id"], author)
        return {"ok": True, "test_id": test_id, "job_id": r0["job_id"], "starts_at": start.isoformat(),
                "warnings": r0.get("warnings", []), "estimated_bytes": (r0.get("job") or {}).get("estimated_bytes")}

    # ================================================================ reading tests
    def _row(self, test_id: str) -> dict[str, Any] | None:
        with self.s.engine.connect() as conn:
            r = conn.execute(select(rf_tests).where(rf_tests.c.test_id == test_id)).mappings().first()
        return dict(r) if r else None

    def _metrics_file(self, job_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        p = self.svc.processor
        arts = p.artifacts(job_id)
        f = next((a for a in reversed(arts) if pathlib.Path(a["path"]).name.startswith("rf_test")
                  and a["path"].endswith(".json")), None)
        if f is None:
            return None, None
        try:
            return json.loads(pathlib.Path(f["path"]).read_text()), f
        except (OSError, ValueError):
            return None, f

    def progress(self, job: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any]:
        now = self.s.clock()
        st = job["status"]
        rs, re_ = dt.datetime.fromisoformat(job["record_start"]), dt.datetime.fromisoformat(job["record_end"])
        total = int((re_ - rs).total_seconds())
        if st == "scheduled":
            wait = max(0, int((dt.datetime.fromisoformat(job["reservation_start"]) - now).total_seconds()))
            return {"phase": "waiting", "label": f"Waiting for the receiver (starts in {wait} s)", "done": False}
        if st == "preparing":
            return {"phase": "initializing", "label": "Initializing receiver and configuring tuner", "done": False}
        if st == "recording":
            el = max(0, min(total, int((now - rs).total_seconds())))
            return {"phase": "recording", "label": f"Recording: {el} / {total} seconds", "elapsed_s": el,
                    "total_s": total, "done": False}
        if st in ("recorded", "processing"):
            a = next((x for x in attempts if x["kind"] == "analyze"), None)
            if a is None or a["status"] == "queued":
                return {"phase": "processing", "label": "Processing IQ: queued for analysis", "done": False}
            if a["status"] == "paused":
                return {"phase": "processing", "label": "Analysis paused while another recording runs", "done": False}
            return {"phase": "processing", "label": "Processing IQ: waterfall, spectrum and metrics", "done": False}
        if st == "completed":
            a = next((x for x in attempts if x["kind"] == "analyze"), None)
            if a is not None and a["status"] == "failed":
                return {"phase": "failed", "label": f"Recorded, but the analysis failed: {a.get('status_reason')}",
                        "done": True}
            return {"phase": "complete", "label": "Complete", "done": True}
        if st == "partial":
            return {"phase": "cancelled", "label": "Stopped early; the IQ captured so far is kept", "done": True}
        return {"phase": st, "label": f"{st.capitalize()}: {job.get('status_reason') or ''}".strip(), "done": True}

    def detail(self, test_id: str) -> dict[str, Any]:
        row = self._row(test_id)
        if row is None:
            raise SchedulerError(f"no test {test_id}", 404)
        job = self.s.get(row["job_id"])
        if job is None:
            raise SchedulerError(f"test {test_id} lost its recording {row['job_id']}", 500)
        attempts = self.svc.processor.attempts(row["job_id"]) if self.svc.processor else []
        prog = self.progress(job, attempts)
        metrics, mart = (None, None)
        if job["status"] in ("completed", "processing", "partial"):
            metrics, mart = self._metrics_file(row["job_id"])
        if metrics and not row["result"]:
            with self.s.engine.begin() as conn:
                conn.execute(rf_tests.update().where(rf_tests.c.test_id == test_id)
                             .values(result=metrics["summary"], updated_at=self.s.clock()))
            row["result"] = metrics["summary"]
        arts = self.svc.processor.artifacts(row["job_id"]) if self.svc.processor else []
        files = {}
        for a in arts:
            name = pathlib.Path(a["path"]).name
            key = ("raw_iq" if a["kind"] == "raw_iq" else "waterfall" if a["kind"] == "waterfall" else
                   "spectrum" if a["kind"] == "spectrum" else "power" if name.startswith("power") else
                   "metrics" if name.startswith("rf_test") else "health" if name.endswith(".health.json") else
                   "sidecar" if a["kind"] == "metadata" else None)
            if key:
                files[key] = {"artifact_id": a["artifact_id"], "name": name, "bytes": a.get("bytes"), "path": a["path"]}
        spec = job["spec"]
        cap = (job.get("result") or {}).get("capture") or {}
        return {"test": self._public(row), "job": {
                    "job_id": job["job_id"], "status": job["status"], "status_reason": job.get("status_reason"),
                    "record_start": job["record_start"], "record_end": job["record_end"],
                    "radio": spec.get("radio"), "rf_path": (spec.get("resolved") or {}).get("rf_path"),
                    "switch_port": (spec.get("resolved") or {}).get("switch_port"),
                    "receiver": spec.get("receiver"), "bytes": cap.get("bytes"),
                    "iq_health": cap.get("iq_health"), "tuner": cap.get("tuner"),
                    "serial": job.get("device_serial")},
                "progress": prog, "metrics": metrics, "files": files}

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {k: _iso(v) for k, v in row.items()}

    def list(self, receiver: str | None = None, tag: str | None = None, part: str | None = None,
             fmin: float | None = None, fmax: float | None = None, since: str | None = None, until: str | None = None,
             status: str | None = None, sort: str = "date", limit: int = 200) -> list[dict[str, Any]]:
        q = select(rf_tests, sched_jobs.c.status, sched_jobs.c.spec, sched_jobs.c.record_start).join(
            sched_jobs, sched_jobs.c.job_id == rf_tests.c.job_id)
        if receiver:
            q = q.where(rf_tests.c.receiver_id == receiver)
        if since:
            q = q.where(rf_tests.c.created_at >= dt.datetime.fromisoformat(since).replace(tzinfo=dt.timezone.utc)
                        if "T" not in since else dt.datetime.fromisoformat(since))
        if until:
            q = q.where(rf_tests.c.created_at <= dt.datetime.fromisoformat(until).replace(tzinfo=dt.timezone.utc)
                        + (dt.timedelta(days=1) if "T" not in until else dt.timedelta()))
        with self.s.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(q.order_by(rf_tests.c.created_at.desc()).limit(1000)).mappings()]
        out = []
        for r in rows:
            req = r["request"] or {}
            path = ((r["spec"] or {}).get("resolved") or {}).get("rf_path") or r["rf_path"] or {}
            parts = [c.get("id") for c in path.get("chain", [])] + [c.get("name") for c in path.get("chain", [])]
            if tag and tag not in (r["tags"] or []):
                continue
            if part and part not in parts:
                continue
            f = req.get("frequency_hz") or 0
            if fmin is not None and f < fmin * 1e6 or fmax is not None and f > fmax * 1e6:
                continue
            res = r["result"] or {}
            state = "complete" if res else r["status"]
            if status and status != state:
                continue
            out.append({"test_id": r["test_id"], "job_id": r["job_id"], "receiver_id": r["receiver_id"],
                        "name": r["name"], "created_at": _iso(r["created_at"]), "record_start": _iso(r["record_start"]),
                        "status": state, "frequency_hz": f, "sample_rate_hz": req.get("sample_rate_hz"),
                        "gain": req.get("gain_db") if req.get("gain_db") is not None else
                        (f"IFGR {req.get('ifgr_db')} RFGR {req.get('rfgr')}" if req.get("rfgr") is not None else req.get("gain_mode")),
                        "duration_s": req.get("duration_s"), "tags": r["tags"] or [], "path": path.get("text"),
                        "parts": [c.get("id") for c in path.get("chain", [])], "baseline_test_id": r["baseline_test_id"],
                        "group_id": r["group_id"], "signal_type": r["signal_type"],
                        "noise_floor_dbfs_per_hz": res.get("noise_floor_dbfs_per_hz"), "snr_db": res.get("snr_db"),
                        "channel_power_dbfs": (res.get("channel") or {}).get("mean_power_dbfs"),
                        "clipped_fraction": res.get("clipped_fraction"), "strong_peaks": res.get("strong_peaks")})
        keys = {"snr": "snr_db", "floor": "noise_floor_dbfs_per_hz", "channel": "channel_power_dbfs",
                "clipping": "clipped_fraction", "peaks": "strong_peaks"}
        if sort in keys:
            k = keys[sort]
            out.sort(key=lambda x: (x[k] is None, -(x[k] or 0) if sort in ("snr", "channel") else (x[k] or 0)))
        return out[:limit]

    def update(self, test_id: str, body: dict[str, Any]) -> dict[str, Any]:
        row = self._row(test_id)
        if row is None:
            raise SchedulerError(f"no test {test_id}", 404)
        vals: dict[str, Any] = {}
        if "notes" in body:
            vals["notes"] = str(body["notes"] or "")[:8000] or None
        if "name" in body:
            vals["name"] = str(body["name"] or "").strip()[:120] or None
        if "tags" in body:
            tags = [str(t).strip() for t in body["tags"] or [] if str(t).strip()]
            if len(tags) > 12 or any(not _TAG.match(t) for t in tags):
                raise SchedulerError("tags: up to 12, each 1-32 letters, digits, spaces or . _ + -", 400)
            vals["tags"] = tags
        if not vals:
            return self._public(row)
        vals["updated_at"] = self.s.clock()
        with self.s.engine.begin() as conn:
            conn.execute(rf_tests.update().where(rf_tests.c.test_id == test_id).values(**vals))
        return self._public(self._row(test_id))

    def cancel(self, test_id: str, author: str) -> dict[str, Any]:
        row = self._row(test_id)
        if row is None:
            raise SchedulerError(f"no test {test_id}", 404)
        cat = self.svc.catalog
        reason = f"signal-path test cancelled by {author}"
        r = cat.cancel(row["job_id"], None, None, reason, author)
        if r.get("needs_confirmation"):        # the owner's own test: stopping it is what they asked for
            r = cat.cancel(row["job_id"], None, r["confirm"], reason, author)
        return r

    def file(self, test_id: str, artifact_id: str) -> tuple[pathlib.Path, str]:
        row = self._row(test_id)
        if row is None:
            raise SchedulerError(f"no test {test_id}", 404)
        if not any(a["artifact_id"] == artifact_id for a in self.svc.processor.artifacts(row["job_id"])):
            raise SchedulerError("that file does not belong to this test", 404)
        return self.svc.processor.artifact_file(artifact_id)

    def iq_link(self, test_id: str, base_url: str) -> dict[str, Any]:
        row = self._row(test_id)
        if row is None:
            raise SchedulerError(f"no test {test_id}", 404)
        raw = next((a for a in self.svc.processor.artifacts(row["job_id"], "raw_iq")), None)
        if raw is None:
            raise SchedulerError("this test has no raw IQ file (yet)", 404)
        return self.svc.catalog.link(raw["artifact_id"], 3600, base_url)

    # ================================================================ presets
    def presets(self, rid: str | None = None) -> list[dict[str, Any]]:
        with self.s.engine.connect() as conn:
            rows = conn.execute(select(rf_test_presets).order_by(rf_test_presets.c.name)).mappings().all()
        return [{**dict(r), "created_at": _iso(r["created_at"])} for r in rows
                if rid is None or r["receiver_id"] in (None, rid)]

    def save_preset(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()[:80]
        if not name:
            raise SchedulerError("a preset needs a name", 400)
        allowed = {"frequency", "center_frequency", "sample_rate_hz", "gain_db", "gain_mode", "ifgr_db", "rfgr", "agc",
                   "bandwidth_hz", "duration_s", "signal_type", "channel_bandwidth_hz", "tags", "antenna"}
        params = {k: v for k, v in (body.get("params") or {}).items() if k in allowed}
        rid = body.get("receiver_id") or None
        if rid is not None:
            self._rx(rid)
        pid = _now_id("ps", self.s.clock())
        with self.s.engine.begin() as conn:
            conn.execute(rf_test_presets.insert().values(preset_id=pid, name=name, receiver_id=rid, params=params,
                                                         created_at=self.s.clock()))
        return {"preset_id": pid, "name": name, "receiver_id": rid, "params": params}

    def delete_preset(self, preset_id: str) -> dict[str, Any]:
        with self.s.engine.begin() as conn:
            n = conn.execute(rf_test_presets.delete().where(rf_test_presets.c.preset_id == preset_id)).rowcount
        if not n:
            raise SchedulerError(f"no preset {preset_id}", 404)
        return {"deleted": preset_id}

    # ================================================================ A/B comparison
    def compare(self, a_id: str, b_id: str) -> dict[str, Any]:
        A, B = self.detail(a_id), self.detail(b_id)
        for t in (A, B):
            if not t["metrics"]:
                raise SchedulerError(f"test {t['test']['test_id']} has no analysis results yet "
                                     f"({t['progress']['label']})", 409)
        return compare_metrics(A, B)

    # ================================================================ printable report
    def report(self, test_id: str, compare_with: str | None = None) -> str:
        d = self.detail(test_id)
        cmp = self.compare(compare_with, test_id) if compare_with else None
        return render_report(d, cmp, self._png_b64)

    def _png_b64(self, d: dict[str, Any], key: str) -> str | None:
        f = d["files"].get(key)
        if not f:
            return None
        try:
            return base64.b64encode(pathlib.Path(f["path"]).read_bytes()).decode()
        except OSError:
            return None


# ==================================================================== comparison logic
METRIC_ROWS = [
    ("noise_floor_dbfs_per_hz", "Noise floor", "dBFS/Hz", "lower"),
    ("noise_floor_dbfs_per_bin", "Noise floor (per FFT bin)", "dBFS", "lower"),
    ("channel.mean_power_dbfs", "Target-channel power", "dBFS", None),
    ("channel.power_over_noise_db", "Channel power over noise floor", "dB", "higher"),
    ("snr_db", "Estimated SNR", "dB", "higher"),
    ("mean_power_dbfs", "Mean received power (sample RMS)", "dBFS", None),
    ("peak_power_dbfs", "Peak band power", "dBFS", None),
    ("clipped_fraction", "Digital clipping", "%", "lower"),
    ("peak_sample_fraction_of_full_scale", "Peak sample (fraction of full scale)", "", None),
    ("strong_peaks", "Strong spectral peaks (≥15 dB)", "", "lower"),
    ("signals_detected", "Detected spectral peaks", "", None),
    ("persistent_spurs", "Persistent narrow carriers", "", "lower"),
    ("transient_events", "Transient bursts", "", None),
    ("noise_floor_time_std_db", "Noise floor variation over time", "dB", "lower"),
    ("spectral_flatness", "Spectral flatness (1 = flat noise)", "", None),
    ("iq_amplitude_imbalance_db", "I/Q amplitude imbalance", "dB", None),
]


def _get(d: dict[str, Any], dotted: str) -> Any:
    for k in dotted.split("."):
        d = (d or {}).get(k) if isinstance(d, dict) else None
    return d


def _setting_diffs(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    ra, rb = a["job"]["radio"] or {}, b["job"]["radio"] or {}
    qa, qb = a["test"]["request"] or {}, b["test"]["request"] or {}
    out = []
    for key, label in (("center_frequency_hz", "Center frequency"), ("target_frequency_hz", "Target frequency"),
                       ("sample_rate_hz", "Sample rate"), ("bandwidth_hz", "Bandwidth"), ("gain_mode", "Gain mode"),
                       ("gain_db", "Tuner gain (dB)"), ("gain_elements", "SDRplay gain (IFGR/RFGR)"), ("agc", "AGC"),
                       ("bias_tee", "Bias tee")):
        if ra.get(key) != rb.get(key):
            out.append({"setting": label, "a": ra.get(key), "b": rb.get(key)})
    for key, label in (("duration_s", "Duration (s)"), ("channel_bandwidth_hz", "Measured channel bandwidth"),
                       ("antenna", "Switch antenna")):
        if qa.get(key) != qb.get(key):
            out.append({"setting": label, "a": qa.get(key), "b": qb.get(key)})
    if a["test"]["receiver_id"] != b["test"]["receiver_id"]:
        out.insert(0, {"setting": "Receiver", "a": a["test"]["receiver_id"], "b": b["test"]["receiver_id"]})
    ma, mb = a["metrics"]["summary"], b["metrics"]["summary"]
    if ma.get("rbw_hz") != mb.get("rbw_hz"):
        out.append({"setting": "Analysis resolution (RBW, Hz)", "a": ma.get("rbw_hz"), "b": mb.get("rbw_hz")})
    return out


def _path_diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    pa = a["job"].get("rf_path") or a["test"].get("rf_path") or {}
    pb = b["job"].get("rf_path") or b["test"].get("rf_path") or {}
    ca = [c.get("id") for c in pa.get("chain", [])]
    cb = [c.get("id") for c in pb.get("chain", [])]
    names = {c.get("id"): c.get("name") for c in pa.get("chain", []) + pb.get("chain", [])}
    kinds = {c.get("id"): c.get("kind") for c in pa.get("chain", []) + pb.get("chain", [])}
    added = [x for x in cb if x not in ca]
    removed = [x for x in ca if x not in cb]
    changes = [f"added {kinds.get(x)} {names.get(x)}" for x in added] + \
              [f"removed {kinds.get(x)} {names.get(x)}" for x in removed]
    if not added and not removed and ca != cb:
        changes.append("same parts in a different order")
    return {"a": pa.get("text"), "b": pb.get("text"), "a_chain": pa.get("chain", []), "b_chain": pb.get("chain", []),
            "changes": changes, "added": [{"id": x, "name": names.get(x), "kind": kinds.get(x)} for x in added],
            "removed": [{"id": x, "name": names.get(x), "kind": kinds.get(x)} for x in removed], "identical": ca == cb}


def _overlay(ma: dict[str, Any], mb: dict[str, Any]) -> dict[str, Any] | None:
    import numpy as np
    fa, fb = np.array(ma["spectrum"]["freqs_hz"], float), np.array(mb["spectrum"]["freqs_hz"], float)
    lo, hi = max(fa[0], fb[0]), min(fa[-1], fb[-1])
    if hi <= lo:
        return None
    step = max(ma["spectrum"]["bin_hz"], mb["spectrum"]["bin_hz"])
    grid = np.arange(lo, hi, step)
    if len(grid) < 8:
        return None

    def on(m: dict[str, Any], f: np.ndarray, k: str) -> np.ndarray:
        return np.interp(grid, f, np.array([v if v is not None else np.nan for v in m["spectrum"][k]], float))
    a_mean, b_mean = on(ma, fa, "mean_dbfs"), on(mb, fb, "mean_dbfs")
    # per-Hz so different RBWs line up
    a_mean = a_mean - 10 * np.log10(ma["summary"]["rbw_hz"])
    b_mean = b_mean - 10 * np.log10(mb["summary"]["rbw_hz"])
    diff = b_mean - a_mean
    return {"freqs_hz": [int(x) for x in grid], "a_dbfs_per_hz": [round(float(x), 2) for x in a_mean],
            "b_dbfs_per_hz": [round(float(x), 2) for x in b_mean], "diff_db": [round(float(x), 2) for x in diff],
            "median_diff_db": round(float(np.nanmedian(diff)), 2),
            "units": "dBFS/Hz (each spectrum divided by its own RBW); difference = B − A"}


def compare_metrics(A: dict[str, Any], B: dict[str, Any]) -> dict[str, Any]:
    ma, mb = A["metrics"], B["metrics"]
    sa, sb = ma["summary"], mb["summary"]
    rows = []
    for key, label, unit, better in METRIC_ROWS:
        va, vb = _get(sa, key), _get(sb, key)
        if key == "clipped_fraction":
            va, vb = (None if va is None else va * 100), (None if vb is None else vb * 100)
        delta = None if va is None or vb is None else round(vb - va, 4 if key == "clipped_fraction" else 2)
        rows.append({"metric": label, "key": key, "unit": unit, "a": va, "b": vb, "change": delta, "better": better})
    settings = _setting_diffs(A, B)
    path = _path_diff(A, B)
    ta = dt.datetime.fromisoformat(A["job"]["record_start"])
    tb = dt.datetime.fromisoformat(B["job"]["record_start"])
    gap = abs((tb - ta).total_seconds())
    warnings, facts, interp, hypo = [], [], [], []
    gain_diff = any(d["setting"] in ("Tuner gain (dB)", "SDRplay gain (IFGR/RFGR)", "Gain mode", "AGC") for d in settings)
    if A["test"]["receiver_id"] != B["test"]["receiver_id"]:
        warnings.append("Different receivers: their ADC scaling and noise figures differ, so absolute dBFS values are "
                        "not comparable. Compare SNR and channel-over-noise instead.")
    if gain_diff:
        warnings.append("Gain settings differ: absolute dBFS levels (power, noise floor) are not directly comparable. "
                        "Figures relative to the noise floor (SNR, channel over noise) are the fairer comparison.")
    if any(d["setting"] in ("Sample rate", "Analysis resolution (RBW, Hz)") for d in settings):
        warnings.append("Sample rate / resolution differ: per-bin values are not comparable; use the per-Hz noise floor.")
    if any(d["setting"] in ("Center frequency", "Target frequency") for d in settings):
        warnings.append("Different frequencies were measured; the comparison only covers where the two bands overlap.")
    if gap > 1800:
        warnings.append(f"The tests are {gap / 3600:.1f} h apart. Interference, propagation and band activity change "
                        "over time, so part of any difference may have nothing to do with the hardware change.")
    st = {A["test"].get("signal_type"), B["test"].get("signal_type")}
    if st & VARIABLE_TYPES:
        warnings.append("Satellite signals change with elevation, Doppler and pass geometry; two recordings at "
                        "different times are only loosely comparable.")
    if max(sa.get("band_power_time_std_db") or 0, sb.get("band_power_time_std_db") or 0) > 2:
        warnings.append("The band power varied by more than 2 dB within a recording: the signal environment was "
                        "changing, so small differences between the tests are not meaningful.")
    if not settings and path["identical"]:
        warnings.append("Nothing differs between the two tests' recorded settings or signal paths; any difference "
                        "is measurement-to-measurement variation or a change that was not recorded in the diagram.")

    def ch(key: str) -> float | None:
        r = next((x for x in rows if x["key"] == key), None)
        return r["change"] if r else None
    d_floor = ch("noise_floor_dbfs_per_hz")
    d_con = ch("channel.power_over_noise_db")
    d_snr = ch("snr_db")
    d_peaks = ch("strong_peaks")
    d_clip = ch("clipped_fraction")
    if d_floor is not None and abs(d_floor) >= 1:
        facts.append(f"The noise floor {'fell' if d_floor < 0 else 'rose'} by {abs(d_floor):.1f} dB "
                     f"({sa['noise_floor_dbfs_per_hz']} → {sb['noise_floor_dbfs_per_hz']} dBFS/Hz).")
    elif d_floor is not None:
        facts.append(f"The noise floor stayed within 1 dB ({d_floor:+.1f} dB).")
    if d_peaks:
        facts.append(f"Strong spectral peaks went from {sa['strong_peaks']} to {sb['strong_peaks']}.")
    if d_clip:
        facts.append(f"Digital clipping went from {(sa['clipped_fraction'] or 0) * 100:.4f}% to "
                     f"{(sb['clipped_fraction'] or 0) * 100:.4f}%.")
    if sa.get("snr_db") is not None and sb.get("snr_db") is not None:
        facts.append(f"Estimated SNR in the target channel: {sa['snr_db']:.1f} dB → {sb['snr_db']:.1f} dB.")
    elif d_con is not None:
        facts.append(f"Target-channel power over the noise floor: {_get(sa, 'channel.power_over_noise_db')} → "
                     f"{_get(sb, 'channel.power_over_noise_db')} dB (no usable SNR in at least one test).")
    added_kinds = {x["kind"] for x in path["added"]}
    removed_kinds = {x["kind"] for x in path["removed"]}
    if d_peaks is not None and d_peaks < 0 and "filter" in added_kinds:
        interp.append("Fewer strong signals after adding a filter is consistent with the filter rejecting them.")
    if d_floor is not None and d_floor <= -2 and "amplifier" in removed_kinds:
        interp.append("The lower floor after removing an amplifier is expected: less gain lowers everything, signal "
                      "included. It says nothing about sensitivity unless SNR also improved.")
    if d_floor is not None and d_floor <= -2 and (d_snr is None or d_snr < 1) and (d_con is None or d_con < 1):
        interp.append("The noise floor dropped but the signal did not stand out more above it, which points to less "
                      "overall gain or level rather than better reception.")
    if d_clip is not None and d_clip < 0 and (sb["clipped_fraction"] or 0) == 0:
        interp.append("Clipping is gone, which removes a source of distortion and spurious signals.")
    better = d_snr if d_snr is not None else d_con
    if better is not None and better >= 3 and not gain_diff and gap <= 1800:
        verdict = (f"The target channel stood {better:.1f} dB further above the noise in B. Under the tested "
                   "conditions this suggests B's configuration helps at this frequency.")
        hypo.append("One pair of recordings cannot rule out a change in the signal itself; repeat both "
                    "configurations (A, B, A again) to confirm.")
    elif better is not None and better <= -3 and not gain_diff and gap <= 1800:
        verdict = (f"The target channel stood {-better:.1f} dB less above the noise in B. Under the tested "
                   "conditions this suggests B's configuration is worse at this frequency.")
        hypo.append("Repeat both configurations to confirm before changing the hardware back.")
    else:
        verdict = ("Inconclusive: the measurements do not show a clear, like-for-like difference in how far the target "
                   "stands above the noise." if better is not None else
                   "Inconclusive for reception: there was no measurable signal in the target channel, so only the "
                   "noise and interference picture can be compared.")
    hypo.append("No claim is made about RF sensitivity: dBFS values are uncalibrated, and a lower noise floor alone "
                "does not mean a weaker signal can be received.")
    return {"a": _brief(A), "b": _brief(B), "settings_differ": settings, "path": path,
            "time_between_s": int(gap), "metrics": rows, "overlay": _overlay(ma, mb),
            "waterfalls": {"a": ma["waterfall"], "b": mb["waterfall"],
                           "shared_scale_dbfs": [min(ma["waterfall"]["min_dbfs"], mb["waterfall"]["min_dbfs"]),
                                                 max(ma["waterfall"]["max_dbfs"], mb["waterfall"]["max_dbfs"])]},
            "report": {"verdict": verdict, "measured": facts, "interpretations": interp, "hypotheses": hypo,
                       "warnings": warnings}}


def _brief(d: dict[str, Any]) -> dict[str, Any]:
    t, j = d["test"], d["job"]
    return {"test_id": t["test_id"], "name": t["name"], "receiver_id": t["receiver_id"], "record_start": j["record_start"],
            "radio": j["radio"], "path": (j.get("rf_path") or t.get("rf_path") or {}).get("text"),
            "switch_port": j.get("switch_port"), "tags": t.get("tags")}


# ==================================================================== printable report
def render_report(d: dict[str, Any], cmp: dict[str, Any] | None, png: Callable[[dict[str, Any], str], str | None]) -> str:
    e = html.escape
    t, j, m = d["test"], d["job"], d["metrics"] or {}
    s = m.get("summary") or {}
    radio = j.get("radio") or {}
    path = (j.get("rf_path") or t.get("rf_path") or {})
    chain = " ↓ ".join(e(c.get("name", "")) for c in path.get("chain", [])) + f" ↓ {e(t['receiver_id'])}"
    gain = (f"{radio.get('gain_db')} dB" if radio.get("gain_db") is not None else
            f"IFGR {radio['gain_elements']['IFGR']} / RFGR {radio['gain_elements']['RFGR']}" if radio.get("gain_elements")
            else str(radio.get("gain_mode")))

    def row(k: str, v: Any) -> str:
        return f"<tr><th>{e(k)}</th><td>{e('' if v is None else str(v))}</td></tr>"
    ch = s.get("channel") or {}
    parts = [f"""<!doctype html><html><head><meta charset="utf-8"><title>Signal path test {e(t['test_id'])}</title>
<style>body{{font:13px/1.45 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#111}}
h1{{font-size:20px}}h2{{font-size:15px;margin-top:22px;border-bottom:1px solid #ccc}}table{{border-collapse:collapse;width:100%}}
th,td{{text-align:left;padding:3px 6px;border-bottom:1px solid #eee;vertical-align:top}}th{{width:38%;font-weight:600}}
img{{max-width:100%;border:1px solid #ddd;margin:6px 0}}.m{{color:#555}}@media print{{h2{{break-after:avoid}}img{{break-inside:avoid}}}}</style>
</head><body><h1>Signal path test — {e(t.get('name') or t['test_id'])}</h1>
<p class="m">{e(t['test_id'])} · recording {e(j['job_id'])} · {e(j['record_start'])} · {e(d['progress']['label'])}</p>
<h2>Configuration</h2><table>{row('Receiver', f"{t['receiver_id']} (serial {j.get('serial')})")}
{row('Signal path', path.get('text'))}{row('Layout version', path.get('layout_version'))}
{row('Switch port', j.get('switch_port'))}{row('Center frequency', f"{radio.get('center_frequency_hz', 0) / 1e6:.6f} MHz")}
{row('Target frequency', f"{(radio.get('target_frequency_hz') or 0) / 1e6:.6f} MHz")}
{row('Sample rate', f"{radio.get('sample_rate_hz', 0) / 1e6:.3f} MS/s")}{row('Bandwidth', radio.get('bandwidth_hz'))}
{row('Gain', gain)}{row('AGC', radio.get('agc'))}{row('Bias tee', radio.get('bias_tee'))}
{row('Duration', f"{(t.get('request') or {}).get('duration_s')} s")}{row('Signal type', t.get('signal_type'))}
{row('Tags', ', '.join(t.get('tags') or []))}{row('Description', t.get('description'))}{row('Notes', t.get('notes'))}</table>
<p>{chain}</p>
<h2>Summary</h2><table>{row('Mean received power (sample RMS)', f"{s.get('mean_power_dbfs')} dBFS")}
{row('Noise floor', f"{s.get('noise_floor_dbfs_per_hz')} dBFS/Hz ({s.get('noise_floor_dbfs_per_bin')} dBFS per {s.get('rbw_hz')} Hz bin)")}
{row('Target-channel power', f"{ch.get('mean_power_dbfs')} dBFS over {ch.get('bandwidth_hz')} Hz" if ch.get('measured') else ch.get('note'))}
{row('Estimated SNR', f"{s.get('snr_db')} dB" if s.get('snr_db') is not None else (ch.get('snr_unavailable') or 'SNR unavailable'))}
{row('Digital clipping', f"{(s.get('clipped_fraction') or 0) * 100:.4f}%")}
{row('ADC codes used', f"{s.get('adc_codes_used')} of {s.get('adc_codes_possible')}")}
{row('Detected spectral peaks', f"{s.get('signals_detected')} ({s.get('strong_peaks')} strong, {s.get('persistent_spurs')} persistent narrow)")}
{row('I/Q imbalance / DC offset', f"{s.get('iq_amplitude_imbalance_db')} dB / I {s.get('dc_offset_i')}, Q {s.get('dc_offset_q')}")}</table>
<ul>{''.join(f"<li><b>{e(q['basis'])}:</b> {e(q['text'])}</li>" for q in m.get('quality', []))}</ul>"""]
    for key, title in (("spectrum", "Average spectrum"), ("waterfall", "Waterfall"), ("power", "Power over time")):
        b = png(d, key)
        if b:
            parts.append(f"<h2>{title}</h2><img alt='{title}' src='data:image/png;base64,{b}'>")
    sig = m.get("signals") or []
    if sig:
        parts.append("<h2>Detected spectral peaks</h2><p class='m'>Candidates only; a peak is not identified as "
                     "interference or as any particular transmitter.</p><table><tr><th>Frequency</th><td><b>Over floor · "
                     "bandwidth · active · kind</b></td></tr>" + "".join(
                         f"<tr><th>{x['frequency_hz'] / 1e6:.5f} MHz</th><td>{x['peak_over_floor_db']} dB · "
                         f"{x['bandwidth_hz']} Hz · {x['duty_cycle'] * 100:.0f}% ({x['start_s']}–{x['end_s']} s) · "
                         f"{e(x.get('kind', ''))}</td></tr>" for x in sig[:25]) + "</table>")
    if cmp:
        r = cmp["report"]
        parts.append("<h2>Comparison with baseline</h2>"
                     f"<p>A: {e(str(cmp['a']['path']))} ({e(cmp['a']['record_start'])})<br>"
                     f"B: {e(str(cmp['b']['path']))} ({e(cmp['b']['record_start'])})</p>"
                     f"<p><b>{e(r['verdict'])}</b></p>"
                     + "".join(f"<p class='m'>⚠ {e(w)}</p>" for w in r["warnings"])
                     + "<table>" + "".join(f"<tr><th>{e(x['metric'])}</th><td>{x['a']} → {x['b']} {e(x['unit'])}"
                                           f" ({'' if x['change'] is None else f'{x['change']:+g}'})</td></tr>"
                                           for x in cmp["metrics"]) + "</table>"
                     + "<p><b>Measured</b></p><ul>" + "".join(f"<li>{e(x)}</li>" for x in r["measured"]) + "</ul>"
                     + "<p><b>Interpretation</b></p><ul>" + "".join(f"<li>{e(x)}</li>" for x in r["interpretations"] or ["none"]) + "</ul>"
                     + "<p><b>Unconfirmed</b></p><ul>" + "".join(f"<li>{e(x)}</li>" for x in r["hypotheses"]) + "</ul>")
    parts.append(f"<h2>Methodology</h2><pre style='white-space:pre-wrap;font-size:12px'>{e(m.get('methodology') or '')}"
                 f"\nAnalysis settings: {e(json.dumps(ANALYSIS_PARAMS))}</pre></body></html>")
    return "\n".join(parts)
