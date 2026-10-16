"""aobs command line."""

from __future__ import annotations

import argparse
import os
import json
import re
import logging
import pathlib
import signal
import sys
import threading
from typing import Any

from .config import Config, load_config
from .logjson import log_event, setup_logging
from .timeutil import fmt_local, iso, utc_now

LOG = logging.getLogger("aobs")


def _ctx(args: argparse.Namespace) -> tuple[Config, Any]:
    from .legacy import load_legacy

    config = load_config(args.config)
    return config, load_legacy(config)


def _engine(config: Config) -> Any:
    from .db import init_db, make_engine
    from .devtools import assert_dev_safe
    assert_dev_safe(config)                     # dev environment: never open a database outside .dev/

    engine = make_engine(config.database_url)
    init_db(engine)
    return engine


# ----------------------------------------------------------------- commands
def cmd_inventory(args: argparse.Namespace) -> int:
    from .hardware import antsw, rtlsdr

    config, legacy = _ctx(args)
    out: dict[str, Any] = {"rtl_devices": [], "librtlsdr_index_map": None}
    for d in rtlsdr.list_rtl_devices():
        holders, unreadable = rtlsdr.usb_holders(d.dev_node)
        out["rtl_devices"].append({"serial": d.serial, "product": f"{d.manufacturer} {d.product}", "usb": d.dev_node,
                                   "visible_holders": [f"{h.comm}[{h.pid}]" for h in holders]})
    try:
        out["librtlsdr_index_map"] = rtlsdr.librtlsdr_index_map()
    except Exception as exc:  # noqa: BLE001
        out["librtlsdr_index_map"] = f"error: {exc}"
    try:
        s = antsw.get_state(config.section("antenna_switch")["base_url"])
        out["antenna_switch"] = {k: s.get(k) for k in ("switch_id", "receiver", "port", "antenna", "healthy", "lease", "firmware")}
    except Exception as exc:  # noqa: BLE001
        out["antenna_switch"] = f"unreachable: {exc}"
    state_path, _ = legacy.coordinator.lease_paths(legacy.station)
    leases = legacy.coordinator.load_state(state_path)
    legacy.coordinator.clean_leases(leases)
    out["coordinator_leases"] = leases.get("leases", {})
    print(json.dumps(out, indent=2))
    return 0


def _print_schedule(passes: list[Any], readiness: dict[str, Any], tz: str, recorder_horizon: float) -> None:
    print(f"{'Local start (' + tz + ')':<26} {'Satellite':<12} {'NORAD':>5} {'MaxEl':>6} {'Quality':<9} "
          f"{'AOS→LOS (0°)':<19} {'Capture':>8} {'Seg':<9} {'Readiness':<9}")
    print("-" * 112)
    for p in passes:
        r = readiness.get(p.pass_id)
        aos = fmt_local(p.aos, tz, "%H:%M") if p.aos else "?"
        los = fmt_local(p.los, tz, "%H:%M") if p.los else "?"
        segs = "+".join(str(s.seconds) for s in p.capture_segments)
        print(f"{fmt_local(p.capture_start, tz, '%a %m-%d %H:%M:%S'):<26} {p.satellite_name:<12} {p.norad_id:>5} "
              f"{p.max_elevation_deg:>5.1f}° {p.predicted_quality:<9} {aos + '→' + los:<19} "
              f"{p.capture_seconds // 60:>3}m{p.capture_seconds % 60:02d}s {segs:<9} {r.verdict if r else '-':<9}")
    print(f"\nREADY is only asserted inside the recorder's own {recorder_horizon:.0f} h scheduling horizon; "
          "later passes show SCHEDULED until then.")


