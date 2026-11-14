"""The watch resets a wedged receiver's USB port (on the dongle staying on USB but streaming nothing, 2026-11-12)."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any

import pytest

from aobs.piratewatch.config import Settings
from aobs.piratewatch.control import resume
from aobs.piratewatch.daemon import NoData, Watch, read_control
from aobs.piratewatch.lease import LeaseKeeper
from aobs.piratewatch.radio import HubPorts, RadioError
from aobs.piratewatch.store import Store
from aobs.piratewatch.transcribe import Transcriber, WhisperProcess

from test_piratewatch import FakeBroker, noise, sweep

UTC = dt.timezone.utc


class Usb:
    """One RTL on one port; a reset can bring it back, leave it gone, or fail."""

    def __init__(self, port: str = "3-2.1", comes_back: bool = True) -> None:
        self.port: str | None = port
        self.comes_back = comes_back
        self.fail: str | None = None
        self.resets: list[str] = []
        self.lease_held_at_reset: list[bool] = []

    def where(self, serial: str) -> str | None:
        assert serial == "OBS-R05"
        return self.port

    def reset(self, port: str) -> str:
        if self.fail:
            raise RadioError(self.fail)
        self.resets.append(port)
        if not self.comes_back:
            self.port = None
        return "/dev/bus/usb/003/041"


@pytest.fixture
def rig(tmp_path: pathlib.Path) -> Any:
    def make(usb: Usb, now: dt.datetime = dt.datetime(2026, 11, 13, 15, 0, tzinfo=UTC), **kw: Any) -> Any:
        s = Settings(data_dir=str(tmp_path / "d"), tool_prefix=str(tmp_path), usb_reenumerate_s=0.2, **kw)
        fb = FakeBroker()
        keeper = LeaseKeeper(fb, s.receiver, s.lease_owner, 2, 120, renew_every_s=3600)  # type: ignore[arg-type]
        alerts: list[tuple[str, dict]] = []
        clock = {"now": now}
        w = Watch(s, keeper=keeper, device_index=lambda _s: 0, holders=lambda _s: [], bias_forced=lambda _i: False,
                  bias_off=lambda _s: None, usb_port=usb.where, usb_reset=usb.reset,
                  hub=HubPorts(s, sysfs=tmp_path / "no-sysfs"),             # not on a hub: the USB reset path
                  alert=lambda text, data: alerts.append((text, data)), clock=lambda: clock["now"],
                  transcriber=Transcriber(s, Store(tmp_path / "x.sqlite3"), worker=WhisperProcess(s, ["true"])))
        w.wait = lambda _s: None

        def no_data(_index: int) -> Any:
            usb.lease_held_at_reset.append(keeper.token is not None)
            raise NoData("rtl_power gave no data for 19 s (OBS-R05 dropped off USB?)")
        w.sweep = no_data
        orig = usb.reset

        def reset(port: str) -> str:
            usb.lease_held_at_reset.append(keeper.token is not None)
            return orig(port)
        w.usb_reset = reset
        return w, fb, alerts, clock
    return make


def resets_logged(w: Watch) -> list[dict]:
    return [json.loads(line) for line in w.reset_log_path().read_text().splitlines()]


def test_three_no_data_failures_in_a_row_reset_the_port_under_the_lease(rig: Any) -> None:
    usb = Usb()
    w, fb, alerts, _ = rig(usb)
    w.cycle()
    w.cycle()
    assert usb.resets == [] and w._no_data == 2
    w.cycle()
    assert usb.resets == ["3-2.1"]
    assert usb.lease_held_at_reset[-1] is True                  # nobody else opened the dongle meanwhile
    assert ("DELETE", "/v1/lease/tok") in fb.calls
    [e] = resets_logged(w)
    assert e["ok"] is True and e["port"] == "3-2.1" and e["back_on_port"] == "3-2.1" and e["reset_this_hour"] == 1
    assert e["no_data_in_a_row"] == 3 and e["node"] == "/dev/bus/usb/003/041"
    assert w._no_data == 0 and alerts == [] and not read_control(w.s).get("usb_halted")
    assert json.loads(w.s.path("status.json").read_text())["usb_resets_last_hour"] == 1


def test_a_good_sweep_restarts_the_count(rig: Any) -> None:
    import numpy as np
    usb = Usb()
    w, *_ = rig(usb)
    w.cycle()
    w.cycle()
    w.on_sweep(sweep(noise(np.random.default_rng(1))))
    w.cycle()
    w.cycle()
    assert usb.resets == []


def test_a_receiver_that_does_not_come_back_stops_the_watch_and_alerts(rig: Any) -> None:
    usb = Usb(comes_back=False)
    w, fb, alerts, _ = rig(usb)
    for _ in range(3):
        w.cycle()
    assert usb.resets == ["3-2.1"]
    assert resets_logged(w)[0]["ok"] is False
    [(text, data)] = alerts
    assert "Pirate watch stopped" in text and "did not come back on USB" in text and "resume" in text
    assert data["source"] == "pirate-watch" and data["receiver"] == "OBS-R05"
    assert read_control(w.s)["usb_halted"]
    fb.calls.clear()
    w.cycle()                                                    # stays off: no lease, no radio
    assert w.state == "blocked" and fb.calls == []
    resume(w.s)
    assert not read_control(w.s).get("usb_halted")


def test_a_failed_reset_ioctl_stops_and_alerts(rig: Any) -> None:
    usb = Usb()
    usb.fail = "USB reset of /dev/bus/usb/003/041 (port 3-2.1) failed: Permission denied"
    w, _, alerts, _ = rig(usb)
    for _ in range(3):
        w.cycle()
    assert resets_logged(w)[0]["error"].endswith("Permission denied")
    assert len(alerts) == 1 and "Permission denied" in alerts[0][0]


def test_a_vanished_receiver_is_reset_on_its_last_known_port(rig: Any) -> None:
    usb = Usb()
    w, *_ = rig(usb)
    w.cycle()                                                    # seen on 3-2.1 while acquiring
    usb.port = None
    w.usb_port = lambda _s: usb.port
    w.cycle()
    w.cycle()
    assert usb.resets == ["3-2.1"]


def test_at_most_three_resets_an_hour_then_stop_and_alert(rig: Any) -> None:
    usb = Usb()
    w, _, alerts, clock = rig(usb)
    start = clock["now"]
    w.reset_log_path().parent.mkdir(parents=True, exist_ok=True)
    w.log_reset({"at": (start - dt.timedelta(minutes=70)).isoformat(), "ok": True})      # older than an hour
    for n in range(3):
        clock["now"] = start + dt.timedelta(minutes=10 * n)
        for _ in range(3):
            w.cycle()
    assert usb.resets == ["3-2.1"] * 3 and alerts == []
    clock["now"] = start + dt.timedelta(minutes=40)
    for _ in range(3):
        w.cycle()
    assert usb.resets == ["3-2.1"] * 3                          # no fourth reset
    [(text, data)] = alerts
    assert "after 3 USB resets in the last hour" in text and len(data["resets_last_hour"]) == 3
    assert w.state == "blocked"


def test_reset_can_be_turned_off(rig: Any) -> None:
    usb = Usb()
    w, *_ = rig(usb, usb_reset_after=0)
    for _ in range(5):
        w.cycle()
    assert usb.resets == [] and w.state == "error"
