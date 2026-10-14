from __future__ import annotations

import datetime as dt
import io
import subprocess
from typing import Any

import pytest

from aobs.config import Config
from aobs.coordination import radio
from aobs.coordination.broker import Broker, BrokerError

UTC = dt.timezone.utc

RTL_POWER_CSV = (
    "2026-10-12, 18:00:00, 144000000, 144100000, 25000.00, 100, -40.0, -40.5, -20.0, -41.0\n"
    "2026-10-12, 18:00:00, 144100000, 144200000, 25000.00, 100, -40.2, -39.8, -40.1, -12.0\n"
)


class FakeProc:
    def __init__(self, out: Any, payload: bytes):
        if out not in (None, subprocess.DEVNULL):
            out.write(payload)
        self.returncode = 0
        self.stderr = io.BytesIO(b"")

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int:
        return 0

    def terminate(self) -> None:
        pass


class FakeHw:
    def __init__(self) -> None:
        self.port = 7
        self.adsb = True
        self.calls: list[str] = []
        self.index = 0
        self.coord: dict[str, str] = {}
        self.payload = RTL_POWER_CSV.encode()

    def switch_state(self) -> dict[str, Any]:
        return {"port": self.port, "healthy": True}

    def switch_select(self, port: int) -> None:
        self.calls.append(f"select {port}")
        self.port = port

    def adsb_active(self) -> bool:
        return self.adsb

    def adsb_stop(self) -> None:
        self.calls.append("adsb stop")
        self.adsb = False

    def adsb_start(self, device_index: int) -> None:
        self.calls.append(f"adsb start {device_index}")
        self.adsb = True

    def device_index(self, serial: str) -> int | None:
        return self.index

    def device_busy(self, serial: str) -> list[str]:
        return ["observatory-adsb.service"] if self.adsb else []

    def coordinator_reserve(self, receiver: str, owner: str, ttl_s: int, purpose: str) -> str:
        self.coord[receiver] = owner
        return "coord-token"

    def coordinator_release(self, receiver: str, token: str) -> None:
        self.coord.pop(receiver, None)

    def popen(self, cmd: list[str], stdout: Any) -> FakeProc:
        self.calls.append(cmd[0])
        return FakeProc(stdout, self.payload)


class Clock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 10, 12, 18, 0, tzinfo=UTC)

    def __call__(self) -> dt.datetime:
        return self.now


@pytest.fixture
def setup(tmp_config: Config) -> tuple[Broker, FakeHw, Clock]:
    tmp_config.raw["antenna_switch"].update(receiver="OBS-R05", ports=[
        {"port": 1, "code": 0, "antenna": "V-DIPOLE"},
        {"port": 6, "code": 5, "antenna": "ADS Airband"},
        {"port": 7, "code": 6, "antenna": "SPRING-XTRA-TALL"},
    ])
    tmp_config.raw["broker"] = {"receivers": ["OBS-R05"]}
    hw, clock = FakeHw(), Clock()
    return Broker(tmp_config, hw, clock), hw, clock


def test_lease_pauses_adsb_and_release_restores_everything(setup):
    broker, hw, _ = setup
    lease = broker.lease("agent", "scan 2m", port=1)
    assert hw.calls == ["adsb stop", "select 1"]
    assert hw.coord == {"OBS-R05": "aobs-broker:agent"}
    assert "coord_token" not in lease
    broker.release(lease["token"])
    assert hw.calls[-2:] == ["select 7", "adsb start 0"]
    assert hw.coord == {} and hw.adsb and hw.port == 7


def test_adsb_restarts_on_rr05s_current_index(setup):
    broker, hw, _ = setup
    lease = broker.lease("agent")
    hw.index = 1  # helix dongle came back and enumerates first
    broker.release(lease["token"])
    assert hw.calls[-1] == "adsb start 1"


def test_adsb_left_alone_if_it_was_not_running(setup):
    broker, hw, _ = setup
    hw.adsb = False
    broker.release(broker.lease("agent")["token"])
    assert not any("adsb" in c for c in hw.calls)


def test_second_owner_is_refused_and_same_owner_renews(setup):
    broker, _, _ = setup
    first = broker.lease("agent")
    with pytest.raises(BrokerError):
        broker.lease("someone-else")
    assert broker.lease("agent")["token"] == first["token"]


def test_unlisted_receiver_and_empty_port_refused(setup):
    broker, _, _ = setup
    with pytest.raises(BrokerError):
        broker.lease("agent", receiver="OBS-R02")
    with pytest.raises(BrokerError):
        broker.lease("agent", port=4)