def cmd_forecast(args: argparse.Namespace) -> int:
    from .satellites.forecast import build_forecast
    from .satellites.readiness import evaluate, gather

    config, legacy = _ctx(args)
    if args.hours:
        config.raw["forecast"]["horizon_hours"] = args.hours
    forecast = build_forecast(config, legacy)
    readiness = evaluate(config, legacy, forecast, gather(config, legacy)) if not args.no_readiness else {}
    if args.json:
        print(json.dumps({"generated_at": iso(forecast.generated_at), "horizon_hours": forecast.horizon_hours,
                          "tle_epochs": {k: iso(v) for k, v in forecast.tle_epochs.items()},
                          "passes": [{"pass_id": p.pass_id, "satellite": p.satellite_name, "norad_id": p.norad_id,
                                      "aos": iso(p.aos), "los": iso(p.los), "culmination": iso(p.culmination),
                                      "max_elevation_deg": round(p.max_elevation_deg, 1),
                                      "capture_start": iso(p.capture_start), "capture_end": iso(p.capture_end),
                                      "capture_seconds": p.capture_seconds, "quality": p.predicted_quality,
                                      "readiness": readiness[p.pass_id].to_json() if p.pass_id in readiness else None}
                                     for p in forecast.passes]}, indent=2))
        return 0
    tz = config.display_tz
    print(f"METEOR forecast for {forecast.receiver} · generated {fmt_local(forecast.generated_at, tz)} · "
          f"horizon {forecast.horizon_hours:.0f} h · {len(forecast.passes)} recording windows")
    print("TLE epochs: " + ", ".join(f"{k} {fmt_local(v, tz, '%m-%d %H:%M %Z')}" for k, v in forecast.tle_epochs.items()))
    print()
    _print_schedule(forecast.passes, readiness, tz, forecast.recorder_horizon_hours)
    return 0


def cmd_readiness(args: argparse.Namespace) -> int:
    from .satellites.forecast import build_forecast
    from .satellites.readiness import evaluate, gather

    config, legacy = _ctx(args)
    forecast = build_forecast(config, legacy)
    results = evaluate(config, legacy, forecast, gather(config, legacy))
    passes = forecast.passes[: args.limit]
    for p in passes:
        r = results.get(p.pass_id)
        if not r:
            continue
        print(f"\n{p.satellite_name} {p.pass_id} · capture {fmt_local(p.capture_start, config.display_tz)} · "
              f"max {p.max_elevation_deg:.1f}° → {r.verdict}")
        print(f"  reason: {r.reason}")
        for c in r.checks:
            print(f"  [{c.status:<4}] {c.name:<25} {c.detail}")
    return 0


def cmd_sync_once(args: argparse.Namespace) -> int:
    from .satellites.service import run_cycle

    config, legacy = _ctx(args)
    engine = _engine(config)
    result = run_cycle(config, legacy, engine)
    print(f"cycle ok: {len(result.forecast.passes)} passes, {len(result.transitions)} status changes, "
          f"{len(result.removed)} removed from forecast")
    for pid, old, new, reason in result.transitions:
        print(f"  {pid}: {old} -> {new} ({reason[:140]})")
    if args.calendar:
        _calendar_cycle(config, engine, result, dry_run=args.calendar == "dry-run")
    return 0


def calendar_gate(config: Config, result: Any) -> tuple[bool, list[str]]:
    """Every precondition for writing to Google Calendar. Returns (ok, problems)."""
    from .satellites.readiness import PASS

    cal = config.section("calendar")
    problems = []
    if not cal.get("enabled"):
        problems.append("calendar.enabled is false")
    if not cal.get("calendar_id"):
        problems.append("calendar.calendar_id is not set (run `aobs calendar setup`)")
    if cal.get("satops_publish", True) is False:
        problems.append("calendar.satops_publish is false: the broker publishes the unified schedule")
    if cal.get("auth_mode", "service_account") == "service_account":
        if not pathlib.Path(cal["service_account_file"]).is_file():
            problems.append("service account key file missing")
    elif not pathlib.Path(cal["token_file"]).is_file():
        problems.append("no OAuth token (run `aobs calendar auth`)")
    if result is not None:
        nxt = next((p for p in result.forecast.passes if p.pass_id in result.readiness), None)
        if nxt is not None:
            agreement = next(c for c in result.readiness[nxt.pass_id].checks if c.name == "recorder_agreement")
            if agreement.status != PASS:
                problems.append(f"forecast not verified against the running recorder: {agreement.detail}")
    return (not problems, problems)


