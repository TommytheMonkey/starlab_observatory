"""Recording readiness verification.

Each check returns PASS / WARN / FAIL / NA with evidence. The verdict is READY
only when every applicable check is PASS *and* the pass lies inside the
recorder's own forecast horizon (so the recorder itself has scheduled it and
we can confirm agreement). Anything unverifiable is never reported as PASS.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import shutil
import socket
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from ..config import Config
from ..hardware import antsw, rtlsdr
from ..legacy import Legacy, RecorderIntent, latest_recorder_intent
from ..timeutil import iso, utc_now
from . import READY, SCHEDULED, WARNING
from .forecast import Forecast, PlannedPass

PASS, WARN, FAIL, NA = "PASS", "WARN", "FAIL", "NA"
GIB = 1024**3


@dataclass
class Check:
    name: str
    status: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Readiness:
    pass_id: str
    verdict: str
    checked_at: dt.datetime
    checks: list[Check]
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"pass_id": self.pass_id, "verdict": self.verdict, "checked_at": iso(self.checked_at),
                "reason": self.reason, "checks": [asdict(c) for c in self.checks]}


@dataclass
class SystemProbe:
    """Host-level facts gathered once per readiness cycle (shared by all passes)."""

    hostname: str
    rtl_devices: list[rtlsdr.UsbRtl]
    index_map: list[str] | None
    index_map_error: str | None
    service: dict[str, str]
    recorder_intent: RecorderIntent | None
    leases: dict[str, Any]
    holders: list[rtlsdr.Holder]
    unreadable_procs: int
    disk_free: int
    antsw_state: dict[str, Any] | None
    antsw_error: str | None
    recorder_capturing: bool


def _systemctl_show(unit: str) -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "show", unit, "-p", "ActiveState,SubState,NRestarts,ExecMainStartTimestamp,UnitFileState,Result"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def gather(config: Config, legacy: Legacy) -> SystemProbe:
    receiver = str(config.section("forecast")["receiver"])
    unit = config.section("legacy")["recorder_unit"].format(receiver=receiver)
    devices = rtlsdr.list_rtl_devices()
    try:
        index_map, index_err = rtlsdr.librtlsdr_index_map(), None
    except Exception as exc:  # noqa: BLE001 - evidence, not control flow
        index_map, index_err = None, str(exc)
    state_path, _lock = legacy.coordinator.lease_paths(legacy.station)
    leases = legacy.coordinator.load_state(state_path)
    legacy.coordinator.clean_leases(leases)
    dev = next((d for d in devices if d.serial == receiver), None)
    holders, unreadable = rtlsdr.usb_holders(dev.dev_node) if dev else ([], 0)
    sw = config.section("antenna_switch")
    try:
        sw_state, sw_err = antsw.get_state(sw["base_url"]), None
    except Exception as exc:  # noqa: BLE001
        sw_state, sw_err = None, str(exc)
    capturing = any(h.comm == "rtl_sdr" and f"-d {receiver}" in h.cmdline for h in holders)
    return SystemProbe(
        hostname=socket.gethostname(),
        rtl_devices=devices,
        index_map=index_map,
        index_map_error=index_err,
        service=_systemctl_show(unit) | {"unit": unit},
        recorder_intent=latest_recorder_intent(unit),
        leases=leases.get("leases", {}),
        holders=holders,
        unreadable_procs=unreadable,
        disk_free=shutil.disk_usage(legacy.recorder["storage_root"]).free,
        antsw_state=sw_state,
        antsw_error=sw_err,
        recorder_capturing=capturing,
    )


def recorder_tle_epoch(legacy: Legacy, receiver: str, norad: int) -> dt.datetime | None:
    path = pathlib.Path(legacy.recorder["storage_root"]) / "pass-cache" / receiver / "observatory.tle"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("1 ") and line[2:7].strip() == str(norad):
            yy, doy = int(line[18:20]), float(line[20:32])
            base = dt.datetime(2000 + yy if yy < 57 else 1900 + yy, 1, 1, tzinfo=dt.timezone.utc)
            return base + dt.timedelta(days=doy - 1)
    return None


# ------------------------------------------------------------------ checks
def check_host(cfg: Config, p: PlannedPass, probe: SystemProbe, **_: Any) -> Check:
    if probe.hostname == p.host:
        return Check("host_reachable", PASS, f"Recorder host {p.host} is this machine")
    return Check("host_reachable", FAIL, f"Running on {probe.hostname}; remote recorder hosts are not yet supported")


def check_receiver(cfg: Config, p: PlannedPass, probe: SystemProbe, **_: Any) -> Check:
    matches = [d for d in probe.rtl_devices if d.serial == p.receiver]
    ev = {"connected_serials": [d.serial for d in probe.rtl_devices]}
    if len(matches) == 1:
        d = matches[0]
        return Check("receiver_present", PASS, f"{p.receiver} on USB {d.dev_node} ({d.manufacturer} {d.product})", ev)
    if not matches:
        return Check("receiver_present", FAIL, f"{p.receiver} is not connected", ev)
    return Check("receiver_present", FAIL, f"{len(matches)} devices share serial {p.receiver}", ev)


def check_service(cfg: Config, p: PlannedPass, probe: SystemProbe, **_: Any) -> Check:
    s = probe.service
    ev = {k: s.get(k) for k in ("unit", "ActiveState", "SubState", "NRestarts", "UnitFileState", "ExecMainStartTimestamp")}
    if s.get("ActiveState") != "active" or s.get("SubState") != "running":
        return Check("recording_service_active", FAIL, f"{s.get('unit')} is {s.get('ActiveState')}/{s.get('SubState')}", ev)
    if s.get("UnitFileState") not in ("enabled", "static", "indirect", "enabled-runtime"):
        return Check("recording_service_active", WARN, f"{s.get('unit')} running but not enabled at boot ({s.get('UnitFileState')})", ev)
    return Check("recording_service_active", PASS, f"{s.get('unit')} active/running", ev)


def check_prediction(cfg: Config, p: PlannedPass, probe: SystemProbe, forecast: Forecast, now: dt.datetime, legacy: Legacy, **_: Any) -> Check:
    fc = cfg.section("forecast")
    age_h = (now - p.tle_epoch).total_seconds() / 3600
    ev = {"tle_epoch": iso(p.tle_epoch), "tle_age_hours": round(age_h, 1), "predicted_at": iso(p.predicted_at)}
    if age_h > float(fc.get("max_tle_age_hours_warn", 336)):
        return Check("valid_prediction", FAIL, f"TLE epoch is {age_h:.0f} h old", ev)
    if age_h > float(fc.get("max_tle_age_hours_ok", 72)):
        return Check("valid_prediction", WARN, f"TLE epoch is {age_h:.0f} h old", ev)
    if (now - forecast.generated_at).total_seconds() > 3600:
        return Check("valid_prediction", WARN, "Forecast is more than 1 h old", ev)
    rec_epoch = recorder_tle_epoch(legacy, p.receiver, p.norad_id)
    ev["recorder_tle_epoch"] = iso(rec_epoch)
    if rec_epoch is None:
        return Check("valid_prediction", WARN, "Recorder's own TLE cache has no elements for this satellite", ev)
    if abs((rec_epoch - p.tle_epoch).total_seconds()) > 36 * 3600:
        return Check("valid_prediction", WARN, "Our TLE epoch differs from the recorder's cache by more than 36 h", ev)
    return Check("valid_prediction", PASS, f"TLE epoch {age_h:.1f} h old; pass predicted by the recorder's own predictor", ev)


def check_recorder_agreement(cfg: Config, p: PlannedPass, probe: SystemProbe, forecast: Forecast, now: dt.datetime, next_capture_start: dt.datetime | None, **_: Any) -> Check:
    """Confirm against the running recorder's journal that it plans this exact window.

    Only the recorder's *next* window is visible in its log; later passes are NA."""
    tol = float(cfg.section("readiness").get("recorder_agreement_tolerance_s", 90))
    if p.capture_start != next_capture_start:
        return Check("recorder_agreement", NA, "Recorder only announces its next window; later passes cannot be cross-checked yet")
    intent = probe.recorder_intent
    if probe.recorder_capturing and p.capture_start <= now:
        return Check("recorder_agreement", PASS, "Recorder is capturing this window now")
    if intent is None:
        return Check("recorder_agreement", WARN, "No 'Waiting for ...' line in the recorder journal to cross-check")
    delta = abs((intent.record_start - p.capture_start).total_seconds())
    ev = {"recorder_start": iso(intent.record_start), "our_start": iso(p.capture_start), "delta_s": round(delta, 1),
          "recorder_satellites": intent.satellites, "logged_at": intent.logged_at_local}
    if delta <= tol and p.satellite_name in intent.satellites:
        return Check("recorder_agreement", PASS, f"Recorder plans {intent.satellites} at the same time (Δ {delta:.1f} s)", ev)
    return Check("recorder_agreement", FAIL, f"Recorder plans {intent.satellites} at {iso(intent.record_start)} (Δ {delta:.0f} s)", ev)


