#!/usr/bin/env python3
"""Add the parsed APRS fields to aprs.json files written before aprsdec parsed packets itself.

aprsdec now stores every packet's aprslib fields ("aprs") and UTC time ("time_utc") in aprs.json when a decode is
written (aprsparse.enrich). This does the same for older decodes, in place: the packets, their order, raw TNC2 lines
and every other key stay as they are; a packet aprslib cannot parse gets parsed=false and is kept. Files that already
have the fields are left alone unless --force. The archive page parses old files on reading until this has run.

  sudo -u starlab /opt/starlab/venv/bin/python scripts/backfill_aprs_packets.py            # every job folder
  sudo -u starlab /opt/starlab/venv/bin/python scripts/backfill_aprs_packets.py --dry-run
  python scripts/backfill_aprs_packets.py --out ~/.cache/aprs-backfill PATH/aprs.json        # copies elsewhere

The job folders belong to the starlab user, so writing in place needs `sudo -u starlab`; --out writes copies
(under OUT, at the file's absolute path) and needs only read access.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from aobs.scheduling import aprsparse  # noqa: E402

ROOTS = [pathlib.Path("/data/starlab/recordings"), pathlib.Path("/data/archive/starlab/recordings"),
         pathlib.Path("/data/starlab/processing")]
NAME = re.compile(r"aprs(-\d\d)?\.json")


def find(roots: list[pathlib.Path]) -> list[pathlib.Path]:
    seen: dict[pathlib.Path, pathlib.Path] = {}           # /data/starlab/recordings is a link into the archive
    for r in roots:
        if r.is_dir():
            for p in r.rglob("aprs*.json"):
                if NAME.fullmatch(p.name) and "processing" in p.parts:
                    seen.setdefault(p.resolve(), p)
    return sorted(seen.values())


def write_atomic(path: pathlib.Path, text: str, like: pathlib.Path | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        if like is not None and like.exists():
            os.chmod(tmp, like.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def backfill(path: pathlib.Path, force: bool, dry_run: bool, out: pathlib.Path | None) -> str:
    doc = json.loads(path.read_text())
    packets = doc.get("packets") or []
    if packets and all("aprs" in p for p in packets) and not force:
        return "already parsed"
    doc["packets"] = aprsparse.enrich(packets, doc.get("recording"))
    ok = sum(1 for p in doc["packets"] if p["aprs"].get("parsed"))
    what = f"{len(packets)} packet(s), {ok} parsed, {len(packets) - ok} unparsed"
    if dry_run:
        return what + " (dry run, not written)"
    dest = out / path.resolve().relative_to("/") if out else path
    write_atomic(dest, json.dumps(doc, indent=1), path if dest == path else None)
    return what + ("" if dest == path else f" -> {dest}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", type=pathlib.Path, help="aprs.json files (default: search --root)")
    ap.add_argument("--root", action="append", type=pathlib.Path, help=f"where to search (default: {', '.join(map(str, ROOTS))})")
    ap.add_argument("--force", action="store_true", help="parse again even when the fields are there")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", type=pathlib.Path, help="write copies under this folder instead of in place")
    a = ap.parse_args(argv)
    files = a.paths or find(a.root or ROOTS)
    bad = 0
    for f in files:
        try:
            print(f"{f}: {backfill(f, a.force, a.dry_run, a.out.expanduser() if a.out else None)}")
        except (OSError, ValueError) as exc:
            bad += 1
            print(f"{f}: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"{len(files)} file(s), {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
