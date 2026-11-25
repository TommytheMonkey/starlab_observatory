#!/usr/bin/env python3
"""Decode one known-good recording through both the automatic (rule) and the manual (booked) decode path and check
that they agree and actually decode.

Both kinds of job reach SatDump the same way: processing.decode_requests(spec) -> Processor._with_profile ->
Processor._validate -> procworker.decode. This script runs exactly that code, once with the job spec of an automatic
recording and once with the job spec of a manual booking, against the same raw IQ, and fails unless

  * the resolved SatDump parameters are identical (and match what the broker itself resolved, when given), and
  * both decodes produce more than 0 CADU frames, and the same number.

Nothing in the archive is written: outputs go to --out (default: a temporary directory).

  /opt/starlab/venv/bin/python scripts/check_decode_paths.py                 # the default known-good recording
  /opt/starlab/venv/bin/python scripts/check_decode_paths.py --resolve-only  # parameters only (seconds)
  /opt/starlab/venv/bin/python scripts/check_decode_paths.py --raw X.cs16 --sidecar X.cs16.json --manual-sidecar Y.json

Exit status 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
import time
import types
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from aobs.scheduling import procworker  # noqa: E402
from aobs.scheduling.processing import Processor, decode_requests  # noqa: E402

ARCHIVE = pathlib.Path("/data/archive/starlab/recordings")
# 2026-11-23 9:46 AM EST, METEOR-M2 3 on RX-01, automatic: 161 frames, 11 images in production
GOOD_RAW = ARCHIVE / "auto/RX-01/meteor-m2-3-20261123-1447/METEOR-M2-3_RX-01_20261123T144635Z_137650000_2000000.cs16"
GOOD_ATTEMPT = ARCHIVE / "auto/RX-01/meteor-m2-3-20261123-1447/processing/a202611231447654e9c/p2026112315025470856c"
# a manual booking from the 2026-11-21 18:3x bulk booking, same receiver and downlink
MANUAL_SIDECAR = ARCHIVE / "jobs/meteor-m2-3-20261122-1510/METEOR-M2-3_RX-01_20261122T151023Z_137650000_2000000.cs16.json"


def recording(raw: pathlib.Path, sidecar: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The recording as the processor sees it (rec) and its first input (what Processor.inputs would give)."""
    spec = sidecar["request"]
    radio = spec["radio"]
    inp = {"path": str(raw), "format": spec["recording"]["format"], "sample_rate_hz": int(radio["sample_rate_hz"]),
           "center_frequency_hz": int(radio["center_frequency_hz"]),
           "target_frequency_hz": radio.get("target_frequency_hz"), "recording_id": sidecar["job_id"]}
    return {"job_id": sidecar["job_id"], "spec": spec, "source": sidecar.get("source")}, inp


def resolve(rec: dict[str, Any], inp: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Run `spec`'s post-recording decode request through the processor's own resolution, for recording `inp`."""
    wanted = decode_requests(spec)
    if not wanted:
        raise SystemExit(f"{rec['job_id']}: the job spec asks for no decode after recording")
    s = types.SimpleNamespace(settings={}, config=types.SimpleNamespace(data_root=pathlib.Path(tempfile.gettempdir())),
                              pipelines={wanted[0]["pipeline"]}, profiles=None)
    proc = Processor(s)            # type: ignore[arg-type]  - only the parameter logic is used
    params, _ = proc._with_profile({**rec, "spec": spec}, dict(wanted[0]))
    return proc._validate("decode", params, inp)


def run(resolved: dict[str, Any], inp: dict[str, Any], out: pathlib.Path) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    r, _ = procworker.decode(inp, resolved, out, "", {"satdump_timeout_seconds": 1800})
    r["wall_s"] = round(time.monotonic() - t0, 1)
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--raw", type=pathlib.Path, default=GOOD_RAW)
    ap.add_argument("--sidecar", type=pathlib.Path, help="the raw file's own sidecar (default: <raw>.json)")
    ap.add_argument("--manual-sidecar", type=pathlib.Path, default=MANUAL_SIDECAR,
                    help="sidecar of a manual booking whose decode request is used for the manual path")
    ap.add_argument("--reference-attempt", type=pathlib.Path, default=GOOD_ATTEMPT,
                    help="a broker attempt dir for --raw; its resolved parameters must match ('' to skip)")
    ap.add_argument("--out", type=pathlib.Path, help="where the decodes go (default: a temporary directory)")
    ap.add_argument("--resolve-only", action="store_true", help="compare the parameters, do not run SatDump")
    a = ap.parse_args(argv)
    sidecar = json.loads((a.sidecar or a.raw.with_name(a.raw.name + ".json")).read_text())
    manual = json.loads(a.manual_sidecar.read_text())
    rec, inp = recording(a.raw, sidecar)
    paths = {"auto" if sidecar.get("source") != "manual" else "own": sidecar["request"], "manual": manual["request"]}
    if "own" in paths:
        print(f"note: {rec['job_id']} is itself a manual job; comparing its spec with {manual['job_id']}'s")
    resolved = {name: resolve(rec, inp, spec) for name, spec in paths.items()}
    ok = True
    (n1, r1), (n2, r2) = resolved.items()
    print(json.dumps({"input": inp, **resolved}, indent=1, default=str))
    if r1 != r2:
        ok = False
        diff = {k: (r1.get(k), r2.get(k)) for k in sorted(set(r1) | set(r2)) if r1.get(k) != r2.get(k)}
        print(f"FAIL: {n1} and {n2} resolve to different parameters: {diff}")
    else:
        print(f"ok: {n1} and {n2} resolve to identical SatDump parameters")
    if a.reference_attempt and str(a.reference_attempt) and (a.reference_attempt / "attempt.json").exists():
        ref = json.loads((a.reference_attempt / "attempt.json").read_text())["params"]
        ref = {k: v for k, v in ref.items() if k != "profile"}
        if ref != r1:
            ok = False
            print(f"FAIL: differs from the broker's own resolution in {a.reference_attempt}: "
                  f"{ {k: (ref.get(k), r1.get(k)) for k in set(ref) | set(r1) if ref.get(k) != r1.get(k)} }")
        else:
            print(f"ok: identical to the broker's resolution in {a.reference_attempt.name}")
    if a.resolve_only:
        return 0 if ok else 1
    base = a.out or pathlib.Path(tempfile.mkdtemp(prefix="check-decode-"))
    frames = {}
    for name, res in resolved.items():
        r = run(res, inp, base / name)
        frames[name] = r["cadu_frames"]
        print(f"{name}: {r['cadu_frames']} frames, {r['images']} image(s), best offset {r['best_residual_offset_hz']:+d} Hz, "
              f"{r['wall_s']} s; command: {' '.join(r['command'])}")
        if not r["cadu_frames"]:
            ok = False
            print(f"FAIL: {name} decoded 0 frames ({r.get('error')})")
    if len(set(frames.values())) != 1:
        ok = False
        print(f"FAIL: frame counts differ: {frames}")
    print("PASS" if ok else "FAILED", f"(outputs in {base})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