def check_storage(cfg: Config, p: PlannedPass, probe: SystemProbe, legacy: Legacy, **_: Any) -> Check:
    floor = int(float(legacy.recorder["min_free_gib"]) * GIB)
    headroom = int(float(cfg.section("readiness").get("min_extra_free_gib", 10)) * GIB)
    need = p.expected_bytes
    ev = {"free_gib": round(probe.disk_free / GIB, 1), "recorder_min_free_gib": legacy.recorder["min_free_gib"],
          "pass_needs_gib": round(need / GIB, 2)}
    if probe.disk_free - need < floor:
        return Check("storage", FAIL, "Recorder would refuse to record (free space below its min_free_gib after this pass)", ev)
    if probe.disk_free - need < floor + headroom:
        return Check("storage", WARN, "Free space close to the recorder's floor", ev)
    return Check("storage", PASS, f"{probe.disk_free / GIB:.0f} GiB free; pass needs {need / GIB:.1f} GiB", ev)


def check_assignment(cfg: Config, p: PlannedPass, probe: SystemProbe, legacy: Legacy, **_: Any) -> Check:
    rec = legacy.recorder["receivers"].get(p.receiver, {})
    job = next((j for j in legacy.station.get("jobs", []) if int(j["norad_id"]) == p.norad_id and j.get("receiver") == p.receiver), None)
    station_rx = next((r for r in legacy.station.get("receivers", []) if r.get("id") == p.receiver), None)
    problems = []
    if p.norad_id not in [int(n) for n in rec.get("pass_targets", [])]:
        problems.append("NORAD id not in recorder pass_targets")
    if job is None:
        problems.append("no station job assigns this satellite to the receiver")
    if station_rx is None or station_rx.get("serial") != p.receiver:
        problems.append("receiver id/serial mismatch in coordinator config")
    elif rec.get("capability") not in station_rx.get("capabilities", []):
        problems.append(f"receiver lacks capability {rec.get('capability')}")
    if problems:
        return Check("receiver_assignment", FAIL, "; ".join(problems))
    note = "" if (job or {}).get("enabled", True) else " (station job disabled, but PassSchedule ignores 'enabled')"
    return Check("receiver_assignment", PASS, f"{p.satellite_name} → {p.receiver} (serial-selected, rtl_sdr -d {p.receiver}){note}")


