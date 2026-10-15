"""Receiver hosts and hardware discovery (brief 4): one snapshot on every host, vendor-tool identification from
mocked output, SSH with real host-key checking against a throwaway sshd on 127.0.0.1, and the rules that keep
discovery and identification from disturbing recordings. No physical SDR is opened: device tools are mocks."""

from __future__ import annotations

import getpass
import os
import pathlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import types
from typing import Any

import pytest
from sqlalchemy import inspect, text

from aobs.db import init_db, make_engine
from aobs.db.migrate import current_version, migrate
from aobs.db.schema import SCHEMA_VERSION
from aobs.inventory import adapters, discovery, snapshot
from aobs.inventory.hosts import HostStore, LocalHost, RunResult, SshHost, _guard, classify_ssh_error, opens_device
from aobs.inventory.service import InventoryService
from aobs.inventory.store import InventoryError
from test_inventory import fake_discover, usb
from test_scheduler import env  # noqa: F401

SSHD = shutil.which("sshd") or "/usr/sbin/sshd"

# ------------------------------------------------------------------ mocked tool output (formats of the real tools)
RTL_LIST = """Found 2 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000002
  1:  RTLSDRBlog, Blog V4, SN: 00000000

"""
RTL_TEST_T = RTL_LIST + """Using device 1: Generic RTL2832U OEM
Detached kernel driver
Found Rafael Micro R828D tuner
RTL-SDR Blog V4 Detected
Supported gain values (29): 0.0 0.9 1.4 2.7 3.7 7.7 8.7 12.5 14.4 15.7 16.6 19.7 20.7 22.9 25.4 28.0 29.7 32.8 33.8 36.4 37.2 38.6 40.2 42.1 43.4 43.9 44.5 48.0 49.6
[R82XX] PLL not locked!
No E4000 tuner found, aborting.
Reattached kernel driver
"""
RTL_EEPROM = RTL_LIST + """Using device 1: Generic RTL2832U OEM
Found Rafael Micro R828D tuner

Current configuration:
__________________________________________
Vendor ID:\t\t0x0bda
Product ID:\t\t0x2838
Manufacturer:\t\tRTLSDRBlog
Product:\t\tBlog V4
Serial number:\t\t00000000
Serial number enabled:\tyes
IR endpoint enabled:\tno
Remote wakeup enabled:\tno
__________________________________________
"""
AIRSPY_INFO = """airspy_lib_version: 1.0.11

Found AirSpy board 1
Board ID Number: 0 (AIRSPY)
Firmware Version: AirSpy NOS v1.0.0-rc10-6-g4008185 2020-05-08
Part ID Number: 0x6906002B 0x00000030
Serial Number: 0x0123456789ABCDEF
Supported sample rates:
\t10.000000 MSPS
\t2.500000 MSPS
Close board 1
"""
HACKRF_INFO = """hackrf_info version: 2023.01.1
libhackrf version: 2023.01.1 (0.8)
Found HackRF
Index: 0
Serial number: 0000000000000000a06063c8234e925f
Board ID Number: 2 (HackRF One)
Firmware Version: 2023.01.1 (API:1.07)
Part ID Number: 0xa000cb3c 0x00574f53
Hardware Revision: r9
Hardware supported by installed firmware: HackRF One
"""
SDRPLAY_FIND = """######################################################
##     Soapy SDR -- the SDR abstraction library     ##
######################################################

Found device 0
  driver = sdrplay
  label = SDRplay Dev0 RSP1B 2306000123
  serial = 2306000123

"""
IIO_SCAN = """Library version: 0.25 (git tag: b6028fd)
Compiled with backends: local xml ip usb
Available contexts:
\t0: 0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO)), serial=1044730a199700191f001800f3d1d1ab83 [usb:3.10.5]
"""
LIME_FIND = "  * [LimeSDR Mini, media=USB 3.0, module=FTDI, addr=24607:1027, serial=1D3AC13B4E3A1C]\n"
RTL_PROBE = """Probe device driver=rtlsdr,serial=00000000
-- Device identification
  driver=RTLSDR
  hardware=R828D
  origin=https://github.com/pothosware/SoapyRTLSDR
  rtl=1
  tuner=Rafael Micro R828D
-- Peripheral summary
  Channels: 1 Rx, 0 Tx
  Other Settings:
     * Bias Tee - Enable the 4.5v DC Bias tee to power SAWbird and LNA devices.
       [key=biastee, default=false, type=bool]
-- RX Channel 0
  Supports AGC: YES
  Stream formats: CS8, CS16, CF32
  Native format: CS8 [full-scale=128]
  Antennas: RX
  Full gain range: [0, 49.6] dB
    TUNER gain range: [0, 49.6] dB
  Full freq range: [24, 1766] MHz
    RF freq range: [24, 1766] MHz
  Sample rates: 0.25, 1.024, 1.536, 1.792, 1.92, 2.048, 2.16, 2.56, 2.88, 3.2 MSps
"""


