"""One processing attempt, run as its own low-priority process:
`nice -n 19 ionice -c 3 python -m aobs.scheduling.procworker <attempt.json>`.

kind=decode   SatDump over each input file (optionally a time slice), with the attempt's parameters.
kind=analyze  any of: checksum, waterfall, spectrum, stats (IQ diagnostics + power-vs-time plot),
              with optional zoom (center/span) and time range.

Everything goes in the attempt's own output directory and is listed in result.json, which
the broker indexes. The broker pauses this process (SIGSTOP) while a recording is running.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
from typing import Any

import numpy as np

from .postprocess import DTYPES, run_satdump, satdump_command

FULL_SCALE = {"cu8": 1.0, "cs8": 1.0, "cs16": 1.0, "cf32": None}


def _bps(fmt: str) -> int:
    dtype, comps = DTYPES[fmt]
    return np.dtype(dtype).itemsize * comps


def _write_slice(src: pathlib.Path, fmt: str, rate: int, t0: float | None, t1: float | None, dst: pathlib.Path) -> pathlib.Path:
    bps = _bps(fmt)
    total = src.stat().st_size // bps
    a = int((t0 or 0) * rate)
    b = min(total, int(t1 * rate)) if t1 else total
    if not 0 <= a < b:
        raise ValueError(f"time range {t0}-{t1} s is outside the recording ({total / rate:.1f} s)")
    with src.open("rb") as fi, dst.open("wb") as fo:
        fi.seek(a * bps)
        left = (b - a) * bps
        while left > 0:
            block = fi.read(min(left, 16 << 20))
            if not block:
                break
            fo.write(block)
            left -= len(block)
    return dst


from .analyze_run import analyze, compare  # noqa: E402  (single analysis engine)


def decode(inp: dict[str, Any], p: dict[str, Any], out: pathlib.Path, tag: str, settings: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """SatDump over one input.

    shift_method=internal: the target channel is shifted to 0 Hz and decimated once (dsp.extract). If
    search_offsets_hz is given (or defaulted by the processor), each small residual offset is removed from
    that extracted file (dsp.shift_file) and decoded separately; the run with the most CADU frames wins.
    This absorbs Doppler and receiver frequency error that SatDump's carrier loop cannot pull in.

    Success is judged from what SatDump produced (CADU frames + images with content). This SatDump build
    exits with status 1 even after a good decode, so the exit code is recorded but not trusted.
    """
    from . import dsp

    src = pathlib.Path(inp["path"])
    rate, fmt = int(inp["sample_rate_hz"]), inp["format"]
    method = p.get("shift_method", "internal")
    plan = None
    stage: dict[str, Any] = {}
    tmp: pathlib.Path | None = None
    in_file, in_fmt, in_rate, freq_shift = src, fmt, rate, None
    if method == "internal" and (p.get("frequency_offset_hz") or p.get("output_sample_rate_hz") or p.get("search_offsets_hz")):
        plan = dsp.plan_channel(rate, int(inp["center_frequency_hz"]),
                                int(inp["center_frequency_hz"]) + int(p.get("frequency_offset_hz") or 0),
                                p.get("output_sample_rate_hz"))
        tmp = out / f".channel{tag}.cf32"
        stage = dsp.extract(src, fmt, plan, tmp, p.get("start_s"), p.get("end_s"))
        in_file, in_fmt, in_rate = tmp, "cf32", plan.output_sample_rate_hz
    elif p.get("start_s") is not None or p.get("end_s") is not None:
        tmp = _write_slice(src, fmt, rate, p.get("start_s"), p.get("end_s"), out / f".slice{tag}.{fmt}")
        in_file = tmp
    if method == "satdump" and p.get("frequency_offset_hz"):
        freq_shift = -int(p["frequency_offset_hz"])           # SatDump shifts by this; see docs/COMMANDS.md
    offsets = [int(o) for o in (p.get("search_offsets_hz") or [0])] if method == "internal" else [0]
    sd = {"pipeline": p["pipeline"], "sample_rate_hz": in_rate, "baseband_format": in_fmt,
          "additional_parameters": p.get("additional_parameters") or {}}
    runs, arts = [], []
    t0 = dt.datetime.now(dt.timezone.utc)
    try:
        for fo in offsets:
            label = f"{fo:+d}Hz" if len(offsets) > 1 else ""
            dest = out / (f"satdump{tag}" + (f"_{label}" if label else ""))
            feed, shifted = in_file, None
            if fo:
                shifted = out / f".channel{tag}_{label}.cf32"
                dsp.shift_file(in_file, shifted, fo, in_rate)
                feed = shifted
            cmd = satdump_command({**settings, "satdump_nice": 0}, sd, str(feed), str(dest), freq_shift)[3:]
            try:
                r = run_satdump(settings, cmd, dest)
            finally:
                if shifted is not None:
                    shifted.unlink(missing_ok=True)
            cadu = sum(f.stat().st_size for f in dest.glob("*.cadu")) // 1024
            images = []
            for rel in r["products"]:
                f = dest / rel
                if f.suffix.lower() in (".png", ".jpg", ".jpeg"):
                    images.append({"file": f"{dest.name}/{rel}", **_image_content(f)})
            runs.append({"residual_offset_hz": fo, "target_frequency_hz": (plan.target_frequency_hz + fo) if plan else None,
                         "output_directory": str(dest), "command": cmd, "return_code": r["return_code"],
                         "cadu_frames": cadu, "images": images, "log": r["log"], **_satdump_sync(pathlib.Path(r["log"]))})
            arts.append({"kind": "log", "path": r["log"], "label": f"satdump log {label}".strip()})
            for rel in r["products"]:
                f = dest / rel
                kind = "image" if f.suffix.lower() in (".png", ".jpg", ".jpeg") else "product"
                q = next((i for i in images if i["file"] == f"{dest.name}/{rel}"), {"has_content": True})
                arts.append({"kind": kind, "path": str(f),
                             "label": f"{label + ' ' if label else ''}{rel}" + ("" if q.get("has_content", True) else " (blank)")})
    finally:
        if tmp is not None and not p.get("keep_intermediate"):
            tmp.unlink(missing_ok=True)
    if tmp is not None and p.get("keep_intermediate") and tmp.exists():
        arts.append({"kind": "product", "path": str(tmp), "label": "extracted channel (cf32)"})
    best = max(runs, key=lambda r: (r["cadu_frames"], sum(i["has_content"] for i in r["images"])))
    real = [i for i in best["images"] if i["has_content"]]
    decoded = best["cadu_frames"] > 0 and bool(real)
    note = None if decoded else ("no CADU frames: the decoder never achieved frame sync" if not best["cadu_frames"]
                                 else "frames decoded but no image with content")
    return {"input": str(src), "command": best["command"], "return_code": best["return_code"],
            "return_code_note": "this SatDump build exits 1 even after a good decode; success is judged from its outputs",
            "error": note, "duration_s": round((dt.datetime.now(dt.timezone.utc) - t0).total_seconds(), 1),
            "shift_method": method, "channel_plan": plan.as_dict() if plan else None, "channel_extraction": stage or None,
            "satdump_freq_shift_hz": freq_shift, "best_residual_offset_hz": best["residual_offset_hz"],
            "best_target_frequency_hz": best["target_frequency_hz"], "cadu_frames": best["cadu_frames"],
            "images": len(real), "images_blank": len(best["images"]) - len(real), "image_checks": best["images"][:20],
            "decoded": decoded, "search": [{k: r[k] for k in ("residual_offset_hz", "target_frequency_hz", "cadu_frames",
                                                               "return_code", "snr_db_max", "synced_reports")}
                                            | {"images_with_content": sum(i["has_content"] for i in r["images"])} for r in runs],
            "snr_db_max": best.get("snr_db_max"), "synced_reports": best.get("synced_reports"),
            "nosync_reports": best.get("nosync_reports")}, arts


def _image_content(path: pathlib.Path) -> dict[str, Any]:
    """A decoded image counts only if it has real structure (not uniform black/white)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            g = np.asarray(im.convert("L"), dtype=np.float32)
        return {"width": int(g.shape[1]), "height": int(g.shape[0]), "pixel_std": round(float(g.std()), 2),
                "has_content": bool(g.size > 1000 and g.std() > 2.0)}
    except Exception as exc:  # noqa: BLE001
        return {"has_content": False, "error": str(exc)}


