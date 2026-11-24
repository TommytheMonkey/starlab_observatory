"""A recording must hold the signal it is booked for. OBS-R10 (an Airspy HF+) recorded METEOR at 192 kS/s from
the 2026-11-21 bulk booking: the 150 kHz LRPT signal did not fit, and the decode was refused after the fact."""

from __future__ import annotations

import pytest

from aobs.scheduling import dsp
from aobs.scheduling.hardware import Receiver, default_radio
from aobs.scheduling.processing import decode_requests
from aobs.scheduling.spec import signal_span_problems

HFPLUS_RATES = [192_000, 228_000, 384_000, 456_000, 650_000, 768_000, 912_000]   # SoapySDRUtil --probe, 2026-11-24
METEOR = {"type": "satellite", "name": "METEOR-M2 4", "norad_id": 59051}


def hfplus() -> Receiver:
    return Receiver(id="OBS-R10", driver="soapy", serial="3B528080C04446C7", hardware="Airspy HF+", antenna="",
                    signal_path="", mode="broker", soapy_driver="airspyhf", caps={"sample_rates_hz": HFPLUS_RATES})


@pytest.mark.parametrize("hint", [None, 150_000])
def test_a_booking_without_a_rate_on_the_hf_plus_records_at_its_native_768k_not_192k(hint):
    r = default_radio(hfplus(), 137_900_000, None, bandwidth_hint_hz=hint)
    assert r["sample_rate_hz"] == 768_000
    assert r["center_frequency_hz"] == 137_900_000 - 768_000 // 8       # 137.804 MHz: off the DC spike


def test_an_explicit_rate_is_kept():
    assert default_radio(hfplus(), 137_900_000, 384_000)["sample_rate_hz"] == 384_000


def test_the_channel_planner_decimates_an_offset_recording_below_1_msps():
    plan = dsp.plan_channel(768_000, 137_804_000, 137_900_000)
    assert (plan.decimation, plan.output_sample_rate_hz) == (2, 384_000)
    assert dsp.plan_channel(2_000_000, 137_650_000, 137_900_000).decimation == 4        # unchanged
    assert dsp.plan_channel(1_024_000, 137_772_000, 137_900_000).decimation == 2        # unchanged
    with pytest.raises(ValueError, match="not fully inside the recording"):
        dsp.plan_channel(192_000, 137_876_000, 137_900_000)


def test_booking_refuses_a_span_that_cannot_hold_the_signal_and_its_doppler():
    job = {"target": METEOR}
    bad = signal_span_problems(job, {"sample_rate_hz": 192_000, "center_frequency_hz": 137_876_000,
                                     "target_frequency_hz": 137_900_000})
    assert len(bad) == 1 and "lrpt at 137.9000 MHz does not fit" in bad[0] and "192 kS/s gives 77 kHz" in bad[0]
    for rate, center in ((768_000, 137_804_000), (2_000_000, 137_650_000), (1_024_000, 137_772_000)):
        assert signal_span_problems(job, {"sample_rate_hz": rate, "center_frequency_hz": center,
                                          "target_frequency_hz": 137_900_000}) == []


def test_auto_and_manual_job_specs_ask_for_the_same_decode():
    """Rules and bookings write the same satdump block; decode_requests is the one place it becomes a request."""
    sd = {"enabled": True, "operation": "decode_after_recording", "pipeline": "meteor_m2-x_lrpt", "frequency_hz": 137_900_000}
    auto = {"satdump": {**sd, "output_directory": "/data/starlab/recordings/auto/decoded/{job_name}"}}
    manual = {"satdump": {**sd, "output_directory": "/data/starlab/recordings/jobs/x/decoded"}}
    assert decode_requests(auto) == decode_requests(manual) == [{"pipeline": "meteor_m2-x_lrpt",
                                                                 "target_frequency_hz": 137_900_000}]
    assert decode_requests({"satdump": {**sd, "enabled": False}}) == []