def R(out: str = "", rc: int = 0, err: str = "") -> RunResult:
    return RunResult(rc, out, err)


# ------------------------------------------------------------------ parsers
def test_vendor_tool_parsers_read_only_what_the_tools_print():
    t = adapters.parse_rtl_test(RTL_TEST_T)
    assert [d["serial"] for d in t["devices"]] == ["00000002", "00000000"] and t["using"] == 1
    assert t["tuner"] == "Rafael Micro R828D" and len(t["gains_db"]) == 29 and t["gains_db"][-1] == 49.6
    assert t["variant"] == "RTL-SDR Blog V4"
    e = adapters.parse_rtl_eeprom(RTL_EEPROM)
    assert e["serial"] == "00000000" and e["vendor_id"] == "0x0bda" and e["manufacturer"] == "RTLSDRBlog"
    a = adapters.parse_airspy_info(AIRSPY_INFO)[0]
    assert a["serial"] == "0123456789ABCDEF" and a["firmware"].startswith("AirSpy NOS v1.0.0-rc10")
    assert a["sample_rates_hz"] == [10_000_000, 2_500_000]
    h = adapters.parse_hackrf_info(HACKRF_INFO)[0]
    assert h["serial"].endswith("a06063c8234e925f") and h["board_id"] == "2 (HackRF One)"
    assert h["firmware"] == "2023.01.1 (API:1.07)" and h["hardware_revision"] == "r9"
    assert adapters.parse_soapy_find(SDRPLAY_FIND) == [{"driver": "sdrplay", "label": "SDRplay Dev0 RSP1B 2306000123",
                                                       "serial": "2306000123"}]
    assert adapters.parse_iio_scan(IIO_SCAN)[0]["uri"] == "usb:3.10.5"
    assert adapters.parse_lime_find(LIME_FIND)[0] == {"name": "LimeSDR Mini", "media": "USB 3.0", "module": "FTDI",
                                                     "addr": "24607:1027", "serial": "1D3AC13B4E3A1C"}


def _rtl_dev(serial: str = "00000000") -> dict[str, Any]:
    return {"driver": "rtlsdr", "soapy_driver": None, "serial": serial}


def test_rtl_identify_opens_by_index_never_by_a_numeric_serial():
    """librtlsdr reads -d 00000000 as index 0: that would open the *other* receiver. Starlab uses the index."""
    calls: list[list[str]] = []

    def run(argv, input=None, timeout=60):
        calls.append(argv)
        if argv[:2] == ["rtl_test", "-d"] and argv[2] == adapters.NO_MATCH:
            return R(err=RTL_LIST + "No matching devices found.\n", rc=1)
        if argv == ["rtl_test", "-d", "1", "-t"]:
            return R(err=RTL_TEST_T)
        if argv == ["rtl_eeprom", "-d", "1"]:
            return R(err=RTL_EEPROM)
        raise AssertionError(argv)

    res = adapters.identify(run, _rtl_dev(), {"rtl_test": "/x", "rtl_eeprom": "/x"}, None)
    assert res["ok"], res
    assert all("00000000" not in a for a in calls), calls
    assert res["fields"]["tuner"] == {"value": "Rafael Micro R828D", "source": "rtl_test"}
    assert res["fields"]["eeprom"]["value"]["serial_enabled"] == "yes"
    assert "frequency_hz" not in res["fields"]                       # rtl_test does not print one: none invented
    assert "bias_tee" not in res["fields"]                           # rtl_biast is not installed on this "host"