def _satdump_sync(log: pathlib.Path) -> dict[str, Any]:
    """Pull lock/sync evidence out of SatDump's progress log."""
    import re
    text = log.read_text(errors="replace") if log.exists() else ""
    snr = [float(x) for x in re.findall(r"SNR\s*:\s*([0-9.]+)\s*dB", text)]
    synced = len(re.findall(r"SYNCED", text))
    nosync = len(re.findall(r"NOSYNC", text))
    return {"snr_db_max": max(snr) if snr else None, "synced_reports": synced, "nosync_reports": nosync}


def main(argv: list[str]) -> int:
    spec = json.loads(pathlib.Path(argv[1]).read_text())
    out = pathlib.Path(spec["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"attempt_id": spec["attempt_id"], "pid": os.getpid(),
                              "started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "inputs": [], "artifacts": []}
    errors = []
    multi = len(spec["inputs"]) > 1
    for n, inp in enumerate(spec["inputs"]):
        tag = f"-{n + 1:02d}" if multi else ""
        try:
            if spec["kind"] == "compare":
                r, arts = compare(spec["inputs"], spec["params"], out)
                result["inputs"].append(r)
                result["artifacts"].extend(arts)
                break
            if spec["kind"] == "decode":
                r, arts = decode(inp, spec["params"], out, tag, spec.get("settings", {}))
                if not r.get("decoded"):
                    errors.append(f"{pathlib.Path(inp['path']).name}: {r.get('error') or 'not decoded'}")
            else:
                r, arts = analyze(inp, spec["params"], out, tag)
            result["inputs"].append(r)
            result["artifacts"].extend(arts)
        except Exception as exc:  # noqa: BLE001 - record and continue with the next input
            errors.append(f"{pathlib.Path(inp['path']).name}: {type(exc).__name__}: {exc}")
    result["errors"] = errors
    result["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    if spec["kind"] == "decode":
        result["ok"] = any(i.get("decoded") for i in result["inputs"])
    else:
        result["ok"] = not errors or bool(result["artifacts"])
    diag = out / "result.json"
    tmp = out / ".result.json.tmp"
    tmp.write_text(json.dumps(result, indent=2, default=str))
    os.replace(tmp, diag)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
