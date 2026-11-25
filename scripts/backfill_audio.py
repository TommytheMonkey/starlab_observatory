#!/usr/bin/env python3
"""Listenable FM audio for recordings made before audio extraction existed.

Queue mode (default) uses the broker's own processing: it submits an ordinary kind=audio attempt for every recording
that has raw IQ on disk, an FM downlink (Processor.wants_audio; --all for every recording) and no audio yet. The
running broker then runs them one at a time below recording priority, pauses them while a receiver records, and
indexes the files, exactly as after a new recording. It needs the broker's database, so run it as starlab:

  sudo -u starlab /opt/starlab/venv/bin/python scripts/backfill_audio.py                      # what it would queue
  sudo -u starlab /opt/starlab/venv/bin/python scripts/backfill_audio.py --apply
  sudo -u starlab /opt/starlab/venv/bin/python scripts/backfill_audio.py --recording m2026112118302d55eb --apply

Offline mode runs the same planning (fmaudio.plan) and DSP (fmaudio.run) on one recording and its JSON sidecar
without the database, into --out. The station location comes from --lat/--lon (the database holds it otherwise);
without them there is no Doppler tracking:

  python scripts/backfill_audio.py --offline RAW.cu8 --out ~/.cache/audio --lat 35.48 --lon -80.83
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from aobs.scheduling import fmaudio  # noqa: E402

DEFAULT_CONFIG = "/etc/starlab/starlab.toml"
DATA_ROOT = pathlib.Path("/data/starlab")


def queue(a: argparse.Namespace) -> int:
    from sqlalchemy import select

    from aobs.cli import _engine
    from aobs.config import load_config
    from aobs.db.schema import sched_jobs
    from aobs.scheduling.catalog import RECORDED
    from aobs.scheduling.processing import Processor
    from aobs.scheduling.store import Scheduler, SchedulerError, row_dict

    config = load_config(a.config)
    s = Scheduler(config, _engine(config))
    p = Processor(s)
    q = select(sched_jobs).where(sched_jobs.c.status.in_(RECORDED))
    if a.recording:
        q = select(sched_jobs).where(sched_jobs.c.job_id.in_(a.recording))
    with s.engine.connect() as conn:
        jobs = [row_dict(r) for r in conn.execute(q.order_by(sched_jobs.c.record_start.desc()))]
    queued = skipped = 0
    for j in jobs:
        rid = j["job_id"]
        if a.since and str(j["record_start"])[:10] < a.since:
            continue
        why = None
        if not p.inputs(j):
            why = "no raw IQ on disk"
        elif not (a.all or a.recording or p.wants_audio(j)):
            why = "no FM downlink (use --all)"
        elif not a.force and any(x["kind"] == "audio" and x["status"] != "failed" for x in p.attempts(rid)):
            why = "has audio already (use --force)"
        if why:
            skipped += 1
            if a.verbose or a.recording:
                print(f"skip  {rid} {j.get('target') or ''}: {why}")
            continue
        if not a.apply:
            print(f"would queue  {rid} {j.get('target') or ''} {j['record_start']}")
            queued += 1
            continue
        try:
            att = p.submit(rid, "audio", {}, "backfill", "backfill_audio.py")
            print(f"queued {att['attempt_id']}  {rid} {j.get('target') or ''}")
            queued += 1
        except SchedulerError as exc:
            print(f"FAILED {rid}: {exc}", file=sys.stderr)
    print(f"{queued} {'queued' if a.apply else 'to queue (dry run; --apply to queue)'}, {skipped} skipped")
    return 0


def offline(a: argparse.Namespace) -> int:
    from aobs.satellites import predict
    from aobs.scheduling.decoders import recording_identity

    raw = a.offline.expanduser().resolve()
    side = json.loads((a.sidecar or raw.with_name(raw.name + ".json")).read_text())
    spec = side["request"]
    radio = spec["radio"]
    first = {"path": str(raw), "format": spec["recording"]["format"], "sample_rate_hz": int(radio["sample_rate_hz"]),
             "center_frequency_hz": int(radio["center_frequency_hz"]), "target_frequency_hz": radio.get("target_frequency_hz")}
    rec = {"job_id": side["job_id"], "spec": spec, "record_start": side.get("start_time"), "target": side.get("satellite")}
    norad, _ = recording_identity(rec)
    elements = predict.load_elements(a.data_root).get(int(norad)) if norad else None
    loc = (a.lat, a.lon, a.elevation) if a.lat is not None and a.lon is not None else None
    params: dict[str, Any] = {} if a.doppler is None else {"doppler": a.doppler}
    resolved = fmaudio.plan(rec, first, params, norad, elements, loc, [])
    out = a.out.expanduser() / side["job_id"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "attempt.json").write_text(json.dumps({"kind": "audio", "inputs": [first], "params": resolved}, indent=1,
                                                 default=str))
    t0 = time.time()
    res, arts = fmaudio.run(first, resolved, out, progress=lambda st, i, n: print(f"[{time.time() - t0:6.1f} s] {st}",
                                                                                  flush=True))
    (out / "result.json").write_text(json.dumps({"inputs": [res], "artifacts": arts}, indent=1))
    for t in res["tracks"]:
        print(f"{t['frequency_hz'] / 1e6:.4f} MHz  {t['label'] or t['source']:<45} {t['active_s']:7.1f} s active  "
              f"{len(t['segments'])} segment(s)  Doppler {'tracked' if t['doppler_tracked'] else 'not tracked'}")
    for e in res["errors"]:
        print(f"warning: {e}", file=sys.stderr)
    print(f"{len(arts)} file(s) in {out} ({time.time() - t0:.0f} s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--recording", action="append", help="only this recording id (repeatable)")
    ap.add_argument("--since", help="only recordings from this date (YYYY-MM-DD)")
    ap.add_argument("--all", action="store_true", help="also recordings without an FM downlink")
    ap.add_argument("--force", action="store_true", help="queue again even when audio exists")
    ap.add_argument("--apply", action="store_true", help="queue the attempts (default: list them)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--offline", type=pathlib.Path, metavar="RAW", help="run one recording here, without the database")
    ap.add_argument("--sidecar", type=pathlib.Path, help="its JSON sidecar (default: RAW.json)")
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("~/.cache/starlab-audio"))
    ap.add_argument("--data-root", type=pathlib.Path, default=DATA_ROOT, help="where the orbital elements are (tle/)")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--elevation", type=float, default=0.0, help="station elevation, m")
    dg = ap.add_mutually_exclusive_group()
    dg.add_argument("--doppler", dest="doppler", action="store_true", default=None, help="require Doppler tracking")
    dg.add_argument("--no-doppler", dest="doppler", action="store_false", help="no Doppler tracking")
    a = ap.parse_args(argv)
    return offline(a) if a.offline else queue(a)


if __name__ == "__main__":
    sys.exit(main())
