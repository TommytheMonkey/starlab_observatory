import dataclasses
import datetime as dt
from typing import Any

import pytest
from sqlalchemy import func, select

from aobs.calendar import sync
from aobs.calendar.google_api import CalendarError
from aobs.calendar.render import body_hash, event_id, render_event
from aobs.db import init_db, make_engine
from aobs.db.schema import calendar_events, pass_status_history, satellite_passes
from aobs.legacy import RecorderIntent
from aobs.satellites.service import run_cycle

from conftest import NOW, make_forecast, make_pass, make_probe

CAL = "cal@group.calendar.google.com"


class FakeCalendar:
    """Mimics Calendar v3 id semantics: ids are unique forever, inserting a used id -> 409."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def get_event(self, calendar_id, eid):
        return self.events.get(eid)

    def insert_event(self, calendar_id, body):
        self.calls.append("insert")
        if body["id"] in self.events:
            raise CalendarError(409, "The requested identifier already exists.")
        self.events[body["id"]] = dict(body, etag=f'"{len(self.calls)}"')
        return self.events[body["id"]]

    def update_event(self, calendar_id, eid, body):
        self.calls.append("update")
        if eid not in self.events:
            raise CalendarError(404, "Not Found")
        self.events[eid] = dict(body, id=eid, etag=f'"{len(self.calls)}"')
        return self.events[eid]


@pytest.fixture
def engine(tmp_config):
    e = make_engine(tmp_config.database_url)
    init_db(e)
    return e


def three_passes():
    return [make_pass(NOW + dt.timedelta(hours=1), rev=17115),
            make_pass(NOW + dt.timedelta(hours=6), norad=59051, rev=13591),
            make_pass(NOW + dt.timedelta(hours=30), rev=17130)]


def probe_for(passes):
    return make_probe(recorder_intent=RecorderIntent("OBS-R01", passes[0].satellite_name, passes[0].capture_start, ""))


def count(engine, table):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(table)).scalar_one()


def statuses(engine):
    with engine.connect() as c:
        return dict(c.execute(select(satellite_passes.c.id, satellite_passes.c.status)).all())


def test_cycle_is_idempotent(tmp_config, fake_legacy, engine):
    passes = three_passes()
    for _ in range(3):
        run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes), probe_for(passes))
    assert count(engine, satellite_passes) == 3
    assert statuses(engine) == {"57166-r17115": "READY", "59051-r13591": "READY", "57166-r17130": "SCHEDULED"}
    assert count(engine, pass_status_history) == 5  # 3 inserts + 2 promotions, no repeats


def test_prediction_shift_updates_same_pass(tmp_config, fake_legacy, engine):
    passes = three_passes()
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes), probe_for(passes))
    # New TLE: pass moves 40 s and our computed revolution number differs.
    shifted = [dataclasses.replace(passes[0], pass_id="57166-r99999", revolution=99999,
                                   culmination=passes[0].culmination + dt.timedelta(seconds=40),
                                   capture_start=passes[0].capture_start + dt.timedelta(seconds=40))] + passes[1:]
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(shifted), probe_for(shifted))
    assert count(engine, satellite_passes) == 3
    with engine.connect() as c:
        start = c.execute(select(satellite_passes.c.capture_start).where(satellite_passes.c.id == "57166-r17115")).scalar_one()
    assert start == passes[0].capture_start + dt.timedelta(seconds=40)


def test_vanished_pass_becomes_warning_not_ready(tmp_config, fake_legacy, engine):
    passes = three_passes()
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes), probe_for(passes))
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes[:1] + passes[2:]), probe_for(passes))
    s = statuses(engine)
    assert s["59051-r13591"] == "WARNING"
    with engine.connect() as c:
        reason = c.execute(select(satellite_passes.c.status_reason).where(satellite_passes.c.id == "59051-r13591")).scalar_one()
    assert "no longer appears" in reason


def test_missed_after_window(tmp_config, fake_legacy, engine):
    passes = three_passes()
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes), probe_for(passes))
    later = passes[0].capture_end + dt.timedelta(minutes=6)
    run_cycle(tmp_config, fake_legacy, engine, later, make_forecast(passes[1:], later), probe_for(passes[1:]))
    assert statuses(engine)["57166-r17115"] == "MISSED"


# ---------------------------------------------------------------- calendar
def test_event_id_is_valid_base32hex():
    eid = event_id("57166-r17115")
    assert eid == "aobs57166r17115"
    assert set(eid) <= set("abcdefghijklmnopqrstuv0123456789")


def test_render_is_ten_minutes_and_shows_full_window(tmp_config, fake_legacy, engine):
    passes = three_passes()
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes), probe_for(passes))
    with engine.connect() as c:
        row = c.execute(select(satellite_passes).where(satellite_passes.c.id == "57166-r17115")).mappings().one()
    body = render_event(row, "America/New_York", 10)
    start = dt.datetime.fromisoformat(body["start"]["dateTime"].replace("Z", "+00:00"))
    end = dt.datetime.fromisoformat(body["end"]["dateTime"].replace("Z", "+00:00"))
    assert end - start == dt.timedelta(minutes=10)
    assert "11 min 30 s" in body["description"]
    assert "first 10 minutes" in body["description"]
    for label in ("NORAD ID: 57166", "Predicted AOS", "Predicted LOS", "Maximum elevation", "Recording computer: observatory",
                  "OBS-R01", "Antenna:", "Center frequency: 137.500 MHz", "Sample rate", "Predicted quality", "readiness: READY"):
        assert label in body["description"], label


def test_render_hash_ignores_millisecond_jitter(tmp_config, fake_legacy, engine):
    passes = three_passes()
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes), probe_for(passes))
    with engine.connect() as c:
        row = dict(c.execute(select(satellite_passes).where(satellite_passes.c.id == "57166-r17115")).mappings().one())
    jitter = dict(row, capture_start=row["capture_start"] + dt.timedelta(milliseconds=40),
                  capture_end=row["capture_end"] - dt.timedelta(milliseconds=30))
    assert body_hash(render_event(row, "America/New_York")) == body_hash(render_event(jitter, "America/New_York"))


def test_calendar_sync_idempotent_and_updates_on_change(tmp_config, fake_legacy, engine):
    passes = three_passes()
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes), probe_for(passes))
    cal = FakeCalendar()
    assert sync.apply(engine, cal, CAL, sync.plan(tmp_config, engine, CAL, NOW), NOW)["create"] == 3
    again = sync.plan(tmp_config, engine, CAL, NOW)
    assert {a.kind for a in again} == {"noop"}
    assert sync.apply(engine, cal, CAL, again, NOW) == {"create": 0, "update": 0, "noop": 3, "error": 0}
    # Pass disappears -> its event must be updated away from READY.
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes[:1] + passes[2:]), probe_for(passes))
    acts = sync.plan(tmp_config, engine, CAL, NOW)
    assert [a.pass_id for a in acts if a.kind == "update"] == ["59051-r13591"]
    sync.apply(engine, cal, CAL, acts, NOW)
    ev = cal.events["aobs59051r13591"]
    assert "WARNING" in ev["summary"] and "READY" not in ev["summary"]
    assert len(cal.events) == 3


def test_lost_database_does_not_duplicate_events(tmp_config, fake_legacy, engine, tmp_path):
    passes = three_passes()
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes), probe_for(passes))
    cal = FakeCalendar()
    sync.apply(engine, cal, CAL, sync.plan(tmp_config, engine, CAL, NOW), NOW)
    with engine.begin() as c:  # simulate losing sync state
        c.execute(calendar_events.delete())
    counts = sync.apply(engine, cal, CAL, sync.plan(tmp_config, engine, CAL, NOW), NOW)
    assert counts["create"] == 3 and counts["error"] == 0  # 409 -> update path
    assert len(cal.events) == 3


def test_one_failing_event_does_not_block_others(tmp_config, fake_legacy, engine):
    passes = three_passes()
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes), probe_for(passes))

    class Flaky(FakeCalendar):
        def insert_event(self, calendar_id, body):
            if body["id"] == "aobs59051r13591":
                raise CalendarError(400, "bad")
            return super().insert_event(calendar_id, body)

    cal = Flaky()
    counts = sync.apply(engine, cal, CAL, sync.plan(tmp_config, engine, CAL, NOW), NOW)
    assert counts == {"create": 2, "update": 0, "noop": 0, "error": 1}
    retry = sync.plan(tmp_config, engine, CAL, NOW)
    assert [a.pass_id for a in retry if a.kind != "noop"] == ["59051-r13591"]


def test_calendar_gate_closed_by_default(tmp_config, fake_legacy, engine):
    from aobs.cli import calendar_gate

    passes = three_passes()
    result = run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes), probe_for(passes))
    ok, problems = calendar_gate(tmp_config, result)
    assert not ok
    assert any("enabled" in p for p in problems)


def test_millisecond_jitter_never_changes_stored_pass_or_event(tmp_config, fake_legacy, engine):
    passes = three_passes()
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes), probe_for(passes))
    cal = FakeCalendar()
    sync.apply(engine, cal, CAL, sync.plan(tmp_config, engine, CAL, NOW), NOW)
    for ms in (499, 501, -499, 37):
        j = dt.timedelta(milliseconds=ms)
        jittered = [dataclasses.replace(p, **{f: getattr(p, f) + j for f in (
            "aos", "los", "threshold_rise", "threshold_set", "culmination", "record_start", "record_end",
            "capture_start", "capture_end")}) for p in passes]
        run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(jittered), probe_for(jittered))
        assert {a.kind for a in sync.plan(tmp_config, engine, CAL, NOW)} == {"noop"}, ms


def test_real_prediction_change_still_updates_event(tmp_config, fake_legacy, engine):
    passes = three_passes()
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(passes), probe_for(passes))
    cal = FakeCalendar()
    sync.apply(engine, cal, CAL, sync.plan(tmp_config, engine, CAL, NOW), NOW)
    moved = [dataclasses.replace(passes[2], capture_start=passes[2].capture_start + dt.timedelta(seconds=30),
                                 record_start=passes[2].record_start + dt.timedelta(seconds=30))] + passes[:2]
    run_cycle(tmp_config, fake_legacy, engine, NOW, make_forecast(moved), probe_for(passes))
    assert [a.pass_id for a in sync.plan(tmp_config, engine, CAL, NOW) if a.kind == "update"] == ["57166-r17130"]