def test_rtl_identify_discards_output_for_another_device():
    def run(argv, input=None, timeout=60):
        if argv[2] == adapters.NO_MATCH:
            return R(err=RTL_LIST)
        return R(err=RTL_TEST_T.replace("Using device 1", "Using device 0"))
    res = adapters.identify(run, _rtl_dev(), {"rtl_test": "/x"}, None)
    assert not res["ok"] and "tuner" not in res["fields"] and any("did not confirm" in p for p in res["problems"])


def test_duplicate_serials_in_the_rtl_list_open_nothing():
    calls = []

    def run(argv, input=None, timeout=60):
        calls.append(argv)
        return R(err=RTL_LIST.replace("00000002", "00000000"))
    res = adapters.identify(run, _rtl_dev(), {"rtl_test": "/x"}, None)
    assert len(calls) == 1 and "lists 2 devices" in res["problems"][0]


def test_identify_reports_soapy_capabilities_with_their_source_and_bias_tee_only_when_the_driver_has_it():
    from aobs.inventory import probe
    p = probe.run_probe("hackrf", "a06063c8234e925f", run=lambda *a, **k: R(RTL_PROBE.replace("rtlsdr", "hackrf")))
    res = adapters.identify(lambda argv, **k: R(HACKRF_INFO), {"driver": "soapy", "soapy_driver": "hackrf",
                            "serial": "0000000000000000a06063c8234e925f"}, {"hackrf_info": "/x", "SoapySDRUtil": "/x"},
                            lambda d, s: p)
    f = res["fields"]
    assert f["firmware"]["source"] == "hackrf_info" and f["hardware_revision"]["value"] == "r9"
    assert f["frequency_hz"]["value"] == [24_000_000, 1_766_000_000] and f["frequency_hz"]["source"].startswith("SoapySDR")
    assert f["bias_tee"]["value"] == "driver setting 'biastee'"
    skipped = adapters.identify(lambda argv, **k: pytest.fail("must not run"), {"driver": "soapy", "soapy_driver": "hackrf",
                                "serial": "x"}, {"hackrf_info": "/x"}, None, skip={"hackrf_info": "another is busy"})
    assert skipped["steps"][0] == {"tool": "hackrf_info", "skipped": "another is busy"}


def test_sdrplay_identify_only_asks_the_api_for_its_list():
    seen = []
    res = adapters.identify(lambda argv, **k: seen.append(argv) or R(SDRPLAY_FIND),
                            {"driver": "sdrplay", "soapy_driver": "sdrplay", "serial": "2306000123"},
                            {"SoapySDRUtil": "/x"}, lambda d, s: pytest.fail("no probe for SDRplay"))
    assert seen == [["SoapySDRUtil", "--find=driver=sdrplay"]] and res["fields"]["model"]["value"] == "RSP1B"


# ------------------------------------------------------------------ the hardware guard
def test_device_tools_are_refused_in_tests_and_development_unless_mocked(tmp_path, monkeypatch):
    assert opens_device(["airspy_info"]) and opens_device(["SoapySDRUtil", "--probe=driver=airspy"])
    assert not opens_device(["SoapySDRUtil", "--info"]) and not opens_device(["sh", "-s"])
    for env_var in ("STARLAB_DEV", "STARLAB_NO_HARDWARE"):                # even with neither set: pytest is enough
        monkeypatch.delenv(env_var, raising=False)
    r = LocalHost().run(["airspy_info"])
    assert r.returncode == 126 and r.error_kind == "blocked" and "pytest" in r.error
    assert LocalHost().run(["rtl_test", "-t"]).error_kind == "blocked"
    ssh = SshHost("h", "127.0.0.1", 1, "u", tmp_path / "k", tmp_path / "kh")
    assert ssh.run(["hackrf_info"]).error_kind == "blocked"             # never reaches ssh
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "airspy_info").write_text("#!/bin/sh\necho mocked\n")
    (bin_ / "airspy_info").chmod(0o755)
    assert LocalHost(str(bin_)).run(["airspy_info"]).stdout == "mocked\n"
    assert LocalHost(str(bin_)).run(["hackrf_info"]).error_kind == "blocked"   # no mock: never the real one
    assert _guard(["hackrf_info", "-x"], "/mock", local=False) == (["/mock/hackrf_info", "-x"], None)  # remote: mock path