def check_antenna(cfg: Config, p: PlannedPass, probe: SystemProbe, **_: Any) -> Check:
    antennas = cfg.raw.get("antennas", {})
    ant_id, ant = next(((k, v) for k, v in antennas.items() if v.get("hardwired_receiver") == p.receiver), (None, None))
    if ant is None:
        return Check("antenna_configuration", FAIL, f"No antenna configured for {p.receiver}")
    lo, hi = p.center_frequency_hz - p.sample_rate // 2, p.center_frequency_hz + p.sample_rate // 2
    ev = {"antenna": ant_id, "capture_band_hz": [lo, hi], "antenna_range_hz": [ant["min_hz"], ant["max_hz"]]}
    if not (ant["min_hz"] <= p.center_frequency_hz <= ant["max_hz"]):
        return Check("antenna_configuration", FAIL, f"{ant_id} does not cover {p.center_frequency_hz / 1e6:.3f} MHz", ev)
    if probe.antsw_state is not None:
        ev["antsw_feeds"] = probe.antsw_state.get("receiver")
        if probe.antsw_state.get("receiver") == p.receiver:
            return Check("antenna_configuration", WARN, f"ANTSW-01 reports feeding {p.receiver}; port {probe.antsw_state.get('port')} unverified for 137 MHz", ev)
    else:
        ev["antsw_error"] = probe.antsw_error
    return Check("antenna_configuration", PASS,
                 f"{ant['description']} hard-wired to {p.receiver} (configuration; RF path is not electronically verifiable); not routed via ANTSW-01", ev)


def check_destination(cfg: Config, p: PlannedPass, probe: SystemProbe, legacy: Legacy, **_: Any) -> Check:
    root = pathlib.Path(legacy.recorder["storage_root"])
    target = root / p.receiver
    ev = {"path": str(target)}
    if not os.path.ismount("/data"):
        return Check("destination_writable", FAIL, "/data is not mounted (recorder refuses to start)", ev)
    if target.is_symlink():
        return Check("destination_writable", FAIL, "Recording directory is a symlink (recorder refuses)", ev)
    if not target.is_dir():
        return Check("destination_writable", WARN, "Recording directory does not exist yet (recorder creates it)", ev)
    st = os.statvfs(target)
    if st.f_flag & os.ST_RDONLY:
        return Check("destination_writable", FAIL, "Filesystem is mounted read-only", ev)
    if not os.access(target, os.W_OK | os.X_OK):
        return Check("destination_writable", FAIL, "Directory not writable by this user (the recorder's user)", ev)
    return Check("destination_writable", PASS, f"{target} writable on mounted /data", ev)


