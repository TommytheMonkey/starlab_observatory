"""The ground-station firmware archive: <firmware_dir>/<release>/ with images/, patches/, config/, buildinfo/, source/,
RELEASE.json (what was built, from which commits, and the verified checksums). Downloads are served only for files
listed in RELEASE.json whose SHA-256 still matches, so a damaged or swapped image is never offered."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Any

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def releases(root: pathlib.Path, verify: bool = False) -> list[dict[str, Any]]:
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        meta = d / "RELEASE.json"
        if not meta.is_file():
            continue
        try:
            r = json.loads(meta.read_text())
        except ValueError:
            continue
        r["id"] = d.name
        for f in r.get("files", []):
            p = d / f["path"]
            f["present"] = p.is_file()
            f["size"] = p.stat().st_size if f["present"] else None
            if verify and f["present"]:
                f["verified"] = sha256(p) == f["sha256"]
        out.append(r)
    return out


def file_for(root: pathlib.Path, release: str, rel: str) -> pathlib.Path | None:
    """The archived file `rel` of `release`, only if RELEASE.json lists it and its checksum matches."""
    if not NAME.fullmatch(release) or ".." in rel or rel.startswith("/"):
        return None
    d = (root / release).resolve()
    if not d.is_relative_to(root.resolve()):
        return None
    try:
        r = json.loads((d / "RELEASE.json").read_text())
    except (OSError, ValueError):
        return None
    entry = next((f for f in r.get("files", []) if f["path"] == rel), None)
    if entry is None:
        return None
    p = (d / rel).resolve()
    if not p.is_relative_to(d) or not p.is_file() or sha256(p) != entry["sha256"]:
        return None
    return p