# ------------------------------------------------------------------ one snapshot for every host
def _fixture(tmp: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    root = tmp / "sysfs"
    usb(root, "1-1", "0bda", "2838", "00000002", "Realtek", "RTL2838UHIDIR", 2)
    usb(root, "1-2", "0bda", "2838", "00000000", "RTLSDRBlog", "Blog V4", 3)
    usb(root, "1-3", "1d50", "6089", "0000000000000000a06063c8234e925f", "Great Scott Gadgets", "HackRF One", 4,
        iface_driver="usbfs")
    usb(root, "1-4", "046d", "c548", "", "Logitech", "USB Receiver", 9)
    proc = tmp / "proc"
    (proc / "777" / "fd").mkdir(parents=True)
    (proc / "777" / "comm").write_text("hackrf_transfer\n")
    (proc / "777" / "fd" / "5").symlink_to("/dev/bus/usb/005/004")
    return root, proc


def test_snapshot_script_sees_what_the_python_reader_sees(tmp_path):
    root, proc = _fixture(tmp_path)
    snap = snapshot.take(LocalHost(), root, proc)
    key = lambda d: d.usb_path  # noqa: E731
    assert sorted(discovery.receiver_like(snap.devices), key=key) == sorted(discovery.scan_usb(root), key=key)
    assert snap.holders["/dev/bus/usb/005/004"][0].comm == "hackrf_transfer" and snap.complete
    found = {d.device.usb_path: d for d in discovery.discover_snapshot(snap)}
    assert set(found) == {"1-1", "1-2", "1-3"}                         # the Logitech receiver is not a radio
    assert found["1-3"].usage["claimed"] and not found["1-3"].usage["free"]
    with pytest.raises(ValueError, match="not a Starlab hardware snapshot"):
        snapshot.parse("Welcome to host!\nstarlab-snapshot\t1\n", "h")
    with pytest.raises(ValueError, match="cut off"):
        snapshot.parse("starlab-snapshot\t1\nhostname\tx\n", "h")


def test_snapshot_script_reads_and_never_opens_device_nodes():
    s = snapshot.SCRIPT
    assert "/dev/bus/usb" in s and "readlink" in s
    for opener in ("exec 3<", "< /dev/", "<\"/dev", "dd ", "cat /dev", "> /dev/bus"):
        assert opener not in s


# ------------------------------------------------------------------ identities
def test_devices_without_serials_can_be_two_receivers_but_duplicate_serials_cannot(env, tmp_path):
    root = tmp_path / "sysfs"
    usb(root, "2-1", "0bda", "2838", "", "Realtek", "RTL2838UHIDIR", 2)
    usb(root, "2-2", "0bda", "2838", "", "Realtek", "RTL2838UHIDIR", 3)
    usb(root, "2-3", "0bda", "2838", "SAME", "Realtek", "RTL2838UHIDIR", 4)
    usb(root, "2-4", "0bda", "2838", "SAME", "Realtek", "RTL2838UHIDIR", 5)
    svc = InventoryService(env.s, discover=fake_discover(), sysfs=root)
    a = svc.add({"usb_path": "2-1", "id": "NOSN-1"}, "owner")
    b = svc.add({"usb_path": "2-2", "id": "NOSN-2"}, "owner")
    assert a["identity"] == b["identity"] == "usb_path" and a["serial"] == b["serial"] == ""
    with pytest.raises(InventoryError, match="already receiver NOSN-1"):
        svc.add({"usb_path": "2-1", "id": "NOSN-3"}, "owner")
    with pytest.raises(InventoryError, match="reports serial SAME.*rtl_eeprom -s"):
        svc.add({"usb_path": "2-3", "id": "DUP-1"}, "owner")
    with pytest.raises(InventoryError, match="cannot be told which one to open"):
        svc.identify("2-3", "owner")
    assert discovery.find_device("", root, usb_path="2-9") is None   # a port lookup never falls back to a serial


def test_migration_5_rebuilds_receivers_with_identity_keys(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'db.sqlite3'}")
    init_db(e)
    with e.begin() as conn:                                            # wind back to schema 4's receivers table
        conn.execute(text("DROP TABLE inv_receivers"))
        conn.execute(text("DROP TABLE inv_hosts"))
        conn.execute(text(
            "CREATE TABLE inv_receivers (id VARCHAR(32) PRIMARY KEY, name VARCHAR(120) NOT NULL, host VARCHAR(64) NOT NULL,"
            " driver VARCHAR(16) NOT NULL, serial VARCHAR(64) NOT NULL, identity VARCHAR(16) NOT NULL, usb_vid VARCHAR(4),"
            " usb_pid VARCHAR(4), usb_path VARCHAR(64), manufacturer VARCHAR(128), model VARCHAR(128), origin VARCHAR(8)"
            " NOT NULL, enabled BOOLEAN NOT NULL, config TEXT NOT NULL, capabilities TEXT, readiness VARCHAR(12) NOT NULL,"
            " readiness_detail TEXT, revision INTEGER NOT NULL, updated_by VARCHAR(128), created_at DATETIME NOT NULL,"
            " updated_at DATETIME NOT NULL, CONSTRAINT uq_inv_receivers_identity UNIQUE (host, driver, serial))"))
        for rid, serial, ident, port in (("A", "S1", "serial", "1-1"), ("B", "", "usb_path", "1-2")):
            conn.execute(text("INSERT INTO inv_receivers VALUES (:i, :i, 'local', 'rtlsdr', :s, :d, NULL, NULL, :p, NULL,"
                              " NULL, 'ui', 1, '{}', NULL, 'verified', '{\"x\": 1}', 3, 'o', '2026-10-01 00:00:00',"
                              " '2026-10-01 00:00:00')"), {"i": rid, "s": serial, "d": ident, "p": port})
        conn.execute(text("UPDATE schema_info SET value='4' WHERE key='schema_version'"))
    out = migrate(e)
    assert out["steps"][0] == "5: receiver hosts and identity keys" and current_version(e) == SCHEMA_VERSION
    with e.connect() as conn:
        rows = {r.id: r for r in conn.execute(text("SELECT * FROM inv_receivers"))}
    assert rows["A"].identity_key == "sn:S1" and rows["B"].identity_key == "port:1-2"
    assert rows["A"].readiness == "verified" and rows["A"].revision == 3   # copied as they were
    assert "inv_hosts" in inspect(e).get_table_names() and "inv_receivers_v4" not in inspect(e).get_table_names()
    assert pathlib.Path(out["backup"]).exists()
    assert migrate(e)["steps"] == []                                      # second run: nothing to do


# ------------------------------------------------------------------ real SSH against a throwaway sshd
MOCK_TOOLS = {
    "rtl_test": f"""#!/bin/sh
echo "rtl_test $*" >> "$(dirname "$0")/calls.log"
if [ "$2" = "{adapters.NO_MATCH}" ]; then cat >&2 <<'X'
{RTL_LIST}No matching devices found.
X
exit 1; fi
cat >&2 <<'X'
{RTL_TEST_T}X
""",
    "rtl_eeprom": f"""#!/bin/sh
echo "rtl_eeprom $*" >> "$(dirname "$0")/calls.log"
cat >&2 <<'X'
{RTL_EEPROM}X
""",
    "SoapySDRUtil": f"""#!/bin/sh
echo "SoapySDRUtil $*" >> "$(dirname "$0")/calls.log"
case "$1" in
  --info) echo "Available factories... airspy, hackrf, rtlsdr" ;;
  --probe=*) cat <<'X'
{RTL_PROBE}X
;;
esac
""",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sshd:
    """An unprivileged sshd on 127.0.0.1 with its own host key and authorized_keys. Stopped by its own handle."""

    def __init__(self, d: pathlib.Path):
        self.d, self.port = d, _free_port()
        d.mkdir(parents=True, exist_ok=True)
        self.auth = d / "authorized_keys"
        self.auth.write_text("")
        self.proc: subprocess.Popen[bytes] | None = None
        self.new_host_key()

    def new_host_key(self) -> None:
        for f in (self.d / "hostkey", self.d / "hostkey.pub"):
            f.unlink(missing_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.d / "hostkey")], check=True)
        self.fingerprint = subprocess.run(["ssh-keygen", "-lf", str(self.d / "hostkey.pub")], capture_output=True,
                                          text=True, check=True).stdout.split()[1]

    def start(self) -> None:
        cfg = self.d / "sshd_config"
        cfg.write_text(f"Port {self.port}\nListenAddress 127.0.0.1\nHostKey {self.d / 'hostkey'}\n"
                       f"AuthorizedKeysFile {self.auth}\nPidFile {self.d / 'sshd.pid'}\nUsePAM no\nStrictModes no\n"
                       "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
                       "PerSourcePenalties no\n")                 # the tests fail logins on purpose
        self.proc = subprocess.Popen([SSHD, "-D", "-e", "-f", str(cfg)], stdout=subprocess.DEVNULL,
                                     stderr=open(self.d / "sshd.log", "ab"))
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError("sshd did not start: " + (self.d / "sshd.log").read_text()[-500:])

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)


