from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "agent"))

from obsagent import radio_commands as rc  # noqa: E402
from test_scheduler import env, fix_dirs, planner, rtl_job, submit, window  # noqa: E402,F401


def test_scheduled_reply_has_every_required_field(env):
    planner(env, [window(60, 75)]).plan()
    res = submit(env, rtl_job("slack-job", start=70, end=80))
    text, files = rc.format_result(res)
    for label in ("*SCHEDULED*", "Job:", "Target:", "Receiver:", "Antenna / signal path:", "Start:", "End:",
                  "Reservation:", "Center:", "Sample rate:", "Bandwidth:", "Gain: manual 33.8 dB", "Bias tee: off",
                  "Recording format: cu8", "Estimated size: 2.88 GB", "Raw output:", "SatDump: meteor_m2-x_lrpt",
                  "Decode output:", "Conflict check: CLEAR — preempted automatic", "Job ID:"):
        assert label in text, label
    assert "+400 kHz from center" in text and "freq_shift -400000 Hz" in text
    name, content = files[0]
    assert name.endswith(".json") and json.loads(content)["resolved"]["device_serial"] == "OBS-R02"


def test_rejected_reply_explains_and_suggests(env):
    submit(env, rtl_job("first"))
    res = submit(env, rtl_job("second", start=70, end=90))
    text, files = rc.format_result(res)
    assert text.startswith("*NOT SCHEDULED*") and "Reason: receiver conflict" in text
    assert "Affected job: `second`" in text and "Suggested correction: Pick a time outside" in text
    assert files == []


def test_dry_run_reply_books_nothing(env):
    text, _ = rc.format_result(submit(env, rtl_job(), dry=True))
    assert "nothing was booked" in text and "*VALID*" in text and "SCHEDULED" not in text


def test_capabilities_and_recordings_render(env):
    caps = env.s.capabilities()
    text = rc.format_capabilities(caps)
    assert "RSP1B" in text and "`240526D360`" in text and "V-DIPOLE (port 1)" in text
    assert rc.format_capabilities(caps, "obs-r02").count("*OBS-") == 1
    assert rc.format_recordings([]) == "No recordings match."


def test_parse_request_handles_slack_escaping():
    req, dry = rc.parse_request('/radio-schedule {&quot;jobs&quot;: [{&quot;job_name&quot;: &quot;a&lt;b&quot;}]}')
    assert req == {"jobs": [{"job_name": "a<b"}]} and not dry
    req, _ = rc.parse_request('{"job_name": "single"}')
    assert req == {"jobs": [{"job_name": "single"}]}
    import pytest
    with pytest.raises(ValueError, match="line 1"):
        rc.parse_request('{"jobs": [}')


# ---------------------------------------------------------------- new commands
from obsagent import commands as cmd  # noqa: E402


class FakeBroker:
    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def get(self, path, **params):
        self.calls.append(("GET", path, params))
        return self.answers.get(path, {})

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        return self.answers.get(path, {"attempt_id": "p1", "recording_id": "m1", "kind": body.get("kind"),
                                       "resolved": {"analysis": ["waterfall"], "fft_size": 4096}})

    def artifact_bytes(self, aid):
        return b"\x89PNG"


def test_parse_args_and_units():
    words, kv, obj = cmd.parse_args('latest receiver=OBS-R01 name="two words"')
    assert words == ["latest"] and kv == {"receiver": "OBS-R01", "name": "two words"} and obj is None
    assert cmd.parse_args('{"recording_id": "m1"}')[2] == {"recording_id": "m1"}
    assert cmd.hz("137.9") == 137_900_000 and cmd.hz("137.9MHz") == 137_900_000 and cmd.hz("250kHz") == 250_000
    assert cmd.hz("137900000") == 137_900_000
    with pytest.raises(ValueError, match="invalid JSON"):
        cmd.parse_args("{bad")


def test_decode_command_maps_to_validated_broker_request():
    b = FakeBroker()
    h = cmd.Handlers(b)
    h.decode("recording=m202610131400abcdef pipeline=meteor_m2-x_lrpt target=137.9 output_rate=500000 start=60 dc_block")
    _, path, body = b.calls[-1]
    assert path == "/v1/catalog/recordings/m202610131400abcdef/process" and body["kind"] == "decode"
    assert body["params"] == {"pipeline": "meteor_m2-x_lrpt", "target_frequency_hz": 137_900_000,
                              "output_sample_rate_hz": 500_000, "start_s": 60, "additional_parameters": {"dc_block": True}}
    h.decode('{"recording_id": "m202610131400abcdef", "pipeline": "noaa_apt", "parameters": {"frequency_offset_hz": 0}}')
    assert b.calls[-1][2]["params"] == {"pipeline": "noaa_apt", "frequency_offset_hz": 0}
    with pytest.raises(ValueError, match="which recording"):
        h.decode("pipeline=x")


