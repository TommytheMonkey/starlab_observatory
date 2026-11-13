"""Which receivers the recording form and bulk booking tick by default (rxsuggest.py)."""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any

import pytest

from aobs.db.schema import sched_jobs
from aobs.scheduling import rflayout, schedview
from aobs.scheduling.rxsuggest import suggest
from test_rule_scheduler import M23, M24, Passes, env  # noqa: F401
from test_scheduler import NOW

UTC = dt.timezone.utc
FREQ = 137_900_000


@pytest.fixture
def rf(env):
    env.s.rf = rflayout.RfLayout(env.s)
    env.s.rf.load()
    return env.s.rf


def layout(rf: rflayout.RfLayout, ranges: dict[str, tuple[float | None, float | None]],
           chains: dict[str, list[str]] | None = None, ports: dict[str, str] | None = None) -> None:
    """Save a layout with the given antenna ranges (MHz) and receiver chains."""
    lay = copy.deepcopy(rf.current["layout"])
    c = lay["components"]
    for aid, (lo, hi) in ranges.items():
        c.setdefault(aid, {"id": aid, "name": aid, "kind": "antenna", "status": "working", "power": "none",
                           "model": "", "notes": "", "gain_db": None})
        c[aid].update(min_mhz=lo, max_mhz=hi)
    for rid, chain in (chains or {}).items():
        lay["receivers"][rid]["chain"] = chain
    if ports:
        c["ANTSW-01"]["ports"].update(ports)
    rf.save(lay, rf.current["version"], "test")


def window(start_min: float = 60, minutes: float = 12) -> tuple[dt.datetime, dt.datetime]:
    a = NOW + dt.timedelta(minutes=start_min)
    return a, a + dt.timedelta(minutes=minutes)


def past_job(env: Any, rid: str, status: str, hours_ago: float, norad: int | None = None, name: str = "x") -> None:
    rx = env.s.receivers[rid]
    a = NOW - dt.timedelta(hours=hours_ago)
    b = a + dt.timedelta(minutes=10)
    with env.s.engine.begin() as conn:
        conn.execute(sched_jobs.insert().values(
            job_id=f"m-{rid}-{status}-{hours_ago}", job_name=name, source="manual", receiver_id=rid,
            device_serial=rx.serial, status=status, executes=True, reservation_start=a, reservation_end=b,
            record_start=a, record_end=b, spec={"metadata": {"norad": norad}} if norad else {"metadata": {}},
            created_at=a, updated_at=a))


def book(env: Any, rid: str, start: dt.datetime, end: dt.datetime) -> None:
    form = {"name": "block", "receiver_id": rid, "start": start.isoformat(), "end": end.isoformat(), "timezone": "UTC",
            "frequency_hz": 100_000_000, "sample_rate_hz": 2_048_000, "pre_roll_s": 0, "post_roll_s": 0}
    if env.s.receivers[rid].switched:
        form["antenna"] = "V-DIPOLE"
    schedview.book(env.s, form, "owner")


def candidates(env: Any) -> list[str]:
    return [r["receiver_id"] for r in suggest(env.s, None, None, None, None)["ranked"]]


# ------------------------------------------------------------------ 1. antenna coverage, narrowest first
def test_the_narrowest_antenna_that_covers_the_frequency_wins(env, rf):
    layout(rf, {"DISCONE": (25, 1300), "QFH-B": (130, 140), "WIDE": (100, 200)},
           chains={"RSP1B": ["QFH-B"], "OBS-R02": ["DISCONE"]})
    a, b = window()
    res = suggest(env.s, FREQ, M23, a, b, sat_name="METEOR-M2 3")
    assert res["receiver_ids"] == ["RSP1B"]
    why = res["reasons"]["RSP1B"]
    assert why["basis"] == "antenna" and "QFH-B (130–140 MHz)" in why["reason"] and "narrowest" in why["reason"]
    ranked = [r["receiver_id"] for r in res["ranked"]]
    assert ranked.index("RSP1B") < ranked.index("OBS-R02")           # both cover; the discone is wider


def test_an_antenna_that_does_not_cover_or_is_broken_is_not_a_match(env, rf):
    layout(rf, {"DISCONE": (25, 1300), "QFH-B": (400, 470)}, chains={"RSP1B": ["QFH-B"], "OBS-R02": ["DISCONE"]})
    a, b = window()
    assert suggest(env.s, FREQ, None, a, b)["receiver_ids"] == ["OBS-R02"]
    lay = copy.deepcopy(rf.current["layout"])
    lay["components"]["DISCONE"]["status"] = "broken"
    rf.save(lay, rf.current["version"], "test")
    assert suggest(env.s, FREQ, None, a, b)["reasons"].get("OBS-R02", {}).get("basis") != "antenna"


def test_a_switched_receiver_suggests_the_port_antenna_that_fits(env, rf):
    layout(rf, {"V-DIPOLE": (130, 140), "MA-30-LOOP": (0.01, 30)})
    a, b = window()
    assert "OBS-R05" in candidates(env)
    res = suggest(env.s, FREQ, None, a, b)
    assert res["receiver_ids"] == ["OBS-R05"] and res["antenna"] == "V-DIPOLE"
    assert suggest(env.s, 10_000_000, None, a, b)["antenna"] == "MA-30-LOOP"


