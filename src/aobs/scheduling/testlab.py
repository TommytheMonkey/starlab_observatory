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
import math
import pathlib
import re
import secrets
from typing import Any, Callable

import numpy as np
from sqlalchemy import and_, select

from ..branding import LOGO_ALT, logo_data_uri
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
            raise SchedulerError(f"{rid} is not a receiver in Starlab's inventory, so it cannot be tested here. "
                                 "Add it from Receivers → Discovered hardware (documentation-only devices such as "
                                 "OBS-R03 or a YARD Stick One cannot be tested)", 400)
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
                                                  "gain_elements",
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
        if rx.gain_model == "elements":
            defaults.update(elements_defaults(rx, last))
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
                "presets": self.presets(rid), "gain_model": rx.gain_model, "test_kinds": list(rx.test_kinds()),
                "readiness": rx.readiness}

    def availability(self, rid: str, duration_s: int = 60) -> dict[str, Any]:
        """Can a test of this length start now? Never assumes 'on USB' means 'free'."""
        rx = self._rx(rid)
        now = self.s.clock()
        lead = int(self.s.settings.get("test_min_lead_seconds", 20))
        start = now + dt.timedelta(seconds=lead + 5)
        end = start + dt.timedelta(seconds=duration_s)
        reasons: list[str] = []
        presence = self.s.presence(rx)
        if presence.connected is False:
            reasons.append(f"{rid} (serial {rx.serial}) is not on USB: {presence.detail}")
        elif presence.connected is None:
            reasons.append(f"cannot confirm {rid} (serial {rx.serial}) is on USB right now: {presence.detail}")
        hw = getattr(getattr(self.svc, "runner", None), "hw", None)
        if hw is not None and presence.connected is not False:
            try:
                busy = hw.busy(rx)
            except Exception as exc:  # noqa: BLE001
                busy = [f"could not check ({exc})"]
            if busy:
                reasons.append(f"{rid} is open in another program right now: {', '.join(map(str, busy))}")
        if rx.driver != "sdrplay" and presence.connected is not False:
            u = self.s.usb_usage(rx)
            if u and u["claimed"] and not u["holders"]:
                reasons.append(f"{rid}'s USB interface is claimed by another program right now ({u['detail']})")
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
        return {"available": not reasons, "reasons": reasons, "presence": presence.as_dict(), "next_reservation": next_res,
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
        elif rx.gain_model == "elements":
            radio.update(bandwidth_hz=req.get("bandwidth_hz"), gain_db=None, agc=False, gain_mode="manual",
                         gain_elements=dict(req["gain_elements"]))
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
            "recording": {"enabled": True, "engine": rx.engines()[0], "format": rx.formats()[0],
                          "output_directory": f"{root}/{{job_name}}", "minimum_free_space_gb": 1},
            "satdump": {"enabled": False, "operation": "none"},
            "postprocess": {"generate_waterfall": True, "generate_spectrum": True, "checksum": True,
                            "analyze_signal_quality": True, "rf_test": True, "retain_raw_iq": True, "decode": False,
                            "record_metadata": True, "channel_bandwidth_hz": int(req["channel_bandwidth_hz"]),
                            "preset": req.get("preset")},
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
        elif rx.gain_model == "elements":
            req.update(normalize_elements(rx, body, rate), gain_db=None, gain_mode="manual", agc=False)
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
        kind = body.get("test_type") or "quick"
        rx = self._rx(rid)
        if kind in ("quick", "interference", "scan", "gain_sweep", "satellite") and kind not in rx.test_kinds():
            raise SchedulerError(f"{TEST_LABEL.get(kind, kind)} is not implemented for {rx.driver} receivers yet; "
                                 f"available: {', '.join(TEST_LABEL.get(k, k) for k in rx.test_kinds())}", 400)
        if kind in ("quick", "interference"):
            req, make = self.normalize(rid, body), self._job
            if kind == "interference":
                req.update(self._investigation(body))
        elif kind == "scan":
            req, make = self.normalize_scan(rid, body), self._scan_job
        elif kind == "gain_sweep":
            req, make = self.normalize_gain_sweep(rid, body), self._gain_job
        elif kind == "satellite":
            req, make = self.normalize_satellite(rid, body), self._sat_job
        else:
            raise SchedulerError(f"unknown test_type {kind!r}", 400)
        req["test_type"] = kind
        return self._launch(rid, kind, req, make, author, dry_run)

    def _launch(self, rid: str, kind: str, req: dict[str, Any], make: Callable[..., dict[str, Any]], author: str,
                dry_run: bool) -> dict[str, Any]:
        rx = self._rx(rid)
        av = self.availability(rid, req["duration_s"])
        if not av["available"]:
            raise SchedulerError("This test cannot run safely right now: " + "; ".join(av["reasons"])
                                 + ". Nothing was started; try again later.", 409)
        now = self.s.clock()
        start = now + dt.timedelta(seconds=int(self.s.settings.get("test_min_lead_seconds", 20)) + 5)
        test_id = _now_id("t", now)
        job = make(rx, req, test_id, start)
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
                test_id=test_id, test_type=kind, job_id=r0["job_id"], receiver_id=rid, name=req["name"],
                description=req["description"], signal_type=req.get("signal_type"), tags=req["tags"],
                baseline_test_id=req["baseline_test_id"], component_id=req.get("component_id"), refs=req.get("refs"),
                group_id=group, request={**req, "switch_port": port}, rf_path=snap, notes=None, result=None,
                created_by=author, created_at=now, updated_at=now))
            if base and not base["group_id"]:
                conn.execute(rf_tests.update().where(rf_tests.c.test_id == base["test_id"]).values(group_id=group))
        LOG.info("rf test %s on %s queued as job %s by %s", test_id, rid, r0["job_id"], author)
        return {"ok": True, "test_id": test_id, "job_id": r0["job_id"], "starts_at": start.isoformat(),
                "warnings": r0.get("warnings", []), "estimated_bytes": (r0.get("job") or {}).get("estimated_bytes")}

    # ================================================================ wideband scans
    def _common_fields(self, rx: Any, body: dict[str, Any]) -> dict[str, Any]:
        """Name/tags/baseline/antenna/gain fields every recorded test type shares (validated)."""
        tags = [str(t).strip() for t in (body.get("tags") or []) if str(t).strip()]
        if len(tags) > 12 or any(not _TAG.match(t) for t in tags):
            raise SchedulerError("tags: up to 12, each 1-32 letters, digits, spaces or . _ + -", 400)
        out: dict[str, Any] = {"name": str(body.get("name") or "").strip()[:120] or None,
                               "description": str(body.get("description") or "").strip()[:4000] or None,
                               "tags": tags, "baseline_test_id": body.get("baseline_test_id") or None, "antenna": None}
        if out["baseline_test_id"] and self._row(out["baseline_test_id"]) is None:
            raise SchedulerError(f"no test {out['baseline_test_id']}", 404)
        if rx.switched:
            ant = body.get("antenna")
            if not ant or str(ant) not in {p["antenna"] for p in self.s.switch_ports.values()}:
                raise SchedulerError(f"{rx.id} is fed through ANTSW-01: choose an antenna that is on a switch port", 400)
            out["antenna"] = str(ant)
        return out

    def _gain_fields(self, rx: Any, body: dict[str, Any]) -> dict[str, Any]:
        if rx.driver == "rtlsdr":
            mode = body.get("gain_mode") or "manual"
            g = body.get("gain_db")
            if mode == "manual" and (g is None or float(g) not in R820T_GAINS_DB):
                raise SchedulerError(f"gain_db must be one of the tuner's steps: {list(R820T_GAINS_DB)}", 400)
            return {"gain_mode": mode, "gain_db": float(g) if mode == "manual" else None, "agc": False, "bandwidth_hz": None}
        bw = int(body.get("bandwidth_hz") or 0)
        if bw not in SDRPLAY_BANDWIDTHS_HZ:
            raise SchedulerError(f"bandwidth_hz must be one of {list(SDRPLAY_BANDWIDTHS_HZ)}", 400)
        if body.get("rfgr") is None:
            raise SchedulerError("SDRplay tests need RFGR (the LNA state)", 400)
        agc = bool(body.get("agc"))
        return {"agc": agc, "ifgr_db": int(body.get("ifgr_db") if body.get("ifgr_db") is not None else 40),
                "rfgr": int(body["rfgr"]), "bandwidth_hz": bw, "gain_db": None, "gain_mode": "auto" if agc else "manual"}

    def normalize_scan(self, rid: str, body: dict[str, Any]) -> dict[str, Any]:
        from . import scanmetrics as sm
        rx = self._rx(rid)
        lo, hi = parse_hz(body.get("start"), "start frequency"), parse_hz(body.get("stop"), "stop frequency")
        rate = int(body.get("sample_rate_hz") or (2_400_000 if rx.driver == "rtlsdr" else 2_000_000))
        res = body.get("resolution") or "auto"
        if res not in ("auto", "coarse", "medium", "fine"):
            try:
                res = int(float(res))
            except (TypeError, ValueError):
                raise SchedulerError("resolution must be auto, coarse, medium, fine or a number of Hz", 400) from None
            if not 100 <= res <= 200_000:
                raise SchedulerError("custom resolution must be 100-200000 Hz", 400)
        dwell = float(body.get("dwell_s") or 0.25)
        if not 0.02 <= dwell <= 5:
            raise SchedulerError("dwell must be 0.02-5 s per step", 400)
        mode = body.get("scan_mode") or "average"
        if mode not in ("average", "peak", "repeated", "filter"):
            raise SchedulerError("scan_mode must be average, peak, repeated or filter", 400)
        repeats = int(body.get("repeats") or (5 if mode == "repeated" else 1))
        if not 1 <= repeats <= 50:
            raise SchedulerError("repeats must be 1-50", 400)
        try:
            plan = sm.plan(lo, hi, rate, res, dwell)
        except ValueError as exc:
            raise SchedulerError(str(exc), 400) from None
        fmin, fmax = (24_000_000, 1_766_000_000) if rx.driver == "rtlsdr" else (1_000, 2_000_000_000)
        half = rate * sm.USABLE / 2
        if lo - half < fmin or hi + half > fmax:
            raise SchedulerError(f"{rx.id} tunes {fmin / 1e6:g}-{fmax / 1e6:g} MHz; a scan of "
                                 f"{lo / 1e6:g}-{hi / 1e6:g} MHz needs steps outside that", 400)
        settle = 0.06 if rx.driver == "rtlsdr" else 0.08
        n = len(plan["centers_hz"])
        if n * repeats > 4000:
            raise SchedulerError(f"{n} steps × {repeats} sweeps is too many; use a coarser resolution or fewer sweeps", 400)
        est = sm.estimate_seconds(n, dwell, settle, repeats, rx.driver)
        regions = []
        for r in body.get("regions") or ([sm.FM_REGION] if mode == "filter" or (lo < 108e6 and hi > 88e6) else []):
            try:
                a, b = parse_hz(r.get("start_hz") or r.get("start"), "region start"), parse_hz(r.get("stop_hz") or r.get("stop"), "region stop")
            except SchedulerError:
                continue
            if b > a:
                regions.append({"name": str(r.get("name") or f"{a / 1e6:g}-{b / 1e6:g} MHz")[:60], "start_hz": a,
                                "stop_hz": b, "role": r.get("role") if r.get("role") in ("stop", "watch") else "stop"})
        req = {"start_hz": lo, "stop_hz": hi, "sample_rate_hz": rate, "resolution": res, "dwell_s": dwell,
               "scan_mode": mode, "repeats": repeats, "settle_s": settle, "regions": regions[:8],
               "plan": {k: v for k, v in plan.items() if k != "centers_hz"}, "centers_hz": plan["centers_hz"],
               "estimated_seconds": round(est, 1), "duration_s": int(math.ceil(est * 1.25 + 6)),
               "estimated_bytes": int(n * repeats * dwell * rate * (2 if rx.driver == "rtlsdr" else 4)),
               "frequency_hz": (lo + hi) // 2, "signal_type": None, "channel_bandwidth_hz": None}
        req.update(self._common_fields(rx, body))
        req.update(self._gain_fields(rx, body))
        return req

    def scan_plan(self, rid: str, body: dict[str, Any]) -> dict[str, Any]:
        """What a scan would do, before anything is booked."""
        req = self.normalize_scan(rid, body)
        return {"steps": len(req["centers_hz"]), "repeats": req["repeats"], "rbw_hz": round(req["plan"]["rbw_hz"], 1),
                "nfft": req["plan"]["nfft"], "stride_hz": req["plan"]["stride_hz"], "estimated_seconds": req["estimated_seconds"],
                "reserved_seconds": req["duration_s"], "estimated_bytes": req["estimated_bytes"], "regions": req["regions"],
                "availability": self.availability(rid, req["duration_s"])}

    def _scan_job(self, rx: Any, req: dict[str, Any], test_id: str, start: dt.datetime) -> dict[str, Any]:
        q = dict(req, center_frequency_hz=req["centers_hz"][0], frequency_hz=req["centers_hz"][0],
                 channel_bandwidth_hz=25_000)
        job = self._job(rx, q, test_id, start)
        job["target"] = {"type": "manual", "name": (req.get("name") or
                         f"scan {req['start_hz'] / 1e6:g}-{req['stop_hz'] / 1e6:g} MHz")[:80]}
        job["radio"]["target_frequency_hz"] = None
        gain: Any = (req["gain_db"] if req["gain_mode"] == "manual" else "auto") if rx.driver == "rtlsdr" else \
            {"IFGR": req["ifgr_db"], "RFGR": req["rfgr"]}
        job["steps"] = {"steps": [{"center_hz": c, "dwell_s": req["dwell_s"], "gain": gain} for c in req["centers_hz"]],
                        "repeats": req["repeats"], "settle_s": req["settle_s"]}
        job["postprocess"] = {"generate_waterfall": False, "generate_spectrum": False, "checksum": False,
                              "analyze_signal_quality": False, "retain_raw_iq": True, "decode": False,
                              "record_metadata": True,
                              "rf_scan": {**req["plan"], "regions": req["regions"], "threshold_db": 10.0}}
        job["metadata"]["experiment"] = "wideband scan"
        return job

    def _npz(self, test_id: str) -> tuple[dict[str, Any], Any]:
        from . import scanmetrics as sm
        d = self.detail(test_id)
        if d["test"]["test_type"] != "scan":
            raise SchedulerError(f"{test_id} is not a wideband scan", 400)
        f = d["files"].get("scan_npz")
        if not f:
            raise SchedulerError(f"scan {test_id} has no results yet ({d['progress']['label']})", 409)
        return d, sm.load(pathlib.Path(f["path"]))

    def compare_scans(self, a_id: str, b_id: str, regions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        from . import scanmetrics as sm
        A, a = self._npz(a_id)
        B, b = self._npz(b_id)
        regs = regions if regions is not None else (A["test"]["request"].get("regions") or B["test"]["request"].get("regions") or [])
        try:
            c = sm.compare(a, b, regs)
        except ValueError as exc:
            raise SchedulerError(str(exc), 400) from None
        ctx = _scan_context(A, B)
        c.update(ctx)
        c["report"] = scan_report(c, ctx, regs)
        c["a"], c["b"] = _scan_brief(A), _scan_brief(B)
        return c

    def filter_test(self, body: dict[str, Any], author: str) -> dict[str, Any]:
        """Over-the-air comparison of two scans, with and without a filter; saved to the filter's history."""
        from . import scanmetrics as sm
        comp_id = body.get("component_id")
        comps = (self.s.rf.current or {}).get("layout", {}).get("components", {}) if getattr(self.s, "rf", None) else {}
        A, a = self._npz(body.get("baseline_test_id") or "")
        B, b = self._npz(body.get("modified_test_id") or "")
        stop = {"name": str(body.get("stopband_name") or "stopband")[:60],
                "start_hz": parse_hz(body.get("stopband_start") or 88e6, "stopband start"),
                "stop_hz": parse_hz(body.get("stopband_stop") or 108e6, "stopband stop")}
        if stop["stop_hz"] <= stop["start_hz"]:
            raise SchedulerError("stopband stop must be above its start", 400)
        pa = {c.get("id") for c in ((A["job"] or {}).get("rf_path") or {}).get("chain", [])}
        pb = {c.get("id") for c in ((B["job"] or {}).get("rf_path") or {}).get("chain", [])}
        if comp_id and not (comp_id in pb and comp_id not in pa):
            note = (f"the recorded paths do not show {comp_id} being added between the two scans "
                    "(baseline without it, modified with it); the result may not be about this part")
        else:
            note = None
        c = sm.compare(a, b, [dict(stop, role="stop")])
        resp = sm.filter_response(c, a, b, stop)
        ctx = _scan_context(A, B)
        conf, why = _filter_confidence(resp, ctx)
        result = {k: v for k, v in resp.items()}
        result.update(confidence=conf, confidence_reasons=why, path_note=note, settings_differ=ctx["settings_differ"],
                      time_between_s=ctx["time_between_s"], component_name=(comps.get(comp_id) or {}).get("name"),
                      method="over-the-air comparative: attenuation = baseline − modified, only where the baseline "
                             "showed signal ≥ 6 dB above its own noise floor. Not a calibrated insertion-loss or "
                             "S-parameter measurement.")
        if body.get("save", True):
            now = self.s.clock()
            tid = _now_id("t", now)
            with self.s.engine.begin() as conn:
                conn.execute(rf_tests.insert().values(
                    test_id=tid, test_type="filter_test", job_id=None, receiver_id=A["test"]["receiver_id"],
                    component_id=comp_id, refs={"baseline_test_id": A["test"]["test_id"], "modified_test_id": B["test"]["test_id"]},
                    name=str(body.get("name") or f"filter test {(comps.get(comp_id) or {}).get('name', comp_id or '')}")[:120],
                    description=None, signal_type=None, tags=["filter-test"], baseline_test_id=A["test"]["test_id"],
                    group_id=A["test"]["group_id"] or A["test"]["test_id"],
                    request={"stopband": stop, "component_id": comp_id, "start_hz": max(A["test"]["request"]["start_hz"], B["test"]["request"]["start_hz"]),
                             "stop_hz": min(A["test"]["request"]["stop_hz"], B["test"]["request"]["stop_hz"])},
                    rf_path=(B["job"] or {}).get("rf_path"), notes=None, result=result, created_by=author,
                    created_at=now, updated_at=now))
            result["test_id"] = tid
        return result

    # ================================================================ interference investigation
    INPUT_STATES = {"antenna": "Antenna connected", "terminated": "50-Ω termination on the input",
                    "open": "Input open (antenna removed, no termination)"}

    def _investigation(self, body: dict[str, Any]) -> dict[str, Any]:
        from .interference import PRESETS
        preset = body.get("preset") or "general"
        if preset not in PRESETS:
            raise SchedulerError(f"preset must be one of {list(PRESETS)}", 400)
        state = body.get("input_state") or "antenna"
        if state not in self.INPUT_STATES:
            raise SchedulerError(f"input_state must be one of {list(self.INPUT_STATES)}", 400)
        return {"preset": preset, "input_state": state, "workflow": body.get("workflow") or None}

    def compare_termination(self, a_id: str, b_id: str) -> dict[str, Any]:
        from .interference import compare_termination
        A, B = self.detail(a_id), self.detail(b_id)
        for t in (A, B):
            if not t["metrics"] or "signals" not in t["metrics"]:
                raise SchedulerError(f"test {t['test']['test_id']} has no signal-test results yet", 409)
        sa, sb = (A["test"]["request"] or {}).get("input_state"), (B["test"]["request"] or {}).get("input_state")
        r = compare_termination(A["metrics"], B["metrics"])
        ctx = compare_metrics(A, B)
        warns = [w for w in ctx["report"]["warnings"] if not w.startswith("Nothing differs")]   # the antenna is the change
        if sb == "open":
            warns.insert(0, "The second recording used an OPEN input, not a 50-Ω termination. An open input can pick "
                            "up signals by itself and does not present the receiver's normal impedance.")
        if sa != "antenna" or sb not in ("terminated", "open"):
            warns.insert(0, f"Expected A = antenna connected and B = antenna removed; recorded states are "
                            f"{self.INPUT_STATES.get(sa, sa)} and {self.INPUT_STATES.get(sb, sb)}.")
        interp = []
        if r["remained"]:
            interp.append(f"{len(r['remained'])} signal(s) stayed with the antenna removed. They may come from inside "
                          "the receiver, or enter through the USB cable, power supply or another coupling path.")
        if r["disappeared"]:
            interp.append(f"{len(r['disappeared'])} signal(s) vanished without the antenna. They probably arrive through "
                          "the antenna system or something connected in its path (including powered parts).")
        return {**r, "a": _brief(A), "b": _brief(B), "input_states": [sa, sb], "settings_differ": ctx["settings_differ"],
                "report": {"measured": [f"{len(r['remained'])} signal(s) present in both, {len(r['disappeared'])} only "
                                        f"with the antenna, {len(r['only_without_antenna'])} only without it."]
                           + ([f"Noise floor changed by {r['floor_change_db']:+.1f} dB with the antenna removed."]
                              if r["floor_change_db"] is not None else []),
                           "interpretations": interp,
                           "hypotheses": ["Removing the antenna does not conclusively identify where interference "
                                          "comes from; it narrows down where to look."],
                           "warnings": warns}}

    # ================================================================ gain sweep
    RTL_SWEEP = (0.0, 8.7, 16.6, 22.9, 29.7, 33.8, 38.6, 43.4, 49.6)

    def normalize_gain_sweep(self, rid: str, body: dict[str, Any]) -> dict[str, Any]:
        rx = self._rx(rid)
        b = dict(body, gain_db=self.RTL_SWEEP[0] if rx.driver == "rtlsdr" else None, gain_mode="manual")
        if rx.driver == "sdrplay":
            b.setdefault("rfgr", 4)
        req = self.normalize(rid, b)
        dwell = float(body.get("dwell_s") or 1.0)
        if not 0.2 <= dwell <= 10:
            raise SchedulerError("dwell per gain must be 0.2-10 s", 400)
        if rx.driver == "rtlsdr":
            gains = [float(g) for g in (body.get("gains") or self.RTL_SWEEP)]
            bad = [g for g in gains if g not in R820T_GAINS_DB]
            if bad or not 2 <= len(gains) <= 29:
                raise SchedulerError(f"gains must be 2-29 of the tuner's steps {list(R820T_GAINS_DB)}", 400)
            steps_gain: list[Any] = sorted(set(gains))
        else:
            element = body.get("sweep_element") or "IFGR"
            if element not in ("IFGR", "RFGR"):
                raise SchedulerError("sweep_element must be IFGR or RFGR", 400)
            vals = [int(v) for v in (body.get("gains") or ([59, 50, 40, 30, 20] if element == "IFGR" else [9, 6, 4, 2, 0]))]
            fixed_if, fixed_rf = int(body.get("ifgr_db") or 40), int(body.get("rfgr") if body.get("rfgr") is not None else 4)
            steps_gain = [{"IFGR": v if element == "IFGR" else fixed_if, "RFGR": v if element == "RFGR" else fixed_rf}
                          for v in vals]
            req.update(sweep_element=element, ifgr_db=steps_gain[0]["IFGR"], rfgr=steps_gain[0]["RFGR"], agc=False)
        settle = 0.2
        est = 4 + len(steps_gain) * (dwell + settle + 0.1)
        req.update(gains=steps_gain, dwell_s=dwell, settle_s=settle, estimated_seconds=round(est, 1),
                   duration_s=int(math.ceil(est * 1.25 + 6)))
        return req

    def _gain_job(self, rx: Any, req: dict[str, Any], test_id: str, start: dt.datetime) -> dict[str, Any]:
        job = self._job(rx, req, test_id, start)
        job["steps"] = {"steps": [{"center_hz": req["center_frequency_hz"], "dwell_s": req["dwell_s"], "gain": g}
                                  for g in req["gains"]], "repeats": 1, "settle_s": req["settle_s"]}
        job["postprocess"] = {"generate_waterfall": False, "generate_spectrum": False, "checksum": False,
                              "analyze_signal_quality": False, "retain_raw_iq": True, "decode": False,
                              "record_metadata": True, "rf_gain_sweep": True,
                              "channel_bandwidth_hz": int(req["channel_bandwidth_hz"])}
        job["metadata"]["experiment"] = "gain sweep"
        return job

    # ================================================================ satellite reception quality
    def satellite_options(self) -> dict[str, Any]:
        from .satquality import SAT_PROFILES, satdump_info
        info = satdump_info(self.s.settings.get("satdump_path", "satdump"),
                           self.s.settings.get("satdump_pipelines_dir", "/usr/share/satdump/pipelines"))
        self.s.settings["_satdump_version"] = info.get("version")
        recs = self.svc.catalog.recordings(per_page=60).get("recordings", []) if self.svc.catalog else []
        recs = [{k: r.get(k) for k in ("recording_id", "receiver", "target", "start", "start_local", "duration_s", "format",
                                       "sample_rate_hz", "center_frequency_hz", "target_frequency_hz", "decode")}
                for r in recs if r.get("size_bytes")]
        return {"satdump": info, "profiles": SAT_PROFILES, "recordings": recs}

    def normalize_satellite(self, rid: str, body: dict[str, Any]) -> dict[str, Any]:
        from .satquality import SAT_PROFILES
        sat = body.get("satellite") or "METEOR-M2-4"
        prof = SAT_PROFILES.get(sat)
        if prof is None:
            raise SchedulerError(f"satellite must be one of {list(SAT_PROFILES)}", 400)
        pipe = body.get("pipeline") or prof["pipeline"]
        if pipe not in self.s.pipelines:
            raise SchedulerError(f"SatDump pipeline {pipe!r} is not installed", 400)
        b = dict(body, frequency=body.get("frequency") or prof["frequency_hz"], signal_type="meteor_lrpt",
                 channel_bandwidth_hz=prof["channel_bandwidth_hz"], duration_s=body.get("duration_s") or 60)
        req = self.normalize(rid, b)
        req.update(satellite=sat, pipeline=pipe, norad_id=prof["norad_id"], preset="satellite")
        return req

    def _sat_job(self, rx: Any, req: dict[str, Any], test_id: str, start: dt.datetime) -> dict[str, Any]:
        job = self._job(rx, req, test_id, start)
        job["target"] = {"type": "satellite", "name": req["satellite"], "satellite": req["satellite"], "norad_id": req["norad_id"]}
        root = job["recording"]["output_directory"].rsplit("/", 1)[0]
        job["satdump"] = {"enabled": True, "operation": "decode_after_recording", "pipeline": req["pipeline"],
                          "input_type": "baseband", "baseband_format": job["recording"]["format"],
                          "sample_rate_hz": req["sample_rate_hz"], "frequency_hz": req["frequency_hz"],
                          "output_directory": f"{root}/{{job_name}}/decoded"}
        job["postprocess"]["decode"] = True
        job["metadata"]["experiment"] = "satellite quality test"
        return job

    def satellite_existing(self, body: dict[str, Any], author: str) -> dict[str, Any]:
        """Satellite quality of a recording that already exists (e.g. a past pass): analysis + decode attempts."""
        from .satquality import SAT_PROFILES
        rid = body.get("recording_id") or ""
        rec = self.s.get(rid)
        if rec is None:
            raise SchedulerError(f"no recording {rid}", 404)
        sat = body.get("satellite") or ("METEOR-M2-3" if "M2-3" in (rec.get("target") or "") else "METEOR-M2-4")
        prof = SAT_PROFILES.get(sat)
        if prof is None:
            raise SchedulerError(f"satellite must be one of {list(SAT_PROFILES)}", 400)
        freq = parse_hz(body.get("frequency") or prof["frequency_hz"], "frequency")
        win = {k: float(body[k]) for k in ("start_s", "end_s") if body.get(k) not in (None, "")}
        p = self.svc.processor
        common = {"target_frequency_hz": freq, **win}
        an = p.submit(rid, "analyze", {**ANALYSIS_PARAMS, **common, "channel_bandwidth_hz": prof["channel_bandwidth_hz"],
                                       "preset": "satellite", "analysis": ["waterfall", "spectrum", "signal_detection",
                                                                           "clipping", "integrity", "rf_test"]},
                      "rf-lab", author)
        dec = p.submit(rid, "decode", {"pipeline": body.get("pipeline") or prof["pipeline"], **common}, "rf-lab", author)
        attempts = {"analyze": an["attempt_id"], "decode": dec["attempt_id"]}
        if body.get("try_conjugate"):
            attempts["decode_conjugate"] = p.submit(rid, "decode", {"pipeline": body.get("pipeline") or prof["pipeline"],
                                                                    **common, "iq_conjugate": True}, "rf-lab", author)["attempt_id"]
        now = self.s.clock()
        tid = _now_id("t", now)
        with self.s.engine.begin() as conn:
            conn.execute(rf_tests.insert().values(
                test_id=tid, test_type="satellite", job_id=rid, receiver_id=rec["receiver_id"], component_id=None,
                refs={"source": "existing recording", "recording_id": rid, "attempts": attempts},
                name=str(body.get("name") or f"{sat} quality · {rid}")[:120], description=body.get("description"),
                signal_type="meteor_lrpt", tags=["satellite"], baseline_test_id=None, group_id=None,
                request={"satellite": sat, "frequency_hz": freq, "pipeline": body.get("pipeline") or prof["pipeline"],
                         "window": win or None, "try_conjugate": bool(body.get("try_conjugate"))},
                rf_path=((rec["spec"].get("resolved") or {}).get("rf_path")), notes=None, result=None,
                created_by=author, created_at=now, updated_at=now))
        return {"ok": True, "test_id": tid, "attempts": attempts}

    def _sat_detail(self, row: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        from .satquality import SAT_PROFILES, ladder
        p = self.svc.processor
        refs = row.get("refs") or {}
        atts = p.attempts(row["job_id"])
        want = set((refs.get("attempts") or {}).values())
        mine = [a for a in atts if a["attempt_id"] in want] if want else [a for a in atts if a["origin"] == "post-recording"]
        decodes = []
        for a in mine:
            if a["kind"] != "decode":
                continue
            r = ((a.get("result") or {}).get("inputs") or [{}])[0] if a.get("result") else {}
            imgs = [x for x in p.artifacts(row["job_id"], "image", a["attempt_id"]) if "(blank)" not in (x.get("label") or "")]
            logs = p.artifacts(row["job_id"], "log", a["attempt_id"])
            decodes.append({"attempt_id": a["attempt_id"], "status": a["status"], "variant": r.get("variant") or
                            ("EXPERIMENTAL: conjugated IQ" if (a.get("resolved") or {}).get("iq_conjugate") else "standard"),
                            "cadu_frames": r.get("cadu_frames"), "images": r.get("images") or 0, "sat": r.get("sat"),
                            "best_target_frequency_hz": r.get("best_target_frequency_hz"), "search": r.get("search"),
                            "error": r.get("error") or a.get("status_reason"), "satdump_version": r.get("satdump_version"),
                            "image_artifacts": [{"artifact_id": x["artifact_id"], "label": x["label"]} for x in imgs[:12]],
                            "log_artifacts": [x["artifact_id"] for x in logs[:3]]})
        an = next((a for a in mine if a["kind"] == "analyze"), None)
        rf = None
        if an is not None:
            f = next((x for x in p.artifacts(row["job_id"], None, an["attempt_id"])
                      if pathlib.Path(x["path"]).name.startswith("rf_test") and x["path"].endswith(".json")), None)
            if f:
                try:
                    rf = json.loads(pathlib.Path(f["path"]).read_text())
                except (OSError, ValueError):
                    rf = None
        req = row.get("request") or {}
        prof = dict(SAT_PROFILES.get(req.get("satellite") or "METEOR-M2-4") or {}, frequency_hz=req.get("frequency_hz"))
        pending = [a for a in mine if a["status"] in ("queued", "running", "paused")]
        return {"profile": prof, "decodes": decodes, "rf": rf, "ladder": ladder(prof, rf, [d for d in decodes if d["cadu_frames"] is not None]),   # ran, even if it found nothing
                "pending": [{"attempt_id": a["attempt_id"], "kind": a["kind"], "status": a["status"]} for a in pending],
                "satellite": req.get("satellite")}

    def sat_compare(self, body: dict[str, Any], author: str) -> dict[str, Any]:
        from .satquality import SAT_PROFILES
        a, b = body.get("recording_a"), body.get("recording_b")
        ra, rb = self.s.get(a or ""), self.s.get(b or "")
        if ra is None or rb is None:
            raise SchedulerError("choose two existing recordings", 404)
        sat = body.get("satellite") or "METEOR-M2-4"
        prof = SAT_PROFILES.get(sat) or SAT_PROFILES["METEOR-M2-4"]
        freq = parse_hz(body.get("frequency") or prof["frequency_hz"], "frequency")
        p = self.svc.processor
        cmp = p.submit(a, "compare", {"recordings": [a, b], "mode": "satellite", "target_frequency_hz": freq,
                                      "channel_bandwidth_hz": prof["channel_bandwidth_hz"]}, "rf-lab", author)
        attempts = {"compare": cmp["attempt_id"]}
        for key, rid in (("decode_a", a), ("decode_b", b)):
            done = [x for x in p.attempts(rid) if x["kind"] == "decode" and x["status"] == "completed"]
            if not done or body.get("redecode"):        # identical decoder configuration for both recordings
                attempts[key] = p.submit(rid, "decode", {"pipeline": prof["pipeline"], "target_frequency_hz": freq},
                                         "rf-lab", author)["attempt_id"]
            else:
                attempts[key] = done[-1]["attempt_id"]
        now = self.s.clock()
        tid = _now_id("t", now)
        with self.s.engine.begin() as conn:
            conn.execute(rf_tests.insert().values(
                test_id=tid, test_type="sat_compare", job_id=None, receiver_id=ra["receiver_id"], component_id=None,
                refs={"recordings": [a, b], "attempts": attempts}, name=str(body.get("name") or f"{sat}: {a} vs {b}")[:120],
                description=None, signal_type="meteor_lrpt", tags=["satellite", "pass-comparison"], baseline_test_id=None,
                group_id=None, request={"satellite": sat, "frequency_hz": freq, "recordings": [a, b]}, rf_path=None,
                notes=None, result=None, created_by=author, created_at=now, updated_at=now))
        return {"ok": True, "test_id": tid, "attempts": attempts}

    def _sat_compare_detail(self, row: dict[str, Any]) -> dict[str, Any]:
        p = self.svc.processor
        refs = row["refs"] or {}
        ids, atts = refs.get("recordings") or [], refs.get("attempts") or {}
        status = {k: p.attempt(v) for k, v in atts.items()}
        pending = [k for k, v in status.items() if v["status"] in ("queued", "running", "paused")]
        data = None
        f = next((x for x in p.artifacts(ids[0], None, atts.get("compare")) if x["path"].endswith("sat_compare.json")), None)
        if f:
            try:
                data = json.loads(pathlib.Path(f["path"]).read_text())
            except (OSError, ValueError):
                data = None
        side = {}
        for i, rid in enumerate(ids):
            rec = self.s.get(rid) or {}
            d = status.get("decode_" + "ab"[i]) or {}
            r = ((d.get("result") or {}).get("inputs") or [{}])[0] if d.get("result") else {}
            snap = ((rec.get("spec") or {}).get("resolved") or {}).get("rf_path")
            side[rid] = {"receiver_id": rec.get("receiver_id"), "target": rec.get("target"), "record_start": rec.get("record_start"),
                         "path": (snap or {}).get("text"), "path_recorded": bool(snap),
                         "decode": {"status": d.get("status"), "cadu_frames": r.get("cadu_frames"), "images": r.get("images"),
                                    "sat": r.get("sat"), "error": r.get("error")}}
        comp = status.get("compare") or {}
        report = _sat_compare_report(data, side, ids) if data else None
        return {"recordings": ids, "pending": pending, "compare_status": comp.get("status"),
                "compare_error": comp.get("status_reason") if comp.get("status") == "failed" else None,
                "data": data, "side": side, "report": report}

    # ================================================================ reading tests
    def _row(self, test_id: str) -> dict[str, Any] | None:
        with self.s.engine.connect() as conn:
            r = conn.execute(select(rf_tests).where(rf_tests.c.test_id == test_id)).mappings().first()
        return dict(r) if r else None

    def _metrics_file(self, job_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        p = self.svc.processor
        arts = p.artifacts(job_id)
        f = next((a for a in reversed(arts) if pathlib.Path(a["path"]).name.split(".")[0] in
                  ("rf_test", "rf_scan", "rf_gain_sweep") and a["path"].endswith(".json")), None)
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
        if row["test_type"] == "sat_compare":
            sc = self._sat_compare_detail(row)
            done = not sc["pending"]
            label = "Complete" if done and sc["data"] else ("Comparison failed: " + str(sc["compare_error"]) if done
                                                            else f"Processing: {', '.join(sc['pending'])}")
            return {"test": self._public(row), "job": None, "progress": {"phase": "complete" if done else "processing",
                    "label": label, "done": done}, "metrics": None, "sat_compare": sc, "files": {}}
        if not row["job_id"]:                    # analysis of existing data (filter test, comparison)
            return {"test": self._public(row), "job": None, "progress": {"phase": "complete", "label": "Complete",
                    "done": True}, "metrics": None, "result": row["result"], "files": {}}
        job = self.s.get(row["job_id"])
        if job is None:
            raise SchedulerError(f"test {test_id} lost its recording {row['job_id']}", 500)
        attempts = self.svc.processor.attempts(row["job_id"]) if self.svc.processor else []
        prog = self.progress(job, attempts)
        sat = None
        if row["test_type"] == "satellite":
            sat = self._sat_detail(row, job)
            if (row.get("refs") or {}).get("source") == "existing recording":
                prog = ({"phase": "processing", "label": "Analysing and decoding: " + ", ".join(
                         f"{x['kind']} {x['status']}" for x in sat["pending"]), "done": False} if sat["pending"]
                        else {"phase": "complete", "label": "Complete", "done": True})
            elif prog["done"] and sat["pending"]:
                prog = {"phase": "processing", "label": "Decoding with SatDump", "done": False}
        metrics, mart = (None, None)
        if job["status"] in ("completed", "processing", "partial"):
            metrics, mart = self._metrics_file(row["job_id"])
        if metrics and metrics.get("summary", {}).get("channel") is not None and "investigation" not in metrics:
            from .interference import diagnose
            metrics["investigation"] = diagnose(metrics, (row["request"] or {}).get("preset") or "general")
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
                   "metrics" if name.split(".")[0] in ("rf_test", "rf_scan", "rf_gain_sweep") and name.endswith(".json") else
                   "scan_npz" if name == "rf_scan.npz" else "health" if name.endswith(".health.json") else
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
                "progress": prog, "metrics": (sat or {}).get("rf") or metrics, "files": files, "satellite": sat}

    def _aliases(self, part: str) -> set[str]:
        rf = getattr(self.s, "rf", None)
        return rf.aliases(part) if rf is not None else {part}

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {k: _iso(v) for k, v in row.items()}

    def list(self, receiver: str | None = None, tag: str | None = None, part: str | None = None,
             fmin: float | None = None, fmax: float | None = None, since: str | None = None, until: str | None = None,
             status: str | None = None, sort: str = "date", limit: int = 200,
             test_type: str | None = None) -> list[dict[str, Any]]:
        q = select(rf_tests, sched_jobs.c.status, sched_jobs.c.spec, sched_jobs.c.record_start).outerjoin(
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
            if part and not (self._aliases(part) & set(parts)):
                continue
            f = req.get("start_hz") or req.get("frequency_hz") or 0           # scans: the range start
            f_hi = req.get("stop_hz") or f
            if fmin is not None and f_hi < fmin * 1e6 or fmax is not None and f > fmax * 1e6:
                continue
            if test_type and r["test_type"] != test_type:
                continue
            res = r["result"] or {}
            if not res and r["job_id"] and r["status"] in ("completed", "partial") and r["test_type"] != "sat_compare":
                m, _ = self._metrics_file(r["job_id"])          # first time anyone looks: cache the summary
                if m and m.get("summary"):
                    res = r["result"] = m["summary"]
                    with self.s.engine.begin() as conn:
                        conn.execute(rf_tests.update().where(rf_tests.c.test_id == r["test_id"]).values(result=res))
            state = "complete" if res else (r["status"] or "complete")
            if status and status != state:
                continue
            out.append({"test_id": r["test_id"], "test_type": r["test_type"], "job_id": r["job_id"],
                        "receiver_id": r["receiver_id"], "component_id": r["component_id"], "refs": r["refs"],
                        "stop_hz": req.get("stop_hz"), "summary": {k: v for k, v in res.items() if k not in ("curve", "grid")} if r["test_type"] != "quick" else None,
                        "name": r["name"], "created_at": _iso(r["created_at"]),
                        "record_start": _iso(r["record_start"] or r["created_at"]),
                        "status": state, "frequency_hz": f, "sample_rate_hz": req.get("sample_rate_hz"),
                        "gain": req.get("gain_db") if req.get("gain_db") is not None else
                        (f"IFGR {req.get('ifgr_db')} RFGR {req.get('rfgr')}" if req.get("rfgr") is not None else req.get("gain_mode")),
                        "duration_s": req.get("duration_s"), "tags": r["tags"] or [], "path": path.get("text"),
                        "parts": [c.get("id") for c in path.get("chain", [])], "baseline_test_id": r["baseline_test_id"],
                        "part_names": {c.get("id"): c.get("name") for c in path.get("chain", [])},
                        "group_id": r["group_id"], "signal_type": r["signal_type"],
                        "noise_floor_dbfs_per_hz": res.get("noise_floor_dbfs_per_hz"), "snr_db": res.get("snr_db"),
                        "channel_power_dbfs": (res.get("channel") or {}).get("mean_power_dbfs"),
                        "clipped_fraction": res.get("clipped_fraction"), "strong_peaks": res.get("strong_peaks")})
        for x in out:                         # scans/filter tests summarize differently
            sm = x.get("summary") or {}
            if x["test_type"] == "scan":
                x["noise_floor_dbfs_per_hz"] = sm.get("noise_floor_median_dbfs_per_hz")
                x["strong_peaks"] = sm.get("strong_carriers")
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
img{{max-width:100%;border:1px solid #ddd;margin:6px 0}}img.logo{{height:56px;border:0;margin:0 0 4px}}.m{{color:#555}}@media print{{h2{{break-after:avoid}}img{{break-inside:avoid}}}}</style>
</head><body><img class="logo" src="{logo_data_uri()}" alt="{LOGO_ALT}"><h1>Signal path test — {e(t.get('name') or t['test_id'])}</h1>
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
    inv = m.get("investigation")
    if inv:
        parts.append(f"<h2>Interference investigation · {e(inv['preset_label'])}</h2><table><tr><th>Anomaly</th><td><b>"
                     "Where · when · evidence · severity / confidence · possible explanations</b></td></tr>"
                     + "".join(_anomaly_row(a) for a in inv["anomalies"]) + "</table>"
                     + ("<p class='m'>No anomaly crossed the thresholds.</p>" if not inv["anomalies"] else "")
                     + "".join(f"<p class='m'>{e(n)}</p>" for n in inv.get("notes", []))
                     + "<h2>Suggested follow-up</h2><ul>" + "".join(f"<li><b>{e(x['title'])}</b>: because {e(x['because'])}</li>"
                                                                    for x in inv["suggestions"]) + "</ul>")
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


# ==================================================================== scan comparison helpers
def _scan_brief(d: dict[str, Any]) -> dict[str, Any]:
    t, j = d["test"], d["job"] or {}
    q = t["request"] or {}
    return {"test_id": t["test_id"], "name": t["name"], "receiver_id": t["receiver_id"], "record_start": j.get("record_start"),
            "path": (j.get("rf_path") or t.get("rf_path") or {}).get("text"), "chain": (j.get("rf_path") or {}).get("chain", []),
            "start_hz": q.get("start_hz"), "stop_hz": q.get("stop_hz"), "rbw_hz": (q.get("plan") or {}).get("rbw_hz"),
            "gain": q.get("gain_db") if q.get("gain_db") is not None else
            (f"IFGR {q.get('ifgr_db')} / RFGR {q.get('rfgr')}" if q.get("rfgr") is not None else "auto"),
            "sample_rate_hz": q.get("sample_rate_hz"), "repeats": q.get("repeats"), "switch_port": j.get("switch_port")}


def _scan_context(A: dict[str, Any], B: dict[str, Any]) -> dict[str, Any]:
    qa, qb = A["test"]["request"], B["test"]["request"]
    diffs = []
    if A["test"]["receiver_id"] != B["test"]["receiver_id"]:
        diffs.append({"setting": "Receiver", "a": A["test"]["receiver_id"], "b": B["test"]["receiver_id"]})
    for k, label in (("gain_db", "Tuner gain (dB)"), ("gain_mode", "Gain mode"), ("ifgr_db", "IFGR"), ("rfgr", "RFGR"),
                     ("agc", "AGC"), ("sample_rate_hz", "Sample rate"), ("bandwidth_hz", "Bandwidth"),
                     ("dwell_s", "Dwell per step"), ("antenna", "Switch antenna")):
        if qa.get(k) != qb.get(k):
            diffs.append({"setting": label, "a": qa.get(k), "b": qb.get(k)})
    if (qa.get("start_hz"), qa.get("stop_hz")) != (qb.get("start_hz"), qb.get("stop_hz")):
        diffs.append({"setting": "Range (MHz)", "a": f"{qa.get('start_hz', 0) / 1e6:g}-{qa.get('stop_hz', 0) / 1e6:g}",
                      "b": f"{qb.get('start_hz', 0) / 1e6:g}-{qb.get('stop_hz', 0) / 1e6:g}"})
    if (qa.get("plan") or {}).get("rbw_hz") != (qb.get("plan") or {}).get("rbw_hz"):
        diffs.append({"setting": "Resolution (RBW, Hz)", "a": (qa.get("plan") or {}).get("rbw_hz"), "b": (qb.get("plan") or {}).get("rbw_hz")})
    ta = dt.datetime.fromisoformat(A["job"]["record_start"])
    tb = dt.datetime.fromisoformat(B["job"]["record_start"])
    return {"settings_differ": diffs, "time_between_s": int(abs((tb - ta).total_seconds())),
            "path": _path_diff(A, B)}


def scan_report(c: dict[str, Any], ctx: dict[str, Any], regions: list[dict[str, Any]]) -> dict[str, Any]:
    facts, interp, hypo, warns = [], [], [], []
    gain_diff = any(d["setting"] in ("Tuner gain (dB)", "Gain mode", "IFGR", "RFGR", "AGC") for d in ctx["settings_differ"])
    if any(d["setting"] == "Receiver" for d in ctx["settings_differ"]):
        warns.append("Different receivers: their tuner responses and ADC scaling differ, so the difference spectrum "
                     "mixes hardware differences with the change you made.")
    if gain_diff:
        warns.append("Gain differs between the scans: part of every difference is the gain change itself.")
    if ctx["time_between_s"] > 1800:
        warns.append(f"The scans are {ctx['time_between_s'] / 3600:.1f} h apart; broadcast and interference levels "
                     "change over time.")
    if (c.get("measurable_fraction") or 0) < 0.2:
        frac = (c.get("measurable_fraction") or 0) * 100
        warns.append(f"Only {'<1' if frac < 1 else f'{frac:.0f}'}% of the range had signal above the "
                     "receiver's own noise floor in the baseline. Where there is only receiver noise, a filter or "
                     "antenna change cannot show any effect.")
    if any(d["setting"] == "Range (MHz)" for d in ctx["settings_differ"]):
        warns.append("The scans cover different ranges; only the overlap is compared.")
    for reg in regions:
        if not any(r["name"] == reg["name"] for r in c["regions"]):
            warns.append(f"{reg['name']} is outside the range both scans cover, so it was not compared.")
    for r in c["regions"]:
        if r["median_change_db"] is None:
            facts.append(f"{r['name']} ({r['start_hz'] / 1e6:g}-{r['stop_hz'] / 1e6:g} MHz): no signal above the "
                         "baseline's noise floor, so no change can be measured there.")
        else:
            facts.append(f"{r['name']}: median change {r['median_change_db']:+.1f} dB over {r['measurable_bins']} "
                         f"bins that had signal; {(r['fraction_reduced_3db'] or 0) * 100:.0f}% of them dropped by 3 dB or more.")
    op = c["outside_regions"]
    if op["median_change_db"] is not None:
        facts.append(f"Everywhere else: median change {op['median_change_db']:+.1f} dB over {op['measurable_bins']} bins with signal.")
    facts.append(f"Noise floor estimate moved by {c['floor_shift_db']:+.1f} dB (median).")
    facts.append(f"{c['carriers_reduced']} of {len(c['carriers'])} baseline carriers dropped by 3 dB or more; "
                 f"{c['carriers_increased']} rose by 3 dB or more.")
    for r in c["regions"]:
        rc, oc = r["median_change_db"], op["median_change_db"]
        if rc is None or oc is None:
            continue
        if r.get("role", "stop") == "stop" and rc <= -3 and abs(oc) <= 1.5:
            interp.append(f"The drop is concentrated in {r['name']} while the rest stayed within 1.5 dB: consistent "
                          "with selective filtering of that region.")
        elif rc <= -3 and oc <= -3:
            interp.append("Power dropped both inside and outside the region: that looks like general loss (attenuation, "
                          "a cable or connector, or less gain) rather than selective filtering, and it weakens wanted "
                          "signals too.")
        if r.get("role") == "watch" and rc <= -2:
            interp.append(f"Signals in {r['name']} (a region you want to keep) dropped by {-rc:.1f} dB: an undesirable "
                          "loss unless the noise dropped by more.")
    hypo.append("A lower measured power does not by itself mean better reception; it can also be lost wanted signal.")
    hypo.append("This is an over-the-air comparison, not a calibrated network-analyzer measurement.")
    return {"measured": facts, "interpretations": interp, "hypotheses": hypo, "warnings": warns}


def _filter_confidence(resp: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, list[str]]:
    why = []
    if any(d["setting"] == "Receiver" for d in ctx["settings_differ"]):
        why.append("different receivers")
    if any(d["setting"] in ("Tuner gain (dB)", "Gain mode", "IFGR", "RFGR", "AGC") for d in ctx["settings_differ"]):
        why.append("different gain")
    if ctx["time_between_s"] > 1800:
        why.append(f"{ctx['time_between_s'] / 3600:.1f} h between scans")
    if resp["stopband_measurable_bins"] < 50:
        why.append(f"only {resp['stopband_measurable_bins']} stopband bins had signal to attenuate")
    if resp["passband_measurable_bins"] < 50:
        why.append(f"only {resp['passband_measurable_bins']} passband bins had signal")
    pc = resp.get("passband_median_change_db")
    if pc is not None and abs(pc) > 2:
        why.append(f"passband moved {pc:+.1f} dB, so conditions changed as well as the filter")
    return ("high" if not why else "medium" if len(why) == 1 else "low"), why


def _anomaly_row(a: dict[str, Any]) -> str:
    e = html.escape
    where = []
    if a.get("frequency_hz"):
        where.append(f"{a['frequency_hz'] / 1e6:.5f} MHz")
    if a.get("frequency_range_hz"):
        lo, hi = a["frequency_range_hz"]
        where.append(f"{lo / 1e6:.4f}–{hi / 1e6:.4f} MHz")
    if a.get("time_s"):
        where.append(f"{a['time_s'][0]}–{a['time_s'][1]} s")
    return (f"<tr><th>{e(a['title'])}</th><td>{e(' · '.join(where))}{' · ' if where else ''}{e(a['evidence'])} · "
            f"<b>{e(a['severity'])}</b> / {e(a['confidence'])} confidence"
            f"<br><span class='m'>possibly: {e('; '.join(a['explanations']))}</span></td></tr>")


def _sat_compare_report(data: dict[str, Any], side: dict[str, Any], ids: list[str]) -> dict[str, Any]:
    ser = data["series"]
    facts, interp, warns = [], [], []
    for rid in ids:
        sr, sd = ser[rid], side[rid]
        dec = sd["decode"]
        sat = dec.get("sat") or {}
        offs = [o for o in sr["offset_hz"] if o is not None]
        facts.append(f"{sd['receiver_id']} ({rid}): target stood a median {sr['contrast_median_db']} dB above its own noise "
                     f"floor (90th percentile {sr['contrast_p90_db']} dB); {sr['seconds_above_3db']} s above 3 dB"
                     + (f"; median offset {int(np.median(offs))} Hz" if offs else "") + ".")
        if dec.get("status") == "completed":
            facts.append(f"{sd['receiver_id']} decode: {dec.get('cadu_frames') or 0} CADU frames, "
                         f"{sat.get('image_lines_max') if sat.get('image_lines_max') is not None else 'lines not reported'} image lines, "
                         f"Viterbi sync {'not reported' if sat.get('viterbi_sync_fraction') is None else f'{sat['viterbi_sync_fraction'] * 100:.0f}%'}.")
        else:
            facts.append(f"{sd['receiver_id']} decode: {dec.get('status') or 'not run'}.")
        if not sd["path_recorded"]:
            warns.append(f"{rid} was recorded before signal paths were tracked; its antenna path is unknown from the record.")
    a, b = ids
    ra, rb = side[a]["receiver_id"], side[b]["receiver_id"]
    warns.insert(0, f"{ra} and {rb} differ in ADC format, gain, antenna, filters and front end. Only relative measures "
                    "(contrast over each one's own noise floor, offset, decoder outcome) are compared; absolute dBFS is not.")
    ca, cb = ser[a]["contrast_median_db"], ser[b]["contrast_median_db"]
    fa, fb = (side[a]["decode"].get("cadu_frames") or 0), (side[b]["decode"].get("cadu_frames") or 0)
    if ca is not None and cb is not None and abs(ca - cb) >= 3:
        hi, lo = (ra, rb) if ca > cb else (rb, ra)
        interp.append(f"The target stood further above the noise in {hi}'s recording than in {lo}'s over the shared "
                      "part of the pass.")
    if (fa > 0) != (fb > 0):
        good, bad = (ra, rb) if fa > 0 else (rb, ra)
        cbad = cb if fb == 0 else ca
        interp.append(f"{good} decoded frames and {bad} did not" + (
            f", although {bad}'s recording still showed the target {cbad} dB above its noise floor: its failure is not "
            "explained by the absence of a signal (frequency, Doppler, timing, level or IQ handling remain possible causes)."
            if cbad is not None and cbad >= 3 else "."))
    return {"measured": facts, "interpretations": interp, "warnings": warns,
            "hypotheses": ["Different decode outcomes do not by themselves show which receiver is more sensitive.",
                           "A failed decode never proves the satellite signal was absent."]}


TEST_LABEL = {"quick": "Quick Signal Test", "scan": "Wideband RF Scan", "interference": "Investigate Interference",
              "gain_sweep": "Gain sweep", "satellite": "Satellite Quality Test"}


def _lowest_rate(caps: dict[str, Any]) -> int | None:
    rates = list(caps.get("sample_rates_hz") or []) + [lo for lo, *_ in (caps.get("sample_rate_ranges_hz") or [])]
    good = [r for r in rates if r >= 1_000_000] or rates
    return int(min(good)) if good else None


def elements_defaults(rx: Any, last: dict[str, Any] | None) -> dict[str, Any]:
    """Defaults for a SoapySDR receiver, from its driver-reported capabilities (never assumed)."""
    caps = rx.caps
    gains = caps.get("gains") or {}
    lo, hi = caps.get("frequency_hz") or (None, None)
    out: dict[str, Any] = {"gain_mode": "manual", "agc": False, "bandwidth_hz": None, "gain_db": None}
    prev = (last or {}).get("gain_elements") or {}
    out["gain_elements"] = {n: prev.get(n, round((g["min"] + g["max"]) / 2)) for n, g in gains.items()}
    if not last:
        out["sample_rate_hz"] = _lowest_rate(caps)
        out["duration_s"] = 10
        if lo is not None and not lo <= 137_900_000 <= hi:
            out["frequency_hz"] = int(lo + (hi - lo) // 2)
        out["source"] = f"{rx.id}'s driver-reported capabilities"
    return out


def normalize_elements(rx: Any, body: dict[str, Any], rate: int) -> dict[str, Any]:
    from .hardware import rate_supported
    caps = rx.caps
    if not caps:
        raise SchedulerError(f"{rx.id} has not been identified yet (Receivers → Identify), so its gains and sample "
                             "rates are unknown", 400)
    if not rate_supported(caps, rate):
        raise SchedulerError(f"{rate} S/s is not a sample rate {rx.id}'s driver offers", 400)
    gains = caps.get("gains") or {}
    given = body.get("gain_elements") or {}
    out = {}
    for name, g in gains.items():
        v = given.get(name)
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise SchedulerError(f"gain element {name} needs a value ({g['min']:g}-{g['max']:g} dB)", 400) from None
        if not g["min"] <= v <= g["max"]:
            raise SchedulerError(f"gain element {name} must be {g['min']:g}-{g['max']:g} dB", 400)
        out[name] = int(v) if v.is_integer() else v
    bw = body.get("bandwidth_hz")
    return {"gain_elements": out, "bandwidth_hz": int(bw) if bw else None}