def test_waterfall_compare_and_reprocess_use_the_same_engine():
    b = FakeBroker()
    h = cmd.Handlers(b)
    h.waterfall("recording=m202610131400abcdef fft=16384 frequency=137.9 bandwidth=250000")
    p = b.calls[-1][2]["params"]
    assert p == {"fft_size": 16384, "frequency_min_hz": 137_775_000, "frequency_max_hz": 138_025_000, "analysis": ["waterfall"]}
    h.analyze("compare=m202610131400abcdef,a202610131400abcdef")
    assert b.calls[-1][2]["kind"] == "compare" and b.calls[-1][2]["params"]["recordings"] == ["m202610131400abcdef", "a202610131400abcdef"]
    h.reprocess('{"recording_id": "m202610131400abcdef", "operation": "satdump_decode", "parameters": {"target_frequency_hz": 137900000}}')
    assert b.calls[-1][2]["params"]["target_frequency_hz"] == 137_900_000


def test_cancel_command_routes_by_kind():
    b = FakeBroker({"/v1/catalog/cancel": {"needs_confirmation": True, "confirm": "abc123", "warning": "RECORDING"}})
    h = cmd.Handlers(b)
    assert "RECORDING" in h.cancel("m202610131400abcdef")[0]
    h.cancel("processing=p20261013140000abcdef")
    assert b.calls[-1][2]["attempt_id"] == "p20261013140000abcdef"
    h.cancel("batch receiver=OBS-R02 start=2026-10-14T00:00:00+00:00")
    assert b.calls[-1][2]["batch"]["receiver"] == "OBS-R02"
    h.cancel("m202610131400abcdef confirm=abc123")
    assert b.calls[-1][2] == {"confirm": "abc123", "reason": "cancelled from Slack", "job_id": "m202610131400abcdef"}


def test_recordings_command_keywords():
    b = FakeBroker({"/v1/catalog/recordings": {"recordings": [], "total": 0, "page": 1, "pages": 1}})
    cmd.Handlers(b).recordings("latest satellite=METEOR-M2-4 decoded=false frequency=137.9")
    q = b.calls[-1][2]
    assert q["per_page"] == 1 and q["decoded"] is False and q["frequency_hz"] == 137_900_000 and q["satellite"] == "METEOR-M2-4"


def test_authorization_tiers():
    from obsagent.radio_commands import RadioCommands
    rc = RadioCommands.__new__(RadioCommands)
    rc.owner, rc.operators, rc.viewers = "U0", {"U0", "U1"}, {"U0", "U1", "U2"}
    assert rc.allowed("U2", "/radio-status") and not rc.allowed("U2", "/radio-decode")
    assert rc.allowed("U1", "/radio-cancel") and not rc.allowed("U9", "/radio-help")


def test_help_covers_every_command():
    for name in cmd.TIER:
        topic = name.replace("/radio-", "")
        text = cmd.help_text(topic)
        assert text and (topic == "help" or f"/radio-{topic}" in text or topic in text)
    assert "Typical flow" in cmd.help_text("")


def test_status_formatting_from_live_service(env, monkeypatch):
    from aobs.scheduling import status as status_mod
    from aobs.scheduling.status import StatusService
    monkeypatch.setattr(status_mod, "holders", lambda rx: ([], 0))          # no real USB/process state in tests
    monkeypatch.setattr(status_mod, "_sdrplay_writing", lambda rx: None)
    planner(env, [window(60, 75)]).plan()
    st = StatusService(env.s, broker_state=lambda: {"leases": {}}).status()
    text = cmd.format_status(st)
    for rx in ("OBS-R01", "OBS-R02", "OBS-R05", "RSP1B"):
        assert f"*{rx}*" in text
    assert "NEXT SCHEDULED" in text and "STORAGE" in text
    r01 = next(r for r in st["receivers"] if r["receiver_id"] == "OBS-R01")
    assert r01["state"].startswith("IDLE (legacy") and r01["next_job"]["source"] == "legacy"
    assert "OBS-R01" in cmd.format_receiver_detail(r01)


def test_tool_definitions_fit_api_limits():
    from obsagent import tools
    names = [t["name"] for t in tools.TOOLS]
    assert len(names) == len(set(names))
    strict = [t for t in tools.TOOLS if t["strict"]]
    assert len(strict) <= tools.MAX_STRICT_TOOLS
    assert sum(tools.union_params(t) for t in strict) <= tools.MAX_STRICT_UNION_PARAMS
    assert set(names) == set(tools.HANDLERS)