@pytest.fixture
def remote(env, tmp_path):
    if not os.access(SSHD, os.X_OK) or not shutil.which("ssh"):
        pytest.skip("OpenSSH server/client not installed")
    sshd = Sshd(tmp_path / "sshd")
    sshd.start()
    bin_ = tmp_path / "remote-bin"
    bin_.mkdir()
    for name, body in MOCK_TOOLS.items():
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    root, proc = _fixture(tmp_path / "remote-hw")
    store = HostStore(env.s.engine, tmp_path / "secrets", tmp_path / "run", clock=env.s.clock, path_prefix=str(bin_))
    svc = InventoryService(env.s, discover=fake_discover(), sysfs=tmp_path / "no-local-usb", hosts=store,
                           remote_paths={"pi": (str(root), str(proc))})
    yield types.SimpleNamespace(sshd=sshd, store=store, svc=svc, bin=bin_, user=getpass.getuser(), root=root, proc=proc)
    sshd.stop()


def _authorize(r: Any) -> None:
    r.sshd.auth.write_text(r.store.client_key()["public_key"] + "\n")


def test_host_is_saved_only_after_its_key_is_accepted_and_a_test_connection_works(remote):
    r = remote
    key = r.store.client_key()
    assert key["public_key"].startswith("ssh-ed25519 ") and key["fingerprint"].startswith("SHA256:")
    assert oct((r.store.secrets_dir / "ssh" / "starlab_ed25519").stat().st_mode & 0o777) == "0o600"
    scan = r.store.scan({"hostname": "127.0.0.1", "port": r.sshd.port})
    assert [k["fingerprint"] for k in scan["keys"]] == [r.sshd.fingerprint]
    body = {"id": "pi", "name": "Roof Pi", "hostname": "127.0.0.1", "port": r.sshd.port, "username": r.user}
    with pytest.raises(InventoryError, match="accept at least one host key"):
        r.store.add(body, "owner")
    with pytest.raises(InventoryError, match="not offering any key you accepted"):
        r.store.add({**body, "accept_fingerprints": ["SHA256:" + "A" * 43]}, "owner")
    with pytest.raises(InventoryError, match="does not take or store SSH passwords"):
        r.store.add({**body, "password": "hunter2", "accept_fingerprints": [r.sshd.fingerprint]}, "owner")
    with pytest.raises(InventoryError, match="refused Starlab's SSH key.*Nothing was saved"):
        r.store.add({**body, "accept_fingerprints": [r.sshd.fingerprint]}, "owner")   # key not authorized yet
    assert r.store.rows() == []
    _authorize(r)
    saved = r.store.add({**body, "accept_fingerprints": [r.sshd.fingerprint]}, "owner")
    assert saved["test"]["ok"] and saved["test"]["hostname"] == socket.gethostname()
    assert saved["fingerprints"] == [{"type": "ED25519", "fingerprint": r.sshd.fingerprint}]
    con = sqlite3.connect(env_db(r))
    dump = "\n".join(con.iterdump())
    assert "hunter2" not in dump and "PRIVATE KEY" not in dump            # no password, no private key stored
    assert r.store.test("pi", "owner")["ok"]