# ------------------------------------------------------------------ 2. the satellite's last completed recording
def test_the_receiver_that_last_completed_the_satellite_breaks_a_tie(env, rf):
    layout(rf, {"DISCONE": (25, 1300), "DISCONE-2": (25, 1300)}, chains={"RSP1B": ["DISCONE-2"], "OBS-R02": ["DISCONE"]})
    past_job(env, "RSP1B", "completed", 30, norad=M23)
    past_job(env, "OBS-R02", "completed", 20, norad=M24)                  # a different satellite, later
    past_job(env, "OBS-R02", "failed", 10, norad=M23)                     # a failure is not a completed recording
    a, b = window()
    res = suggest(env.s, FREQ, M23, a, b, sat_name="METEOR-M2 3")
    assert res["receiver_ids"] == ["RSP1B"]
    assert "last completed METEOR-M2 3" in res["reasons"]["RSP1B"]["reason"] and res["reasons"]["RSP1B"]["at"]


def test_without_antenna_ranges_the_satellite_history_decides(env, rf):
    past_job(env, "RSP1B", "completed", 30, norad=M23)
    past_job(env, "OBS-R02", "completed", 2, norad=M24)
    a, b = window()
    res = suggest(env.s, FREQ, M23, a, b, sat_name="METEOR-M2 3")
    assert res["receiver_ids"] == ["RSP1B"] and res["reasons"]["RSP1B"]["basis"] == "satellite"


# ------------------------------------------------------------------ 3. reservations in the setup-teardown window
def test_a_receiver_booked_in_the_setup_to_teardown_window_is_left_out(env, rf):
    layout(rf, {"DISCONE": (25, 1300), "QFH-B": (130, 140)}, chains={"RSP1B": ["QFH-B"], "OBS-R02": ["DISCONE"]})
    a, b = window(60, 12)
    book(env, "RSP1B", b + dt.timedelta(seconds=10), b + dt.timedelta(minutes=5))     # only inside the teardown
    assert suggest(env.s, FREQ, None, a, b, 0, 0)["receiver_ids"] == ["RSP1B"]        # no teardown: no clash
    res = suggest(env.s, FREQ, None, a, b, 30, 30)
    assert res["receiver_ids"] == ["OBS-R02"] and "RSP1B" in res["excluded"]
    assert "busy" in res["excluded"]["RSP1B"]
    assert "RSP1B" not in [r["receiver_id"] for r in res["ranked"]]


def test_every_receiver_busy_suggests_nothing(env, rf):
    a, b = window()
    for rid in candidates(env):
        book(env, rid, a, b)
    res = suggest(env.s, FREQ, None, a, b)
    assert res["receiver_ids"] == [] and res["reasons"] == {} and set(res["excluded"]) == set(candidates(env))


# ------------------------------------------------------------------ 4. fallback: the most recently used receiver
def test_fallback_is_the_most_recently_used_receiver(env, rf):
    past_job(env, "RSP1B", "completed", 30)
    past_job(env, "OBS-R02", "failed", 3)                 # it ran, so it was used
    past_job(env, "RSP1B", "cancelled", 1)               # never ran: not a use
    a, b = window()
    res = suggest(env.s, 98_100_000, None, a, b)
    assert res["receiver_ids"] == ["OBS-R02"] and res["reasons"]["OBS-R02"]["basis"] == "recent"


def test_no_history_and_no_ranges_still_ticks_one_receiver(env, rf):
    a, b = window()
    res = suggest(env.s, None, None, a, b)
    assert len(res["receiver_ids"]) == 1 and res["reasons"][res["receiver_ids"][0]]["basis"] == "first"


# ------------------------------------------------------------------ the pass form and bulk booking use it
def test_pass_form_ticks_the_suggestion_and_books_with_it(env, rf, monkeypatch):
    Passes(monkeypatch, [(M23, 120, 14, 45), (M24, 125, 12, 50)])
    layout(rf, {"DISCONE": (25, 1300), "QFH-B": (130, 140)}, chains={"RSP1B": ["QFH-B"], "OBS-R02": ["DISCONE"]})
    found = __import__("aobs.satellites.passes", fromlist=["PassService"]).PassService(env.s).predicted(48)
    p1, p2 = found["passes"][:2]
    f = schedview.form_for_pass(env.s, p1["norad"], p1["aos"], None)
    assert f["receiver_ids"] == ["RSP1B"] and f["receiver_id"] == "RSP1B" and "RSP1B" in f["suggest"]["reasons"]
    schedview.book(env.s, f, "owner")                     # the suggestion travels back harmlessly
    # bulk booking asks for each pass in turn, so the overlapping second pass goes elsewhere
    f2 = schedview.form_for_pass(env.s, p2["norad"], p2["aos"], None)
    assert f2["receiver_ids"] == ["OBS-R02"] and "RSP1B" in f2["suggest"]["excluded"]
    # an explicit receiver is still respected
    f3 = schedview.form_for_pass(env.s, p2["norad"], p2["aos"], "OBS-R02")
    assert f3["receiver_ids"] == ["OBS-R02"] and f3["antenna"] is None
