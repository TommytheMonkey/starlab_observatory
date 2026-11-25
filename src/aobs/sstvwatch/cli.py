"""python -m aobs.sstvwatch {run,status,release,resume,images,plan,decode}"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys
import tempfile
from zoneinfo import ZoneInfo

from ..piratewatch.cli import _duration
from . import config
from .plan import Planner
from .store import Store, iso
from .watch import main as run_daemon, read_control, released, write_control

UTC = dt.timezone.utc


def _status(s: config.Settings) -> dict:
    try:
        return json.loads(s.path("status.json").read_text())
    except (OSError, ValueError):
        return {}


def cmd_status(s: config.Settings, a: argparse.Namespace) -> int:
    st, ctl = _status(s), read_control(s)
    if a.json:
        print(json.dumps({"status": st, "control": ctl}, indent=2))
        return 0
    if not st:
        print("The watch has not run yet (no status.json).")
        return 0
    age = (dt.datetime.now(UTC) - dt.datetime.fromisoformat(st["at"].replace("Z", "+00:00"))).total_seconds()
    print(f"{st['state']}{' (stale: is the watch running?)' if age > 30 else ''}  "
          f"decoded this run {st.get('decoded_this_run', 0)}, decode queue {st.get('decode_queue', 0)}, "
          f"held for a Slack batch {st.get('pending_batch', 0)}")
    for w in st.get("watchers", []):
        on = f"{w['channel']} {w['freq_hz'] / 1e6:.3f} MHz {str(w['sideband']).upper()}" if w.get("channel") else "-"
        print(f"  {w['receiver']}: {w['state']} {w.get('detail', '')}  on {on}  lease "
              f"{'held' if w.get('lease_held') else 'not held'}")
    print("archive: " + (", ".join(f"{k} {v}" for k, v in sorted((st.get("counts") or {}).items())) or "empty"))
    if released(ctl, dt.datetime.now(UTC)):
        print(f"released{' until ' + ctl['until'] if ctl.get('until') else ''}")
    return 0


def cmd_release(s: config.Settings, a: argparse.Namespace) -> int:
    until = iso(dt.datetime.now(UTC) + a.duration) if a.duration else None
    write_control(s, released=True, until=until, at=iso(dt.datetime.now(UTC)))
    print(f"released{' until ' + until if until else '; run `resume` to restart'}")
    return 0


def cmd_resume(s: config.Settings, a: argparse.Namespace) -> int:
    write_control(s, released=False, until=None)
    print("resume requested")
    return 0


def cmd_images(s: config.Settings, a: argparse.Namespace) -> int:
    rows = Store(s.path("sstvwatch.sqlite3")).images(status=None if a.all else "decoded", band=a.band, mode=a.mode,
                                                     limit=a.limit)
    if a.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    for r in rows:
        print(f"{r['id']:>6} {r['start_utc']} {r['freq_hz'] / 1e6:8.3f} {r['band']:>5} {str(r['mode']):<12} "
              f"{r['status']:<8} q={r['quality'] if r['quality'] is not None else '-'} "
              f"snr={r['snr_db'] if r['snr_db'] is not None else '-'} {r.get('receiver') or ''} "
              f"{r.get('image_path') or r.get('error') or ''}")
    return 0


def cmd_plan(s: config.Settings, a: argparse.Namespace) -> int:
    """The dwell plan the watch would follow over the next `hours`, for the first watcher, by local time."""
    p = Planner(s)
    t = dt.datetime.now(UTC)
    end = t + dt.timedelta(hours=a.hours)
    tz = ZoneInfo(s.timezone)
    totals: dict[str, float] = {}
    while t < end:
        d = p.next(t)
        if d is None:
            t += dt.timedelta(minutes=30)
            continue
        if not a.summary:
            print(f"{t.astimezone(tz):%a %H:%M}  {d.channel.name:<5} {d.channel.hz / 1e6:7.3f} MHz "
                  f"{d.channel.mode.upper()}  {d.seconds / 60:4.1f} min  ({'day' if d.daytime else 'night'})")
        totals[d.channel.name] = totals.get(d.channel.name, 0) + d.seconds
        t += dt.timedelta(seconds=d.seconds)
    tot = sum(totals.values()) or 1
    print("share of listening time: " + ", ".join(f"{k} {v / tot:.0%}" for k, v in sorted(totals.items(),
                                                                                          key=lambda kv: -kv[1])))
    return 0


def cmd_decode(s: config.Settings, a: argparse.Namespace) -> int:
    """Run the watch's detection and decode on an audio WAV (to check a recording, or the threshold)."""
    from ..scheduling.sstvdec import _read_wav, find_vis_headers
    from .decodeworker import decode_job
    from .modes import mode_of
    x, sr = _read_wav(pathlib.Path(a.wav))
    heads = find_vis_headers(x, sr)
    print(f"{len(heads)} VIS header(s): " + ", ".join(f"{h['at_s']} s VIS {h['vis']} ({mode_of(h['vis'])[0]})"
                                                      for h in heads))
    out = pathlib.Path(a.out or tempfile.mkdtemp(prefix="sstvwatch-decode-"))
    out.mkdir(parents=True, exist_ok=True)
    res = decode_job({"wav": a.wav, "out_dir": str(out), "stem": pathlib.Path(a.wav).stem,
                      "vis_mode": mode_of(heads[0]["vis"])[0] if heads else None, "min_quality": s.min_quality})
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("status") == "decoded" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aobs.sstvwatch", description="SSTV watch (HF calling frequencies, ISS)")
    ap.add_argument("--config", type=pathlib.Path, help=f"settings file (default {config.DEFAULT_CONFIG})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the watch (foreground; the systemd unit runs this)")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("release", help="stop the radio and free the receivers")
    p.add_argument("duration", nargs="?", type=_duration, help="auto-resume after this long (e.g. 2h)")
    sub.add_parser("resume", help="take the receivers back after a release")
    p = sub.add_parser("images", help="the newest archived images")
    p.add_argument("--all", action="store_true", help="include noise and failed decodes")
    p.add_argument("--band")
    p.add_argument("--mode")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("plan", help="the dwell plan over the next hours")
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--summary", action="store_true")
    p = sub.add_parser("decode", help="detect and decode SSTV in a WAV file")
    p.add_argument("wav")
    p.add_argument("--out")
    a = ap.parse_args(argv)
    s = config.load(a.config)
    if a.cmd == "run":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        return run_daemon(s)
    return {"status": cmd_status, "release": cmd_release, "resume": cmd_resume, "images": cmd_images,
            "plan": cmd_plan, "decode": cmd_decode}[a.cmd](s, a)


if __name__ == "__main__":
    sys.exit(main())
