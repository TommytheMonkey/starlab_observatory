"""Readiness from hardware evidence: called by the runner when a Test Lab capture finishes.

verified - the capture process recorded and the file holds real IQ samples (iqcheck)
failed   - the device could not be opened or configured, the stream stopped, or the samples are not
           real receiver output; the reasons are kept for the UI
(no change) - the test was cancelled or interrupted before it proved anything either way

Stepped captures (wideband scans, gain sweeps) are not used: their files are many short tunings.
"""

from __future__ import annotations

import logging
from typing import Any

from .iqcheck import verify_iq

LOG = logging.getLogger("aobs.inventory.readiness")


def on_test_capture(s: Any, job: dict[str, Any], capture: dict[str, Any]) -> dict[str, Any] | None:
    meta = (job.get("spec") or {}).get("metadata") or job.get("metadata") or {}
    test_id = meta.get("rf_test")
    inv = getattr(s, "inventory", None)
    if not test_id or inv is None or (job.get("spec") or {}).get("steps"):
        return None
    status = capture.get("status")
    if status in ("interrupted", "cancelled"):
        return None
    spec = job["spec"]
    radio, fmt = spec.get("radio") or {}, (spec.get("recording") or {}).get("format")
    base = {"test_id": test_id, "job_id": job["job_id"], "driver": (spec.get("receiver") or {}).get("driver"),
            "serial": job.get("device_serial"), "settings": {k: radio.get(k) for k in (
                "center_frequency_hz", "sample_rate_hz", "gain_mode", "gain_db", "gain_elements", "bandwidth_hz")},
            "format": fmt, "capture_status": status, "bytes": capture.get("bytes"),
            "expected_bytes": capture.get("expected_bytes")}
    path = capture.get("file") or (job.get("paths") or {}).get("raw")
    if status != "recorded" or not path:
        detail = {**base, "problems": [capture.get("error") or f"capture {status or 'failed'}"]}
        inv.record_readiness(job["receiver_id"], False, detail, f"test {test_id}")
        s.reload_receivers()
        return detail
    try:
        iq = verify_iq(path, fmt, int(radio["sample_rate_hz"]), capture.get("expected_bytes"))
    except Exception as exc:  # noqa: BLE001 - an unreadable file is a failed test, not a crash
        LOG.exception("IQ verification of %s failed", job["job_id"])
        iq = {"ok": False, "problems": [f"could not read the samples: {exc}"], "checks": [], "warnings": []}
    detail = {**base, "iq": {k: iq.get(k) for k in ("checks", "warnings", "samples_checked", "rms_dbfs")},
              "problems": iq.get("problems", [])}
    inv.record_readiness(job["receiver_id"], bool(iq.get("ok")), detail, f"test {test_id}")
    s.reload_receivers()
    LOG.info("receiver %s readiness %s by test %s", job["receiver_id"], "verified" if iq.get("ok") else "failed", test_id)
    return detail