def _calendar_cycle(config: Config, engine: Any, result: Any, dry_run: bool) -> None:
    from .calendar import sync

    cal = config.section("calendar")
    ok, problems = calendar_gate(config, result)
    calendar_id = cal.get("calendar_id") or "(unset)"
    actions = sync.plan(config, engine, calendar_id)
    summary = {k: sum(1 for a in actions if a.kind == k) for k in ("create", "update", "noop")}
    if dry_run or not ok:
        if not dry_run:
            log_event(LOG, "calendar sync skipped: gate closed", logging.WARNING, problems=problems)
        print(f"calendar plan ({'dry-run' if dry_run else 'NOT APPLIED'}): {summary}")
        for p in problems:
            print(f"  gate: {p}")
        return
    from .calendar.google_api import CalendarClient, credentials_from_config

    client = CalendarClient(credentials_from_config(cal))
    info = client.get_calendar(calendar_id)  # raises if the target calendar is not accessible
    if info.get("accessRole") not in ("writer", "owner"):
        raise RuntimeError(f"Calendar access role is {info.get('accessRole')}; need 'Make changes to events'")
    counts = sync.apply(engine, client, calendar_id, actions)
    print(f"calendar sync: {counts}")


def cmd_calendar(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    cal = config.section("calendar")
    from .calendar import google_api

    if args.calendar_cmd == "auth":
        google_api.run_auth_flow(pathlib.Path(cal["client_secret_file"]), pathlib.Path(cal["token_file"]), args.port)
        print(f"Token stored at {cal['token_file']} (mode 600).")
        return 0
    if args.calendar_cmd == "setup":
        creds = google_api.credentials_from_config(cal)
        client = google_api.CalendarClient(creds)
        if args.calendar_id or cal.get("calendar_id"):
            info = client.get_calendar(args.calendar_id or cal["calendar_id"])
            print(f"Calendar accessible: '{info.get('summary')}' role={info.get('accessRole')} tz={info.get('timeZone')}")
            if info.get("summary") != cal["calendar_name"]:
                print(f"WARNING: title differs from configured calendar_name '{cal['calendar_name']}'")
            return 0 if info.get("accessRole") in ("writer", "owner") else 2
        if cal.get("auth_mode", "service_account") == "service_account":
            print(f"Create a calendar named '{cal['calendar_name']}' in Google Calendar, share it with "
                  f"{getattr(creds, 'service_account_email', '?')} ('Make changes to events'), then run "
                  "`aobs calendar setup --calendar-id <id>`.")
            return 2
        if not args.create:
            print("calendar_id is empty. The app's scope (calendar.app.created) only sees calendars it creates.\n"
                  f"Re-run with --create to create '{cal['calendar_name']}', then put its id in the config.")
            return 2
        info = client.create_calendar(cal["calendar_name"], config.display_tz)
        print(f"Created calendar '{info['summary']}'. Set calendar.calendar_id = \"{info['id']}\" in {config.path}")
        return 0
    if args.calendar_cmd in ("plan", "sync"):
        from .satellites.service import run_cycle

        cfg, legacy = _ctx(args)
        engine = _engine(cfg)
        result = run_cycle(cfg, legacy, engine)
        _calendar_cycle(cfg, engine, result, dry_run=args.calendar_cmd == "plan")
        if args.calendar_cmd == "plan" and args.show:
            from .calendar import sync
            for a in sync.plan(cfg, engine, cal.get("calendar_id") or "(unset)")[: args.show]:
                print(f"\n=== {a.kind.upper()} {a.event_id}\n{a.body['summary']}\n"
                      f"{a.body['start']['dateTime']} → {a.body['end']['dateTime']}\n{a.body['description']}")
        return 0
    return 2


def cmd_run(args: argparse.Namespace) -> int:
    from .satellites.service import run_cycle

    config, legacy = _ctx(args)
    engine = _engine(config)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    failures = 0
    while not stop.is_set():
        try:
            result = run_cycle(config, legacy, engine)
            log_event(LOG, "satops cycle complete", passes=len(result.forecast.passes),
                      transitions=len(result.transitions), removed=len(result.removed))
            if config.section("calendar").get("enabled"):
                _calendar_cycle(config, engine, result, dry_run=False)
            failures = 0
            delay = args.interval
        except Exception:  # noqa: BLE001 - keep the service alive; back off
            failures += 1
            delay = min(args.interval, 15 * 2 ** min(failures, 5))
            LOG.exception("satops cycle failed (attempt %d); retrying in %ds", failures, delay)
        stop.wait(delay)
    log_event(LOG, "satops stopped")
    return 0


def cmd_broker(args: argparse.Namespace) -> int:
    import uvicorn

    from .coordination.api import create_app
    from .coordination.broker import Broker
    from .coordination.hardware import LiveHardware

    from .devtools import assert_dev_safe, broker_lock

    config = load_config(args.config)
    b = config.section("broker")
    assert_dev_safe(config, args.port or int(b.get("port", 8751)))
    with broker_lock(config.data_root):        # one broker per data folder, ever
        return _run_broker(args, config, b, create_app, Broker, LiveHardware)


def _run_broker(args: argparse.Namespace, config: Config, b: dict[str, Any], create_app: Any, Broker: Any,
                LiveHardware: Any) -> int:
    import uvicorn

    api_token = pathlib.Path(b["token_file"]).read_text().strip()
    engine = _engine(config)

    def passes(hours: float) -> list[dict[str, Any]]:
        import datetime as dt

        from sqlalchemy import and_, select

        from .db.schema import satellite_passes as sp
        now = utc_now()
        with engine.connect() as conn:
            rows = conn.execute(select(sp).where(and_(
                sp.c.in_forecast.is_(True), sp.c.capture_end > now,
                sp.c.capture_start < now + dt.timedelta(hours=hours))).order_by(sp.c.capture_start)).mappings().all()
        return [{"satellite": r["satellite_name"], "receiver": r["receiver"], "start": iso(r["capture_start"]),
                 "start_local": fmt_local(r["capture_start"], config.display_tz), "end": iso(r["capture_end"]),
                 "max_elevation_deg": round(r["max_elevation_deg"] or 0, 1), "status": r["status"],
                 "status_reason": r["status_reason"]} for r in rows]

    hw = LiveHardware(config)
    broker = Broker(config, hw)
    service = build_scheduler(config, engine, broker, hw) if config.section("scheduler").get("enabled") else None
    if service is not None:
        service.status.broker_state = broker.state
    app = create_app(broker, api_token, passes=passes, scheduler=service)
    uvicorn.run(app, host=args.host or b["host"], port=args.port or int(b.get("port", 8751)), log_level="warning")
    return 0


def build_scheduler(config: Config, engine: Any, broker: Any, hw: Any) -> Any:
    from .scheduling.planner import Planner
    from .scheduling.runner import LiveRunnerHardware, Runner
    from .scheduling.service import SchedulerService
    from .scheduling.store import Scheduler

    from .scheduling.processing import Processor

    s = Scheduler(config, engine)
    broker.reservation_guard = s.next_reservation
    processor = Processor(s)
    from .inventory.hosts import HostStore
    from .scheduling.backends import RemoteDevices
    hosts = HostStore(engine, config.secrets_dir, config.data_root / "run", clock=s.clock)
    s.remote_devices = RemoteDevices(hosts)      # receivers on remote hosts: USB state over SSH, cached
    runner = Runner(s, LiveRunnerHardware(s, hw), processor=processor, hosts=hosts,
                    take_switched=lambda rx, job_id, port, until: broker.take_for_job(rx.id, job_id, port, until),
                    renew_switched=broker.renew_for_job, release_switched=broker.release_for_job,
                    find_switched=broker.lease_token_for)
    client = calendar_id = None
    cal = config.section("calendar")
    if config.section("scheduler").get("calendar") and cal.get("enabled") and cal.get("calendar_id"):
        try:
            from .calendar.google_api import CalendarClient, credentials_from_config
            client, calendar_id = CalendarClient(credentials_from_config(cal)), cal["calendar_id"]
        except Exception:  # noqa: BLE001 - scheduling must work without the calendar
            LOG.exception("scheduler calendar disabled: credentials unavailable")
    from .scheduling.rflayout import RfLayout

    def set_ports(ports: dict[int, dict[str, Any]]) -> None:     # the lease API's port list follows the layout
        broker.ports.clear()
        broker.ports.update(ports)
    has_switch = bool(getattr(hw, "switch_url", ""))
    s.rf = RfLayout(s, on_ports=set_ports, board_read=getattr(hw, "switch_board_ports", None) if has_switch else None,
                    board_push=getattr(hw, "switch_push_names", None) if has_switch else None)
    try:
        s.rf.load()
    except Exception:  # noqa: BLE001 - the broker runs on the config's receivers if the layout cannot load
        LOG.exception("RF layout could not be loaded")
    from .sessions import sdrpp
    from .sessions.backends import LocalSdrppBackend, SimSdrppBackend
    from .sessions.manager import DEFAULTS as SESSION_DEFAULTS, SessionManager
    sset = {**SESSION_DEFAULTS, **config.section("sessions")}
    s.sessions = SessionManager(s, {"local": LocalSdrppBackend(sdrpp.paths_from(sset)), "sim": SimSdrppBackend()},
                                hw=runner.hw, leases=lambda: broker.state().get("leases", {}))
    return SchedulerService(s, Planner(s), runner, client, calendar_id, processor)


def cmd_schedule(args: argparse.Namespace) -> int:
    """Local, read-mostly scheduler tools (the broker owns execution)."""
    from .scheduling.planner import Planner
    from .scheduling.service import shadow_report
    from .scheduling.store import Scheduler

    if args.action == "iq-health":            # read-only: no database, no hardware
        from .scheduling import iqhealth, rtllog
        for f in args.paths or []:
            m = re.search(r"_(\d{6,8})\.cu8$", f)
            log = next((x for x in (f + ".log", f[:-4] + ".rtl.log") if pathlib.Path(x).is_file()), None)
            h = iqhealth.assess(f, "cu8", args.rate or (int(m[1]) if m else None), gain_db=args.gain)
            out = {k: h.get(k) for k in ("file", "status", "summary", "rms_dbfs", "sample_spread_lsb", "i_std_lsb",
                                         "q_std_lsb", "i_unique_codes", "q_unique_codes", "dc_offset_i_lsb",
                                         "dc_offset_q_lsb", "clipped_fraction", "repeated_sample_fraction",
                                         "flat_block_fraction", "samples", "duration_s", "evidence", "issues")}
            if log:
                t = rtllog.parse(pathlib.Path(log).read_text(errors="replace"))
                out["tuner"] = {k: t[k] for k in ("selected_serial", "tuner", "tuned_frequency_hz", "reported_gain_db",
                                                  "reported_sample_rate_hz", "direct_sampling_final", "pll_warnings",
                                                  "pll_warnings_after_tune", "status", "evidence")}
            print(json.dumps(out, indent=2))
        return 0
    config = load_config(args.config)
    s = Scheduler(config, _engine(config))
    if args.action == "validate":
        payload = json.loads(pathlib.Path(args.file).read_text())
        print(json.dumps(s.submit(payload["jobs"], payload.get("transaction_mode", "all_or_nothing"), "cli", dry_run=True),
                         indent=2, default=str))
    elif args.action == "plan":
        print(json.dumps(Planner(s).plan(), indent=2, default=str))
    elif args.action == "jobs":
        for j in s.jobs(start=utc_now() - __import__("datetime").timedelta(hours=6)):
            print(f"{j['record_start'][:16]}  {j['receiver_id']:8} {j['source']:6} {j['status']:10} {j['job_id']}  {j['target']}")
    elif args.action == "shadow":
        print(json.dumps(shadow_report(s), indent=2, default=str))
    elif args.action == "preflight":
        return _cutover_preflight(s, args.receivers or [], args.skip_shadow)
    elif args.action == "set-mode":
        _set_mode(config.path, args.receivers[0], args.mode)
    elif args.action == "import":
        from .scheduling.catalog import Catalog
        from .scheduling.processing import Processor
        res = Catalog(s, Processor(s)).import_recordings(args.receivers or None, dry_run=not args.apply)
        for f in res["files"]:
            print(f"{'WOULD IMPORT' if res['dry_run'] else 'IMPORTED'} {f['path']} ({f['bytes'] / 1e9:.2f} GB) "
                  f"{json.dumps(f['metadata'])}" + (f" UNCERTAIN: {', '.join(f['uncertain'])}" if f["uncertain"] else ""))
        for sk in res["skipped"]:
            print(f"SKIPPED {sk['path']}: {sk['why']}")
        print(f"{res['found']} file(s){'' if res['dry_run'] else ', created ' + ', '.join(res['created'])}")
    elif args.action == "retire-satops-events":
        from .calendar.google_api import CalendarClient, credentials_from_config
        cal = config.section("calendar")
        client = CalendarClient(credentials_from_config(cal))
        now = utc_now()
        events = client.list_managed_events(cal["calendar_id"], iso(now), iso(now + __import__("datetime").timedelta(days=8)))
        gone = [e for e in events if e.get("status") != "cancelled"
                and "aobs_pass_id" in (e.get("extendedProperties", {}).get("private", {}))]
        for e in gone:
            client.delete_event(cal["calendar_id"], e["id"])
        print(f"deleted {len(gone)} future satops pass events")
    return 0


def _cutover_preflight(s: Any, receivers: list[str], skip_shadow: bool) -> int:
    """Exit 0 only if it is safe to stop the legacy recorders of these receivers right now."""
    import datetime as dt

    from .scheduling.service import shadow_report
    from .scheduling.store import ACTIVE

    now = utc_now()
    problems = []
    for j in s.jobs(status=list(ACTIVE)):
        if j["executes"]:   # the cutover restarts the broker; do it between recordings
            problems.append(f"broker job {j['job_id']} on {j['receiver_id']} is {j['status']}")
    for rid in receivers:
        rx = s.receivers.get(rid)
        if rx is None:
            problems.append(f"unknown receiver {rid}")
            continue
        soon = s.jobs(receiver=rid, status=["scheduled", *ACTIVE], start=now, end=now + dt.timedelta(minutes=20))
        for j in soon:
            problems.append(f"{rid}: {j['source']} {j['status']} {j['target']} reserved "
                            f"{j['reservation_start']} to {j['reservation_end']} (within 20 min)")
        if not skip_shadow:
            rep = shadow_report(s).get(rid)
            if rep is None:
                problems.append(f"{rid}: not in shadow mode, so there is no shadow comparison (use --skip-shadow to override)")
            elif not rep["agrees"]:
                problems.append(f"{rid}: shadow comparison not good enough yet: {json.dumps({k: rep[k] for k in ('compared', 'pending', 'worst_start_diff_s', 'unmatched_shadow', 'issues')})}")
    print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
    return 0 if not problems else 3


def _set_mode(path: pathlib.Path, receiver: str, mode: str) -> None:
    """Rewrite one receiver's `mode = ...` line in place, leaving the rest of the file untouched."""
    import re

    if mode not in ("legacy", "shadow", "broker"):
        raise SystemExit("mode must be legacy, shadow or broker")
    lines = path.read_text().splitlines(keepends=True)
    header = f"[scheduler.receivers.{receiver}]"
    inside, done = False, False
    for i, line in enumerate(lines):
        if line.startswith("["):
            inside = line.strip() == header
            continue
        if inside and re.match(r"^mode\s*=", line):
            lines[i] = re.sub(r'^mode\s*=\s*"[a-z]+"', f'mode = "{mode}"', line)
            done = True
            break
    if not done:
        raise SystemExit(f"no mode line under {header} in {path}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(lines))
    tmp.replace(path)
    print(f"{receiver}: mode = {mode}")


def _broker_running() -> list[int]:
    out = []
    for d in pathlib.Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if "aobs" in cmd and " broker" in cmd:
            out.append(int(d.name))
    return out


def cmd_db(args: argparse.Namespace) -> int:
    """Database status, backups and restore. `status` and `list` never migrate or write."""
    from .db import make_engine
    from .db import migrate as m
    from .db.schema import SCHEMA_VERSION

    from .devtools import assert_dev_safe
    config = load_config(args.config)
    assert_dev_safe(config)
    engine = make_engine(config.database_url)
    if args.action == "status":
        found = m.current_version(engine)
        print(json.dumps({"database": str(m.sqlite_path(engine) or config.database_url), "schema_version": found,
                          "code_schema_version": SCHEMA_VERSION,
                          "state": "new" if found is None else "up to date" if found == SCHEMA_VERSION else
                          "will be migrated at the next start (backup first)" if found < SCHEMA_VERSION else
                          "NEWER than this code: refuse to start", "backups": m.list_backups(engine)[:10]}, indent=2))
        return 0
    if args.action == "migrate":                 # installer/upgrade: create or migrate (backup first)
        from .db import init_db
        print(json.dumps(init_db(engine), indent=2))
        return 0
    if args.action == "list":
        for b in m.list_backups(engine):
            print(f"{b['created_at'][:19]}  {b['bytes'] / 1e6:9.1f} MB  {b['file']}")
        return 0
    if args.action == "backup":
        path = m.backup(engine, "manual")
        print(path or "not a SQLite database: use the database's own backup tools")
        return 0 if path else 1
    if args.action == "restore":
        if not args.file:
            print("restore needs the backup file (see `aobs db list`)")
            return 2
        src = pathlib.Path(args.file)
        if not src.is_absolute():
            src = (m.sqlite_path(engine) or pathlib.Path(".")).parent / "backups" / src
        running = _broker_running()
        if running:
            print(f"the broker is running (pid {', '.join(map(str, running))}); stop it first "
                  "(sudo systemctl stop observatory-broker), restore, then start it")
            return 1
        if not args.yes:
            print(f"this replaces {m.sqlite_path(engine)} with {src.name} (a safety backup is taken first). "
                  "Run again with --yes to do it.")
            return 1
        print(json.dumps(m.restore(engine, src), indent=2))
        return 0
    return 2


def cmd_web_link(args: argparse.Namespace) -> int:
    """Print a sign-in link for the web pages (valid 15 minutes; the browser then stays signed in 30 days)."""
    import socket

    from .web.auth import login_url

    config = load_config(args.config)
    b = config.section("broker")
    host = str(b.get("host", "127.0.0.1"))
    if host in ("0.0.0.0", "::", ""):
        host = socket.gethostname()
    base = args.base_url or f"http://{host}:{b.get('port', 8751)}"
    print(login_url(config, base))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="starlab", description="Starlab Observatory")
    parser.add_argument("--config")
    parser.add_argument("--json-logs", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventory", help="Receivers, USB holders, antenna switch, leases (read-only)")
    f = sub.add_parser("forecast", help="Rolling METEOR forecast with readiness (read-only)")
    f.add_argument("--hours", type=float)
    f.add_argument("--json", action="store_true")
    f.add_argument("--no-readiness", action="store_true")
    r = sub.add_parser("readiness", help="Detailed readiness checks for upcoming passes")
    r.add_argument("--limit", type=int, default=3)
    s = sub.add_parser("sync-once", help="One satops cycle (writes the observatory database)")
    s.add_argument("--calendar", choices=["dry-run", "apply"])
    run = sub.add_parser("run", help="Satops service loop")
    run.add_argument("--interval", type=int, default=300)
    c = sub.add_parser("calendar", help="Google Calendar setup and sync")
    csub = c.add_subparsers(dest="calendar_cmd", required=True)
    a = csub.add_parser("auth")
    a.add_argument("--port", type=int, default=8085)
    st = csub.add_parser("setup")
    st.add_argument("--create", action="store_true")
    st.add_argument("--calendar-id")
    pl = csub.add_parser("plan")
    pl.add_argument("--show", type=int, default=0)
    csub.add_parser("sync")
    br = sub.add_parser("broker", help="Receiver broker API for the scanning agent")
    br.add_argument("--host")
    br.add_argument("--port", type=int)
    sc = sub.add_parser("schedule", help="Unified scheduler: validate a job file (dry run), plan, list, shadow report")
    sc.add_argument("action", choices=["validate", "plan", "jobs", "shadow", "preflight", "set-mode",
                                       "retire-satops-events", "import", "iq-health"])
    sc.add_argument("--paths", nargs="*", help="iq-health: CU8 files to assess (read-only)")
    sc.add_argument("--rate", type=int, help="iq-health: sample rate if not in the file name")
    sc.add_argument("--gain", type=float, help="iq-health: requested gain in dB, if known")
    sc.add_argument("--apply", action="store_true", help="import: create records (default is a dry run)")
    sc.add_argument("file", nargs="?", help="validate: job JSON file")
    sc.add_argument("--receivers", nargs="*", help="preflight/set-mode: receiver ids")
    sc.add_argument("--mode", help="set-mode: legacy | shadow | broker")
    sc.add_argument("--skip-shadow", action="store_true", help="preflight: do not require a passing shadow comparison")
    dbp = sub.add_parser("db", help="Database: status, backup, list backups, restore (broker stopped)")
    dbp.add_argument("action", choices=["status", "migrate", "backup", "list", "restore"])
    dbp.add_argument("file", nargs="?", help="restore: backup file (name in backups/ or a path)")
    dbp.add_argument("--yes", action="store_true", help="restore: really replace the database")
    wl = sub.add_parser("web-link", help="Print a sign-in link for the web pages")
    wl.add_argument("--base-url", help="how browsers reach this server, e.g. http://starlab.local:8751")
    args = parser.parse_args(argv)
    setup_logging(json_output=args.json_logs or args.cmd in ("run", "broker"), level=logging.INFO)
    handlers = {"inventory": cmd_inventory, "forecast": cmd_forecast, "readiness": cmd_readiness,
                "sync-once": cmd_sync_once, "run": cmd_run, "calendar": cmd_calendar, "broker": cmd_broker,
                "schedule": cmd_schedule, "db": cmd_db, "web-link": cmd_web_link}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