def env_db(r: Any) -> str:
    return r.svc.s.engine.url.database


def _add_host(r: Any) -> None:
    _authorize(r)
    r.store.add({"id": "pi", "name": "Roof Pi", "hostname": "127.0.0.1", "port": r.sshd.port, "username": r.user,
                 "accept_fingerprints": [r.sshd.fingerprint]}, "owner")


def test_remote_discovery_is_the_same_snapshot_over_ssh(remote):
    r = remote
    _add_host(r)
    res = r.svc.discover_host("pi", "owner")
    assert res["ok"] and res["count"] == 3
    o = r.svc.overview()
    remote_items = {d["usb_path"]: d for d in o["discovered"] if d["host"] == "pi"}
    assert set(remote_items) == {"1-1", "1-2", "1-3"} and remote_items["1-2"]["connection"] == "usb, over ssh"
    assert remote_items["1-3"]["usage"]["detail"].startswith("in use")
    assert remote_items["1-1"]["support"]["status"] == "supported"        # rtl_test etc. found on the remote PATH
    host = next(h for h in o["hosts"] if h["id"] == "pi")
    assert host["discovery"]["summary"]["tools"]["rtl_test"].startswith(str(r.bin))


def test_remote_identify_runs_the_vendor_tools_there_and_refuses_busy_devices(remote):
    r = remote
    _add_host(r)
    r.svc.discover_host("pi", "owner")
    res = r.svc.identify("1-2", "owner", host="pi")
    assert res["ok"], res
    assert res["fields"]["tuner"]["value"] == "Rafael Micro R828D"
    assert res["fields"]["frequency_hz"]["value"] == [24_000_000, 1_766_000_000]
    calls = (r.bin / "calls.log").read_text().splitlines()
    assert "rtl_test -d 1 -t" in calls and "rtl_eeprom -d 1" in calls and not any("-d 00000000" in c for c in calls)
    assert "SoapySDRUtil --probe=driver=rtlsdr,serial=00000000" in calls
    with pytest.raises(InventoryError, match="identifying opens the device: it is in use"):
        r.svc.identify("1-3", "owner", host="pi")                           # the HackRF is claimed by hackrf_transfer
    row = r.svc.add({"usb_path": "1-2", "host": "pi", "id": "PI-R1", "name": "Pi RTL"}, "owner")
    assert row["host"] == "pi" and row["readiness"] == "untested" and row["identification"]["fields"]["tuner"]
    assert "PI-R1" not in r.svc.s.receivers                                 # the scheduler never sees remote receivers
    entry = next(x for x in r.svc.overview()["receivers"] if x["id"] == "PI-R1")
    assert entry["remote"] and entry["test_kinds"] == [] and "does not record" in entry["remote_note"]
    with pytest.raises(InventoryError, match="receivers PI-R1 are configured on pi"):
        r.store.delete("pi", r.store.get("pi")["revision"], "owner")


