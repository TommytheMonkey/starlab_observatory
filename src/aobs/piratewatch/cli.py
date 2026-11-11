"""python -m aobs.piratewatch {run,status,release,resume,hits,heatmap,baseline,relearn,transcribe}"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import re
import sys
import time

from . import config, heatmap
from .daemon import main as run_daemon, read_control, released, write_control
from .store import Store, iso

UTC = dt.timezone.utc


def _duration(text: str) -> dt.timedelta:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", text.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError("use e.g. 90m, 2h, 1d")
    n, unit = float(m.group(1)), m.group(2) or "m"
    return dt.timedelta(seconds=n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


def _status(s: config.Settings) -> dict:
    try:
        return json.loads(s.path("status.json").read_text())
    except (OSError, ValueError):
        return {}


def cmd_status(s: config.Settings, a: argparse.Namespace) -> int:
    st = _status(s)
    ctl = read_control(s)
    if a.json:
        print(json.dumps({"status": st, "control": ctl}, indent=2))
        return 0
    if not st:
        print("The watch has not run yet (no status.json).")
        return 0
    age = (dt.datetime.now(UTC) - dt.datetime.fromisoformat(st["at"].replace("Z", "+00:00"))).total_seconds()
    stale = " (stale: is the watch running?)" if age > 30 and st["state"] not in ("stopped", "released") else ""
    print(f"{st['state']}{stale}  {st.get('detail', '')}")
    b = st.get("baseline", {})
    print(f"receiver {st['receiver']}  lease {'held' if st.get('lease_held') else 'not held'}"
          f"  baseline {b.get('learned')}/{b.get('of')} sweeps, {b.get('excluded_ranges')} excluded ranges")
    if st.get("last_sweep"):
        ls = st["last_sweep"]
        print(f"last sweep {ls['at']}: {ls['bins']} bins of {ls['bin_hz']} Hz, median {ls['median_db']} dB")
    print("hits: " + ", ".join(f"{k} {v}" for k, v in sorted(st.get("hits", {}).items())) or "none")
    if released(ctl, dt.datetime.now(UTC)):
        print(f"released for live use{' until ' + ctl['until'] if ctl.get('until') else ''}")
    return 0


def cmd_release(s: config.Settings, a: argparse.Namespace) -> int:
    until = iso(dt.datetime.now(UTC) + a.duration) if a.duration else None
    write_control(s, released=True, until=until, at=iso(dt.datetime.now(UTC)))
    print(f"Release requested{' until ' + until if until else ''}; waiting for the watch to stop the radio...")
    deadline = time.monotonic() + a.wait
    while time.monotonic() < deadline:
        st = _status(s)
        if st.get("state") in ("released", "stopped") or not st:
            print(f"{s.receiver} is free. Start SDR++ server now (sdrpp --server), and run `resume` when done.")
            return 0
        time.sleep(1)
    print("The watch has not confirmed yet (is it running?). `status` shows its state.")
    return 1


def cmd_resume(s: config.Settings, a: argparse.Namespace) -> int:
    write_control(s, released=False, until=None)
    print("Resume requested. The watch restarts once nothing else holds the receiver "
          "(quit SDR++ server or disconnect its client first).")
    return 0


def cmd_relearn(s: config.Settings, a: argparse.Namespace) -> int:
    write_control(s, relearn=True)
    print(f"The baseline will be relearned from the next {s.learn_sweeps} sweeps.")
    return 0


def cmd_hits(s: config.Settings, a: argparse.Namespace) -> int:
    store = Store(s.path("piratewatch.sqlite3"))
    rows = store.hits(None if a.all else "speech", a.limit)
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    for h in reversed(rows):
        print(f"#{h['id']} {h['start_utc'] or h['detected_at']}  {h['freq_hz'] / 1e6:.4f} MHz  "
              f"{(h['duration_s'] or 0):5.1f} s  peak {h['peak_db']} dB (+{h['snr_db']})  [{h['status']}]"
              f"{'  ' + h['language'] if h['language'] else ''}")
        if h["transcript"]:
            print(f"    {h['transcript']}")
        if h["translation"] and h["translation"] != h["transcript"]:
            print(f"    EN: {h['translation']}")
        if h["audio_path"]:
            print(f"    {h['audio_path']}")
        if h["note"]:
            print(f"    note: {h['note']}")
    if not rows:
        print("No hits yet." if a.all else "No speech hits yet (--all shows every detection).")
    return 0


def cmd_heatmap(s: config.Settings, a: argparse.Namespace) -> int:
    store = Store(s.path("piratewatch.sqlite3"))
    print(heatmap.ascii(store, a.days))
    path = heatmap.render_html(store, pathlib.Path(a.html) if a.html else s.path("heatmap.html"), max(a.days, 14))
    print(f"\nHTML: {path}")
    return 0


def cmd_baseline(s: config.Settings, a: argparse.Namespace) -> int:
    from .spectrum import Baseline
    b = Baseline(s.learn_sweeps)
    if not b.load(s.path("baseline.npz")):
        print("No baseline yet.")
        return 0
    print(f"learned {b.learned}/{s.learn_sweeps} sweeps")
    for lo, hi in b.excluded_ranges():
        print(f"  excluded {lo / 1e6:.4f}-{hi / 1e6:.4f} MHz")
    return 0


def cmd_transcribe(s: config.Settings, a: argparse.Namespace) -> int:
    from .transcribe import WhisperProcess
    w = WhisperProcess(s)
    try:
        print(json.dumps(w.transcribe(0, str(pathlib.Path(a.path).resolve())), indent=2))
    finally:
        w.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aobs.piratewatch", description="Satcom pirate watch on OBS-R05")
    ap.add_argument("--config", type=pathlib.Path, help=f"settings file (default {config.DEFAULT_CONFIG})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the watch (foreground; the systemd unit runs this)")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("release", help="stop the watch's radio and free the receiver for SDR++")
    p.add_argument("duration", nargs="?", type=_duration, help="auto-resume after this long (e.g. 2h); default: until resume")
    p.add_argument("--wait", type=float, default=20)
    sub.add_parser("resume", help="take the receiver back after a release")
    sub.add_parser("relearn", help="relearn the steady-carrier baseline")
    p = sub.add_parser("hits")
    p.add_argument("--all", action="store_true", help="include missed, non-speech and pending detections")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("heatmap")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--html")
    sub.add_parser("baseline", help="show the learned steady carriers")
    p = sub.add_parser("transcribe", help="run Whisper on one audio file")
    p.add_argument("path")
    a = ap.parse_args(argv)
    s = config.load(a.config)
    if a.cmd == "run":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        return run_daemon(s)
    return {"status": cmd_status, "release": cmd_release, "resume": cmd_resume, "relearn": cmd_relearn,
            "hits": cmd_hits, "heatmap": cmd_heatmap, "baseline": cmd_baseline,
            "transcribe": cmd_transcribe}[a.cmd](s, a)


if __name__ == "__main__":
    sys.exit(main())