def test_expiry_releases_and_restores(setup):
    broker, hw, clock = setup
    broker.lease("agent", ttl_s=60, port=1)
    clock.now += dt.timedelta(seconds=61)
    assert broker.expire() == ["OBS-R05"]
    assert hw.adsb and hw.port == 7


def test_recover_after_crash(setup, tmp_config):
    broker, hw, clock = setup
    broker.lease("agent", port=1)
    assert not hw.adsb
    fresh = Broker(tmp_config, hw, clock)  # new process, same state file
    assert fresh.recover() == ["OBS-R05"]
    assert hw.adsb and hw.port == 7 and hw.coord == {}


def test_failed_setup_rolls_back(setup):
    broker, hw, _ = setup

    def boom(port: int) -> None:
        raise RuntimeError("switch offline")
    hw.switch_select = boom
    with pytest.raises(RuntimeError):
        broker.lease("agent", port=1)
    assert hw.adsb and hw.coord == {} and broker.state()["leases"] == {}


def test_sweep_finds_peaks(setup):
    broker, hw, _ = setup
    token = broker.lease("agent")["token"]
    result = broker.sweep(token, radio.Sweep(144_000_000, 144_200_000, 25_000, seconds=2))
    assert result["bins"] == 8
    assert [p["center_hz"] for p in result["peaks"]] == [144_187_500, 144_062_500]
    assert broker.capture_file(result["file"]).exists()


def test_audio_capture_writes_wav(setup):
    broker, hw, _ = setup
    hw.payload = b"\x00\x01" * radio.AUDIO_RATE * 2
    token = broker.lease("agent")["token"]
    result = broker.audio(token, radio.AudioCapture(146_520_000, seconds=2))
    assert result["seconds"] == 2.0
    assert broker.capture_file(result["file"]).read_bytes()[:4] == b"RIFF"


def test_capture_file_rejects_traversal(setup):
    broker, _, _ = setup
    with pytest.raises(BrokerError):
        broker.capture_file("../secrets/broker_token")


def test_radio_validation():
    with pytest.raises(radio.RadioError):
        radio.Sweep(10_000_000, 20_000_000, 10_000).validate()      # HF without direct sampling
    radio.Sweep(3_000_000, 8_000_000, 10_000, direct=True).validate()
    with pytest.raises(radio.RadioError):
        radio.Sweep(100_000_000, 1_000_000_000, 1_000).validate()   # too many bins
    with pytest.raises(radio.RadioError):
        radio.AudioCapture(146_520_000, mode="cw").validate()
    assert radio.AudioCapture(7_200_000, mode="lsb", direct=True).command("OBS-R05")[-3:] == ["-E", "direct2", "-"]


def test_audio_bandwidth_gain_ppm_flags():
    spec = radio.AudioCapture(127_850_000, mode="am", gain_db=28.0, bandwidth_hz=10_000, ppm=2, squelch=150)
    spec.validate()
    cmd = spec.command("OBS-R05")
    assert cmd[cmd.index("-s") + 1] == "10000" and cmd[cmd.index("-g") + 1] == "28"
    assert cmd[cmd.index("-p") + 1] == "2" and cmd[cmd.index("-l") + 1] == "150"
    assert spec.wav_rate == 10_000
    assert radio.AudioCapture(98_100_000, mode="wfm", bandwidth_hz=10_000).wav_rate == 32_000  # wfm ignores it
    with pytest.raises(radio.RadioError):
        radio.AudioCapture(146_520_000, gain_db=60).validate()
    with pytest.raises(radio.RadioError):
        radio.AudioCapture(146_520_000, bandwidth_hz=200_000).validate()


def test_squelched_silence_is_not_an_error(setup):
    broker, hw, _ = setup
    hw.payload = b""

    class Quiet(FakeProc):
        def __init__(self, out, payload):
            super().__init__(out, payload)
            self.stderr = io.BytesIO(b"Tuned to 146802344 Hz.\n")
    hw.popen = lambda cmd, stdout: Quiet(stdout, b"")
    token = broker.lease("agent")["token"]
    result = broker.audio(token, radio.AudioCapture(146_520_000, seconds=2, squelch=200))
    assert result["squelch_open"] is False and result["file"] is None


def test_sweep_crop_flag():
    cmd = radio.Sweep(118_000_000, 137_000_000, 25_000, crop=0.3, gain_db=20.7).command("OBS-R05")
    assert "30%" in cmd and cmd[cmd.index("-g") + 1] == "20.7"
