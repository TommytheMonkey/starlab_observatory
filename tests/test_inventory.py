"""Receiver inventory: migration, TOML import, passive discovery, identify, add, readiness from real IQ."""

from __future__ import annotations

import datetime as dt
import functools
import json
import pathlib
import sqlite3
from typing import Any

import numpy as np
import pytest
from sqlalchemy import inspect, select, text

from aobs.db import init_db, make_engine
from aobs.db.migrate import current_version
from aobs.db.schema import SCHEMA_VERSION, inv_receivers, metadata
from aobs.inventory import discovery, iqcheck, probe
from aobs.inventory.service import InventoryService
from aobs.inventory.store import InventoryError, effective_readiness
from aobs.scheduling.store import SchedulerError
from test_scheduler import FakeHandle, FakeRunnerHw, env, rtl_job, fix_dirs  # noqa: F401
from test_testlab import lab, run_test  # noqa: F401

AIRSPY_SERIAL = "0123456789ABCDEF"          # not the station's real Airspy: tests never touch real hardware

AIRSPY_PROBE = """######################################################
##     Soapy SDR -- the SDR abstraction library     ##
######################################################

Probe device driver=airspy,serial=123456789abcdef
----------------------------------------------------
-- Device identification
----------------------------------------------------
  driver=Airspy
  hardware=Airspy
  serial=123456789abcdef

----------------------------------------------------
-- Peripheral summary
----------------------------------------------------
  Channels: 1 Rx, 0 Tx
  Timestamps: NO
  Other Settings:
     * Bias tee - Enable the 4.5v DC Bias tee to power SAWbird and LNA devices.
       [key=biastee, default=false, type=bool]
     * Bit pack - Enable packing 4 12-bit samples into 3 16-bit words for 25% less USB trafic.
       [key=bitpack, default=false, type=bool]

----------------------------------------------------
-- RX Channel 0
----------------------------------------------------
  Full-duplex: NO
  Supports AGC: YES
  Stream formats: CS16, CF32
  Native format: CS16 [full-scale=32767]
  Stream args:
     * Buffer Size - Number of bytes per buffer, multiples of 512 only.
       [key=bufflen, units=bytes, default=262144, type=int]
  Antennas: RX
  Full gain range: [0, 45] dB
    LNA gain range: [0, 15] dB
    MIX gain range: [0, 15] dB
    VGA gain range: [0, 15] dB
  Full freq range: [24, 1800] MHz
    RF freq range: [24, 1800] MHz
  Sample rates: 10, 2.5 MSps
"""


# ------------------------------------------------------------------ fake sysfs
def usb(root: pathlib.Path, name: str, vid: str, pid: str, serial: str = "", manufacturer: str = "", product: str = "",
        devnum: int = 4, iface_driver: str | None = None) -> None:
    d = root / name
    d.mkdir(parents=True)
    for k, v in {"idVendor": vid, "idProduct": pid, "serial": serial, "manufacturer": manufacturer,
                 "product": product, "bcdDevice": "0100", "busnum": "5", "devnum": str(devnum)}.items():
        if v or k not in ("serial",):
            (d / k).write_text(v + "\n")
    i = d / f"{name}:1.0"
    i.mkdir()
    if iface_driver:
        target = root / "_drivers" / iface_driver
        target.mkdir(parents=True, exist_ok=True)
        (i / "driver").symlink_to(target)