def test_a_changed_host_key_stops_every_connection_until_the_owner_accepts_the_new_one(remote):
    r = remote
    _add_host(r)
    r.sshd.stop()
    old = r.sshd.fingerprint
    r.sshd.new_host_key()                                                   # reinstalled, or someone in the middle
    r.sshd.start()
    t = r.store.test("pi", "owner")
    assert not t["ok"] and t["kind"] == "host_key_changed" and "Nothing was run" in t["error"]
    d = r.svc.discover_host("pi", "owner")
    assert not d["ok"] and "not the one you accepted" in d["error"]
    with pytest.raises(InventoryError, match="not offering any key you accepted"):
        r.store.accept_key("pi", {"revision": r.store.get("pi")["revision"], "accept_fingerprints": [old]}, "owner")
    out = r.store.accept_key("pi", {"revision": r.store.get("pi")["revision"],
                                    "accept_fingerprints": [r.sshd.fingerprint]}, "owner")
    assert out["test"]["ok"] and r.svc.discover_host("pi", "owner")["ok"]
    events = [e["action"] for e in r.svc.s.inventory.events("host:pi")]
    assert "host_key_replaced" in events and "host_test_failed" in events


def test_ssh_errors_are_explained():
    assert classify_ssh_error("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@")[0] == "host_key_changed"
    assert classify_ssh_error("Host key verification failed.")[0] == "host_key_unknown"
    assert classify_ssh_error("u@h: Permission denied (publickey).")[0] == "auth_failed"
    assert classify_ssh_error("connect to host h port 22: Connection refused")[0] == "refused"
    assert classify_ssh_error("ssh: connect to host 10.0.0.9 port 22: Connection timed out")[0] == "timeout"
    assert classify_ssh_error("Connection closed by 127.0.0.1 port 2222")[0] == "closed"     # sshd penalties
