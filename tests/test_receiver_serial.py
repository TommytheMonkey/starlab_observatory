"""Re-serialing a receiver (rtl_eeprom -s): the inventory row and its not-started bookings follow the new serial."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from aobs.db.schema import sched_jobs
from aobs.inventory.store import InventoryError, effective_readiness
from test_scheduler import env, errors, rtl_job, submit  # noqa: F401


def add_r07(env: Any, serial: str = "00000001") -> None:
    env.s.inventory.create(rid="OBS-R07", name="OBS-R07", driver="rtlsdr", serial=serial, identity="serial",
                           usb_path="5-1", usb_vid="0bda", usb_pid="2838", manufacturer="RTLSDRBlog",
                           model="Blog V4L", config={}, capabilities=None, actor="owner (web)")
    env.s.inventory.record_readiness("OBS-R07", True, {"test_id": "t1", "job_id": "m0"}, "test t1")
    env.present.add(serial)
    env.s.reload_receivers()


def booked_serials(env: Any) -> dict[str, tuple[str, str]]:
    with env.s.engine.begin() as conn:
        return {r.job_id: (r.device_serial, r.spec["resolved"]["device_serial"])
                for r in conn.execute(select(sched_jobs.c.job_id, sched_jobs.c.device_serial, sched_jobs.c.spec)
                                      .where(sched_jobs.c.receiver_id == "OBS-R07"))}


def test_new_serial_moves_the_receiver_its_bookings_and_its_readiness(env):
    add_r07(env)
    job = rtl_job(rx="OBS-R07")
    job["receiver"]["serial"] = "00000001"
    r = submit(env, job)
    assert r["results"][0]["ok"], errors(r)
    jid = r["results"][0]["job_id"]

    dry = env.s.inventory.change_serial("OBS-R07", "OBS-R07", "test", same_port=True, apply=False)
    assert dry["bookings"] == [jid] and dry["readiness_carried_over"] and not dry["applied"]
    assert env.s.inventory.get("OBS-R07")["serial"] == "00000001"            # a dry run writes nothing

    env.s.inventory.change_serial("OBS-R07", "OBS-R07", "test", same_port=True, apply=True)
    row = env.s.inventory.get("OBS-R07")
    assert row["serial"] == "OBS-R07" and row["identity_key"] == "sn:OBS-R07"
    assert effective_readiness(row) == ("verified", False)
    assert row["readiness_detail"]["serial_changed_from"] == "00000001"
    assert booked_serials(env)[jid] == ("OBS-R07", "OBS-R07")


def test_without_the_new_serial_on_the_same_port_the_receiver_is_untested(env):
    add_r07(env)
    out = env.s.inventory.change_serial("OBS-R07", "OBS-R07", "test", same_port=False, apply=True)
    assert not out["readiness_carried_over"]
    assert effective_readiness(env.s.inventory.get("OBS-R07")) == ("untested", True)


def test_serial_change_refuses_a_busy_receiver_a_taken_serial_and_toml_receivers(env):
    add_r07(env)
    with pytest.raises(InventoryError, match="already receiver OBS-R02"):
        env.s.inventory.change_serial("OBS-R07", "OBS-R02", "test", same_port=True, apply=True)
    with pytest.raises(InventoryError, match="observatory.toml"):
        env.s.inventory.change_serial("OBS-R02", "X1", "test", same_port=True, apply=True)
    job = rtl_job(rx="OBS-R07")
    job["receiver"]["serial"] = "00000001"
    jid = submit(env, job)["results"][0]["job_id"]
    with env.s.engine.begin() as conn:
        conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == jid).values(status="recording"))
    with pytest.raises(InventoryError, match="in use"):
        env.s.inventory.change_serial("OBS-R07", "OBS-R07", "test", same_port=True, apply=True)
    assert env.s.inventory.get("OBS-R07")["serial"] == "00000001"