@pytest.fixture
def sysfs(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "sysfs"
    usb(root, "3-3", "0bda", "2838", "OBS-R02", "Realtek", "RTL2838UHIDIR", 2)
    usb(root, "5-4", "1d50", "60a1", f"AIRSPY SN:{AIRSPY_SERIAL}", "www.airspy.com", "AIRSPY", 5)
    usb(root, "5-3", "1d50", "605b", "009a", "Great Scott Gadgets", "YARD Stick One", 3)
    usb(root, "1-1", "046d", "c548", "", "Logitech", "USB Receiver", 9)                  # not a radio: ignored
    return root


def no_holders(node: str, proc: Any) -> tuple[list[Any], int]:
    return [], 0


def fake_discover(**over: Any) -> Any:
    kw = dict(modules=frozenset({"airspy", "sdrplay", "rtlsdr"}), which=lambda t: f"/usr/bin/{t}", holders=no_holders)
    kw.update(over)
    return functools.partial(discovery.discover, **kw)


# ------------------------------------------------------------------ migration
def test_new_database_is_created_at_the_current_version(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'new.sqlite3'}")
    out = init_db(e)
    assert out["created"] and current_version(e) == SCHEMA_VERSION
    assert {"inv_receivers", "inv_events"} <= set(inspect(e).get_table_names())
    assert not (tmp_path / "backups").exists()                     # nothing to back up


def _v1_database(path: pathlib.Path, receivers_rows: int = 0) -> Any:
    e = make_engine(f"sqlite:///{path}")
    init_db(e)
    with e.begin() as conn:                                          # wind it back to what schema 1 looked like
        conn.execute(text("DROP TABLE inv_receivers"))
        conn.execute(text("DROP TABLE inv_events"))
        conn.execute(text("DROP TABLE settings_docs"))
        conn.execute(text("DROP TABLE rx_sessions"))
        conn.execute(text("CREATE TABLE receivers (id VARCHAR(32) PRIMARY KEY, serial VARCHAR(64))"))
        for i in range(receivers_rows):
            conn.execute(text("INSERT INTO receivers VALUES (:i, :s)"), {"i": f"R{i}", "s": f"S{i}"})
        conn.execute(text("UPDATE schema_info SET value='1' WHERE key='schema_version'"))
    return e


def test_schema_1_database_is_backed_up_then_migrated(tmp_path):
    e = _v1_database(tmp_path / "db" / "obs.sqlite3")
    out = init_db(e)
    assert out["from"] == 1 and out["to"] == SCHEMA_VERSION and out["steps"][0] == "2: receiver inventory"
    backup = pathlib.Path(out["backup"])
    assert backup.parent == tmp_path / "db" / "backups" and f"pre-migration-v1-v{SCHEMA_VERSION}" in backup.name
    with sqlite3.connect(backup) as b:                              # the backup is the untouched schema-1 database
        assert b.execute("select value from schema_info where key='schema_version'").fetchone()[0] == "1"
        assert "receivers" in {r[0] for r in b.execute("select name from sqlite_master where type='table'")}
    names = set(inspect(e).get_table_names())
    assert {"inv_receivers", "inv_events"} <= names and "receivers" not in names
    assert init_db(e)["backup"] is None and current_version(e) == SCHEMA_VERSION      # second start: nothing to do


def test_old_receivers_table_with_rows_is_kept_not_dropped(tmp_path):
    e = _v1_database(tmp_path / "obs.sqlite3", receivers_rows=2)
    init_db(e)
    with e.connect() as conn:
        assert conn.execute(text("select count(*) from receivers_pre_v2")).scalar() == 2


def test_newer_database_is_refused(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'obs.sqlite3'}")
    init_db(e)
    with e.begin() as conn:
        conn.execute(text(f"UPDATE schema_info SET value='{SCHEMA_VERSION + 1}' WHERE key='schema_version'"))
    with pytest.raises(RuntimeError, match="newer than this code"):
        init_db(e)


# ------------------------------------------------------------------ TOML import
def test_toml_receivers_are_imported_in_service_but_untested(env):
    rows = {r["id"]: r for r in env.s.inventory.listing()}
    assert set(rows) == {"OBS-R01", "OBS-R02", "OBS-R05", "RSP1B"}
    r02 = rows["OBS-R02"]
    assert r02["origin"] == "toml" and r02["readiness"] == "untested" and r02["config"]["mode"] == "broker"
    rx = env.s.receivers["OBS-R02"]
    assert rx.in_service and rx.schedulable and rx.readiness == "untested"     # keeps recording as before
    assert env.s.receivers["RSP1B"].serial == "240526D360"
    assert [e["action"] for e in env.s.inventory.events("OBS-R02")] == ["imported"]


def test_toml_changes_follow_and_removed_receivers_are_disabled(env):
    from aobs.scheduling.store import Scheduler
    env.cfg.raw["scheduler"]["receivers"]["OBS-R02"]["mode"] = "shadow"
    del env.cfg.raw["scheduler"]["receivers"]["OBS-R01"]
    s2 = Scheduler(env.cfg, env.s.engine, env.clock, present=lambda rx: True, satdump_pipelines=frozenset())
    assert s2.receivers["OBS-R02"].mode == "shadow" and "OBS-R01" not in s2.receivers
    assert [e["action"] for e in s2.inventory.events("OBS-R01")][:1] == ["disabled"]
    assert Scheduler(env.cfg, env.s.engine, env.clock, present=lambda rx: True,
                     satdump_pipelines=frozenset()).inventory.events("OBS-R02")[0]["action"] == "updated"   # once


# ------------------------------------------------------------------ passive discovery
def test_discovery_identifies_devices_without_opening_them(env, sysfs):
    found = {d.device.usb_path: d.as_dict() for d in fake_discover()(env.s.inventory.listing() and
             [type("R", (), r)() for r in env.s.inventory.rows()], sysfs=sysfs)}
    assert set(found) == {"3-3", "5-4", "5-3"}
    assert found["3-3"]["configured_as"] == "OBS-R02" and found["3-3"]["support"]["status"] == "supported"
    air = found["5-4"]
    assert air["serial"] == AIRSPY_SERIAL and air["driver"] == "airspy" and air["needs_probe"]
    assert air["model"] == "Airspy R2 or Airspy Mini (same USB ID)" and air["configured_as"] is None
    assert air["usage"]["free"]
    yard = found["5-3"]
    assert yard["support"]["status"] == "not_a_receiver" and "does not stream IQ" in yard["support"]["detail"]


def test_discovery_reports_missing_drivers_claims_duplicates_and_missing_serials(tmp_path):
    root = tmp_path / "sys"
    usb(root, "1-1", "0bda", "2838", "SAME", devnum=2)
    usb(root, "1-2", "0bda", "2838", "SAME", devnum=3, iface_driver="usbfs")
    usb(root, "1-3", "0bda", "2838", "", devnum=4)
    usb(root, "1-4", "1d50", "6089", "000000000000000075b068dc317bae07", devnum=5)
    usb(root, "1-5", "1df7", "3010", "1234", devnum=6)
    got = {d.device.usb_path: d for d in fake_discover(modules=frozenset({"rtlsdr"}))([], sysfs=root)}
    assert "rtl_eeprom -s" in got["1-1"].warnings[0] and got["1-2"].usage["claimed"] and not got["1-2"].usage["free"]
    assert "USB port" in got["1-3"].warnings[0]
    assert got["1-4"].support.status == "driver_missing" and "hackrf" in got["1-4"].support.detail
    assert got["1-5"].support.status == "unsupported" and "RSP1A/RSP1B" in got["1-5"].support.detail


def test_soapy_info_and_probe_parsing():
    assert discovery.parse_soapy_info("Available factories... airspy, hackrf, rtlsdr, sdrplay\n") == \
        frozenset({"airspy", "hackrf", "rtlsdr", "sdrplay"})
    caps, problems = probe.capabilities_from_probe(probe.parse_probe(AIRSPY_PROBE))
    assert problems == []
    assert caps["sample_rates_hz"] == [2_500_000, 10_000_000] and caps["frequency_hz"] == [24_000_000, 1_800_000_000]
    assert caps["gains"] == {n: {"min": 0.0, "max": 15.0, "step": None} for n in ("LNA", "MIX", "VGA")}
    assert caps["formats"] == ["cs16", "cf32"] and caps["bias_tee_setting"] == "biastee"
    assert caps["native_format"] == "CS16" and caps["agc"] is True


def test_probe_failure_is_reported_not_guessed():
    class R:
        returncode, stdout, stderr = 1, "", "Error making device: airspy_open() failed -- AIRSPY_ERROR_NOT_FOUND"
    res = probe.run_probe("airspy", AIRSPY_SERIAL, run=lambda *a, **k: R())
    assert not res["ok"] and "could not open" in res["error"] and "capabilities" not in res
    assert not probe.run_probe("airspy", "x;rm -rf /", run=lambda *a, **k: R())["ok"]


# ------------------------------------------------------------------ IQ verification
def _write(path: pathlib.Path, fmt: str, i: np.ndarray, q: np.ndarray) -> pathlib.Path:
    iq = np.empty(2 * len(i))
    iq[0::2], iq[1::2] = i, q
    dtype = {"cu8": np.uint8, "cs16": "<i2", "cf32": "<f4"}[fmt]
    path.write_bytes(iq.astype(dtype).tobytes())
    return path


def test_iq_check_accepts_real_noise_and_rejects_fakes(tmp_path):
    rng = np.random.default_rng(3)
    n, rate = 400_000, 250_000
    ok = iqcheck.verify_iq(_write(tmp_path / "a.cs16", "cs16", rng.normal(0, 300, n), rng.normal(0, 300, n)), "cs16", rate,
                           n * 4)
    assert ok["ok"], ok["problems"]
    assert ok["samples_checked"] > 0 and ok["rms_dbfs"] < -20
    zeros = iqcheck.verify_iq(_write(tmp_path / "z.cs16", "cs16", np.zeros(n), np.zeros(n)), "cs16", rate)
    assert not zeros["ok"] and any("zero" in p for p in zeros["problems"])
    flat = iqcheck.verify_iq(_write(tmp_path / "f.cu8", "cu8", np.full(n, 127.0), np.full(n, 128.0)), "cu8", rate)
    assert not flat["ok"] and any("not real receiver noise" in p for p in flat["problems"])
    same = rng.normal(0, 300, n)
    copy = iqcheck.verify_iq(_write(tmp_path / "c.cs16", "cs16", same, same), "cs16", rate)
    assert not copy["ok"] and any("identical" in p for p in copy["problems"])
    short = iqcheck.verify_iq(tmp_path / "a.cs16", "cs16", rate, expected_bytes=n * 4 * 10)
    assert not short["ok"] and any("expected bytes" in p for p in short["problems"])
    stuck = np.repeat(rng.normal(0, 300, n // 100), 100)
    st = iqcheck.verify_iq(_write(tmp_path / "s.cs16", "cs16", stuck, stuck[::-1]), "cs16", rate)
    assert not st["ok"] and any("repeat" in p for p in st["problems"])
    assert not iqcheck.verify_iq(tmp_path / "missing.cs16", "cs16", rate)["ok"]


# ------------------------------------------------------------------ the web UI's flow
class SoapyHw(FakeRunnerHw):
    """Writes cs16: noise (good=True), or all zeros, like a device that opened but streamed nothing."""

    def __init__(self, good: bool = True) -> None:
        super().__init__()
        self.good = good

    def spawn(self, argv, log):
        run = json.loads(pathlib.Path(argv[-1]).read_text())
        self.calls.append(("spawn", run["serial"], run.get("soapy_driver")))
        n = int(run["radio"]["sample_rate_hz"] * 1)
        rng = np.random.default_rng(5)
        x = rng.normal(0, 400, 2 * n) if self.good else np.zeros(2 * n)
        raw = pathlib.Path(run["raw"])
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(x.astype("<i2").tobytes())
        pathlib.Path(run["result"]).write_text(json.dumps({"status": "recorded", "bytes": raw.stat().st_size,
                                                           "actual_start": run["record_start"]}))
        return FakeHandle(4242)


@pytest.fixture
def inv(env, lab, sysfs):
    probes: list[tuple[str, str]] = []

    def fake_probe(driver: str, serial: str) -> dict[str, Any]:
        probes.append((driver, serial))
        return probe.run_probe(driver, serial, run=lambda *a, **k: type("R", (), {
            "returncode": 0, "stdout": AIRSPY_PROBE, "stderr": ""})())

    svc = InventoryService(env.s, discover=fake_discover(), run_probe=fake_probe, sysfs=sysfs)
    svc.probes = probes
    env.present.add(AIRSPY_SERIAL)
    return svc


def add_airspy(inv: InventoryService, rid: str = "OBS-R06") -> dict[str, Any]:
    inv.identify("5-4", "owner (web)")
    return inv.add({"usb_path": "5-4", "id": rid, "name": "Airspy R2 (roof)"}, "owner (web)")


def test_overview_separates_managed_discovered_and_documentation_devices(env, inv):
    o = inv.overview()
    assert {r["id"] for r in o["receivers"]} == {"OBS-R01", "OBS-R02", "OBS-R05", "RSP1B"}
    disc = {d["usb_path"]: d for d in o["discovered"]}
    assert disc["3-3"]["configured_as"] == "OBS-R02"
    assert disc["5-4"]["configured_as"] is None and disc["5-4"]["support"]["status"] == "supported"
    assert disc["5-4"]["state"] == "discovered" and disc["3-3"]["state"] == "configured"
    assert {r["state"] for r in o["receivers"]} == {"configured"}
    assert disc["5-3"]["support"]["status"] == "not_a_receiver"
    with pytest.raises(InventoryError, match="not an IQ receiver"):
        inv.add({"usb_path": "5-3", "id": "YARD-1"}, "owner (web)")


def test_airspy_must_be_identified_before_it_can_be_added(env, inv):
    with pytest.raises(InventoryError, match="identify the device first"):
        inv.add({"usb_path": "5-4", "id": "OBS-R06"}, "owner (web)")
    res = inv.identify("5-4", "owner (web)")
    assert res["ok"] and res["capabilities"]["source"] == "SoapySDRUtil --probe"
    assert inv.probes == [("airspy", AIRSPY_SERIAL)]


def test_identify_never_opens_a_device_that_is_in_use(env, lab, sysfs):
    busy = InventoryService(env.s, discover=fake_discover(
        holders=lambda node, proc: ([type("H", (), {"pid": 99999, "comm": "airspy_rx", "cmdline": "airspy_rx"})()], 0)),
        run_probe=lambda *a: pytest.fail("the probe must not run"), sysfs=sysfs)
    with pytest.raises(InventoryError, match=r"identifying opens the device: it is in use: open in airspy_rx\[99999\]"):
        busy.identify("5-4", "owner (web)")
    target = sysfs / "_drivers" / "usbfs"                   # a program (perhaps root's) claimed the USB interface
    target.mkdir(parents=True)
    (sysfs / "5-4" / "5-4:1.0" / "driver").symlink_to(target)
    claimed = InventoryService(env.s, discover=fake_discover(), run_probe=lambda *a: pytest.fail("no probe"),
                               sysfs=sysfs)
    with pytest.raises(InventoryError, match="claimed its USB interface"):
        claimed.identify("5-4", "owner (web)")


def test_added_receiver_is_real_untested_and_cannot_record_until_verified(env, inv):
    row = add_airspy(inv)
    assert row["origin"] == "ui" and row["readiness"] == "untested" and row["driver"] == "airspy"
    assert row["state"] == "configured"
    assert row["serial"] == AIRSPY_SERIAL and row["capabilities"]["sample_rates_hz"] == [2_500_000, 10_000_000]
    rx = env.s.receivers["OBS-R06"]
    assert not rx.schedulable and rx.auto == {} and rx.formats() == ("cs16", "cf32")
    assert env.s.rf.current["layout"]["receivers"]["OBS-R06"]["managed"]          # a real lane in the layout
    disc = {d["usb_path"]: d for d in inv.overview()["discovered"]}
    assert disc["5-4"]["configured_as"] == "OBS-R06"
    job = fix_dirs(rtl_job("manual-on-airspy", rx="OBS-R06"), env.tmp)
    job["receiver"].update(driver="airspy", serial=AIRSPY_SERIAL)
    res = env.s.submit([job])["results"][0]
    assert not res["ok"] and any("has not passed a hardware test" in e for e in res["errors"])
    with pytest.raises(InventoryError, match="already receiver OBS-R06"):
        inv.add({"usb_path": "5-4", "id": "OBS-R07"}, "owner (web)")


def test_verification_with_real_samples_makes_the_receiver_ready(env, lab, inv):
    add_airspy(inv)
    lab.runner.hw = SoapyHw(good=True)
    started = run_test(env, lab, {**inv.verify_body(lab.options("OBS-R06")), "receiver_id": "OBS-R06"})
    job = env.s.get(started["job_id"])
    assert job["spec"]["radio"]["gain_elements"] == {"LNA": 8, "MIX": 8, "VGA": 8}
    assert job["spec"]["radio"]["sample_rate_hz"] == 2_500_000 and job["spec"]["recording"]["format"] == "cs16"
    assert ("spawn", AIRSPY_SERIAL, "airspy") in lab.runner.hw.calls
    row = env.s.inventory.public("OBS-R06")
    assert row["readiness"] == "verified" and row["state"] == "verified", row["readiness_detail"]
    det = row["readiness_detail"]
    assert det["test_id"] == started["test_id"] and det["iq"]["samples_checked"] > 0 and det["problems"] == []
    assert env.s.receivers["OBS-R06"].schedulable


def test_a_device_that_opens_but_streams_nothing_fails_readiness(env, lab, inv):
    add_airspy(inv)
    lab.runner.hw = SoapyHw(good=False)
    run_test(env, lab, {**inv.verify_body(lab.options("OBS-R06")), "receiver_id": "OBS-R06"})
    row = env.s.inventory.public("OBS-R06")
    assert row["readiness"] == "failed" and row["state"] == "configured"
    assert any("zero" in p for p in row["readiness_detail"]["problems"])
    assert not env.s.receivers["OBS-R06"].schedulable


def test_readiness_is_bound_to_the_hardware_identity(env, lab, inv):
    add_airspy(inv)
    lab.runner.hw = SoapyHw(good=True)
    run_test(env, lab, {**inv.verify_body(lab.options("OBS-R06")), "receiver_id": "OBS-R06"})
    with env.s.engine.begin() as conn:                          # the same ID now points at a different device
        conn.execute(inv_receivers.update().where(inv_receivers.c.id == "OBS-R06").values(serial="FEDCBA9876543210"))
    row = env.s.inventory.get("OBS-R06")
    assert effective_readiness(row) == ("untested", True)


def test_tests_the_driver_does_not_implement_are_refused(env, lab, inv):
    add_airspy(inv)
    with pytest.raises(SchedulerError, match="Wideband RF Scan is not implemented for airspy"):
        lab.start("OBS-R06", {"test_type": "scan"}, "owner (web)")
    with pytest.raises(SchedulerError, match="gain element LNA must be 0-15"):
        lab.start("OBS-R06", {**inv.verify_body(lab.options("OBS-R06")), "gain_elements": {"LNA": 30, "MIX": 1,
                                                                                         "VGA": 1}}, "owner (web)")
    with pytest.raises(SchedulerError, match="not a sample rate"):
        lab.start("OBS-R06", {**inv.verify_body(lab.options("OBS-R06")), "sample_rate_hz": 2_400_000}, "owner (web)")


def test_test_refused_while_another_program_holds_the_receiver(env, lab, inv):
    add_airspy(inv)
    env.s.usb_usage = lambda rx: {"free": False, "claimed": True, "holders": [], "detail": "in use: a program has "
                                  "claimed its USB interface"} if rx.id == "OBS-R06" else None
    av = lab.availability("OBS-R06", 10)
    assert not av["available"] and "claimed by another program" in av["reasons"][0]
    with pytest.raises(SchedulerError, match="cannot run safely right now"):
        lab.start("OBS-R06", {**inv.verify_body(lab.options("OBS-R06")), "receiver_id": "OBS-R06"}, "owner (web)")


def test_edit_disable_and_remove_keep_history_and_documentation(env, inv):
    row = add_airspy(inv)
    row = inv.update("OBS-R06", {"name": "Airspy (shack)", "notes": "on the window antenna", "revision": row["revision"]},
                     "owner (web)")
    assert row["name"] == "Airspy (shack)" and row["config"]["notes"] == "on the window antenna"
    with pytest.raises(InventoryError, match="changed elsewhere"):
        inv.update("OBS-R06", {"name": "x", "revision": 1}, "owner (web)")
    row = inv.update("OBS-R06", {"enabled": False, "revision": row["revision"]}, "owner (web)")
    assert "OBS-R06" not in env.s.receivers
    with pytest.raises(InventoryError, match="configured in observatory.toml"):
        inv.update("OBS-R02", {"notes": "x", "revision": env.s.inventory.get("OBS-R02")["revision"]}, "owner (web)")
    inv.delete("OBS-R06", row["revision"], "owner (web)")
    lane = env.s.rf.current["layout"]["receivers"]["OBS-R06"]
    assert not lane["managed"] and lane["serial"] == AIRSPY_SERIAL and lane["label"] == "Airspy (shack)"
    assert [e["action"] for e in env.s.inventory.events("OBS-R06")] == \
        ["deleted", "disabled", "updated", "created"]


def test_layout_entry_can_become_a_real_receiver(env, inv):
    lay = json.loads(json.dumps(env.s.rf.current["layout"]))
    lay["receivers"]["EXT-1"] = {"id": "EXT-1", "chain": [], "notes": "drawn earlier", "label": "Airspy R2",
                                 "serial": "0x" + AIRSPY_SERIAL, "hardware": "Airspy R2", "host": "observatory"}
    env.s.rf.save(lay, env.s.rf.current["version"], "owner (web)")
    inv.identify("5-4", "owner (web)")
    with pytest.raises(InventoryError, match="already used in the signal-path layout"):
        inv.add({"usb_path": "5-4", "id": "EXT-1"}, "owner (web)")
    inv.add({"usb_path": "5-4", "id": "EXT-1", "from_layout": True}, "owner (web)")
    lane = env.s.rf.current["layout"]["receivers"]["EXT-1"]
    assert lane["managed"] and lane["notes"] == "drawn earlier"