def check_conflicts(cfg: Config, p: PlannedPass, probe: SystemProbe, now: dt.datetime, **_: Any) -> Check:
    leg = cfg.section("legacy")
    imminent = dt.timedelta(minutes=float(cfg.section("readiness").get("conflict_imminent_minutes", 30)))
    problems: list[tuple[str, str]] = []
    ev: dict[str, Any] = {}

    lease = probe.leases.get(p.receiver)
    own = f"observatory:passes:{p.receiver}"
    if lease and lease.get("owner") != own:
        ev["lease"] = {k: lease.get(k) for k in ("owner", "priority", "expires_at", "purpose")}
        problems.append((FAIL, f"Coordinator lease on {p.receiver} held by {lease.get('owner')} (recorder reserves with preempt=False and will fail)"))

    foreign = [h for h in probe.holders if not (h.comm == "rtl_sdr" and f"-d {p.receiver}" in h.cmdline)]
    if foreign:
        ev["usb_holders"] = [asdict(h) for h in foreign]
        sev = FAIL if p.capture_start - now <= imminent else WARN
        problems.append((sev, f"{p.receiver} USB device held by {', '.join(f'{h.comm}[{h.pid}]' for h in foreign)}"))
    ev["uninspectable_processes"] = probe.unreadable_procs

    # dump1090 opens --device-index 0. If index 0 ever stops being OBS-R05, a restart of
    # observatory-adsb.service would seize another receiver.
    idx = int(leg.get("adsb_device_index", 0))
    if probe.index_map is None:
        problems.append((WARN, f"Could not read librtlsdr index map: {probe.index_map_error}"))
    else:
        ev["librtlsdr_index_map"] = probe.index_map
        actual = probe.index_map[idx] if idx < len(probe.index_map) else None
        if actual != leg.get("adsb_serial"):
            sev = FAIL if actual == p.receiver else WARN
            problems.append((sev, f"ADS-B uses --device-index {idx}, which now resolves to {actual}, not {leg.get('adsb_serial')}"))

    if not problems:
        return Check("no_scheduling_conflict", PASS,
                     f"No foreign lease or visible USB holder on {p.receiver}; ADS-B index {idx} = {leg.get('adsb_serial')}"
                     + (f" ({probe.unreadable_procs} root processes not inspectable)" if probe.unreadable_procs else ""), ev)
    worst = FAIL if any(s == FAIL for s, _ in problems) else WARN
    return Check("no_scheduling_conflict", worst, "; ".join(m for _, m in problems), ev)


CHECKS: list[Callable[..., Check]] = [
    check_host, check_receiver, check_service, check_prediction, check_recorder_agreement,
    check_storage, check_assignment, check_antenna, check_destination, check_conflicts,
]


def evaluate(config: Config, legacy: Legacy, forecast: Forecast, probe: SystemProbe, now: dt.datetime | None = None) -> dict[str, Readiness]:
    now = now or utc_now()
    upcoming = [p for p in forecast.passes if p.capture_end > now]
    next_capture_start = min((p.capture_start for p in upcoming), default=None)
    horizon_end = now + dt.timedelta(hours=forecast.recorder_horizon_hours)
    results: dict[str, Readiness] = {}
    for p in upcoming:
        checks = [fn(config, p, probe, legacy=legacy, forecast=forecast, now=now, next_capture_start=next_capture_start) for fn in CHECKS]
        failed = [c for c in checks if c.status in (FAIL, WARN)]
        if failed:
            verdict, reason = WARNING, "; ".join(f"{c.name}: {c.detail}" for c in failed)
        elif p.record_start > horizon_end:
            verdict = SCHEDULED
            reason = (f"All current checks pass, but the pass is beyond the recorder's {forecast.recorder_horizon_hours:.0f} h "
                      "scheduling horizon; READY is only asserted once the recorder itself has it in its forecast")
        else:
            verdict, reason = READY, "All readiness checks passed"
        results[p.pass_id] = Readiness(p.pass_id, verdict, now, checks, reason)
    return results
