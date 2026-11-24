"""One processing attempt, run as its own low-priority process:
`nice -n 19 ionice -c 3 python -m aobs.scheduling.procworker <attempt.json>`.

kind=decode   SatDump over each input file (optionally a time slice), with the attempt's parameters; pipeline
              "sstv" runs Starlab's SSTV decoder instead (sstvdec: FM audio WAV plus every SSTV image).
kind=analyze  any of: checksum, waterfall, spectrum, stats (IQ diagnostics + power-vs-time plot),
              with optional zoom (center/span) and time range.
kind=transfer raw IQ kept on a receiver host, streamed over SSH (`cat`) into central storage; kept only
              when its size and SHA-256 match what the host reported when the recording ended.

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
        off = int(p.get("frequency_offset_hz") or 0)
        conj = bool(p.get("iq_conjugate"))     # EXPERIMENTAL: conjugating mirrors the spectrum, so +off lands at -off
        plan = dsp.plan_channel(rate, int(inp["center_frequency_hz"]),
                                int(inp["center_frequency_hz"]) + (-off if conj else off), p.get("output_sample_rate_hz"))
        tmp = out / f".channel{tag}.cf32"
        _progress(out, "extracting the channel", 0, 1, None)
        stage = dsp.extract(src, fmt, plan, tmp, p.get("start_s"), p.get("end_s"), conjugate=conj)
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
        for n_run, fo in enumerate(offsets):
            label = f"{fo:+d}Hz" if len(offsets) > 1 else ""
            dest = out / (f"satdump{tag}" + (f"_{label}" if label else ""))
            _progress(out, "decoding", n_run, len(offsets), dest)
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
    note = None if decoded else ("frames decoded but no image with content" if best["cadu_frames"]
                                 else "no CADU frames: " + satdump_failure(runs, in_rate))
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
            "nosync_reports": best.get("nosync_reports"), "sat": best.get("sat"),
            "variant": "EXPERIMENTAL: conjugated IQ (spectrum mirrored before extraction)" if p.get("iq_conjugate") else "standard",
            "satdump_version": settings.get("_satdump_version")}, arts


def _progress(out: pathlib.Path, stage: str, run: int, runs: int, dest: pathlib.Path | None) -> None:
    """Where a decode is, for the archive's progress bar: the stage, which offset run of how many, and the folder
    whose satdump.log carries SatDump's own percentage. Best effort: never fails the decode."""
    try:
        tmp = out / ".progress.json.tmp"
        tmp.write_text(json.dumps({"stage": stage, "run": run, "runs": runs, "dest": str(dest) if dest else None,
                                   "at": dt.datetime.now(dt.timezone.utc).isoformat()}))
        os.replace(tmp, out / "progress.json")
    except OSError:
        pass


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


# SatDump error lines that say nothing about the decode (optional SDR plugins, TLE refresh)
_SD_NOISE = ("libAaroniaRTSAAPI", "curl_easy_perform", "Error updating TLEs", "Failed to get TLE")


def satdump_failure(runs: list[dict[str, Any]], rate: int) -> str:
    """Why SatDump produced no CADU frames, in words, from its own logs over every offset run."""
    import re
    texts = []
    for r in runs:
        try:
            texts.append(re.sub(r"\x1b\[[0-9;]*m", "", pathlib.Path(r["log"]).read_text(errors="replace")))
        except OSError:
            texts.append("")
    n = len(runs)
    errs = [ln.split("(E)", 1)[1].strip() for t in texts for ln in t.splitlines()
            if "(E)" in ln and not any(x in ln for x in _SD_NOISE)]
    if any(r.get("return_code") is None for r in runs):
        return f"SatDump timed out on {sum(r.get('return_code') is None for r in runs)} of {n} run(s)"
    crashed = [r for r in runs if (r.get("return_code") or 0) < 0]
    if crashed:
        what = next((ln.split("what():", 1)[1].strip() for t in texts for ln in t.splitlines() if "what():" in ln), "")
        return f"SatDump crashed (signal {-crashed[0]['return_code']}) on {len(crashed)} of {n} run(s)" + \
            (f": {what[:160]}" if what else "")
    rate_err = next((e for e in errs if "samplerate" in e.lower() or "sample rate" in e.lower()), None)
    if rate_err:
        return f"wrong samplerate ({rate} S/s given): SatDump said: {rate_err[:160]}"
    if not any("Demodulation finished" in t for t in texts):
        return "SatDump never demodulated" + (f": {errs[-1][:160]}" if errs else " (no input read; see satdump.log)")
    sats = [r.get("sat") or {} for r in runs]
    peak = max((s.get("peak_snr_db") or 0.0 for s in sats), default=0.0)
    vit = max((s.get("viterbi_sync_fraction") or 0.0 for s in sats), default=0.0)
    defr = max((s.get("deframer_sync_fraction") or 0.0 for s in sats), default=0.0)
    if vit > 0 and defr == 0:
        return f"no sync: Viterbi locked ({vit:.0%} of reports) but the deframer never synced, at every offset ({n} tried)"
    if vit > 0:
        return f"no frames: deframer synced only briefly ({defr:.0%} of reports) at every offset ({n} tried)"
    if peak <= 0.5:
        return f"no signal: the demodulator found no carrier (peak SNR {peak:.1f} dB) at any of {n} offsets"
    return f"no sync: demodulator peak SNR {peak:.1f} dB but Viterbi never locked at any of {n} offsets " \
           "(signal too weak, or wrong frequency/pipeline)"


def _satdump_sync(log: pathlib.Path) -> dict[str, Any]:
    """Pull lock/sync evidence out of SatDump's progress log."""
    import re
    text = log.read_text(errors="replace") if log.exists() else ""
    snr = [float(x) for x in re.findall(r"SNR\s*:\s*([0-9.]+)\s*dB", text)]
    synced = len(re.findall(r"SYNCED", text))
    nosync = len(re.findall(r"NOSYNC", text))
    from .satquality import parse_satdump_log
    return {"snr_db_max": max(snr) if snr else None, "synced_reports": synced, "nosync_reports": nosync,
            "sat": parse_satdump_log(text)}


def transfer(p: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Copy raw IQ from a receiver host to its central storage path. The copy is written beside the target and
    moved into place only after it checks out, so a failed transfer never leaves a half file under the real name."""
    import hashlib
    import subprocess
    import time
    dest = pathlib.Path(p["dest"])
    if dest.exists():
        return {}, [f"{dest} already exists; nothing was copied"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name("." + dest.name + ".transfer")
    h = hashlib.sha256()
    t0 = time.monotonic()
    try:
        with open(tmp, "wb") as out:
            proc = subprocess.Popen(p["argv"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
            assert proc.stdout is not None
            for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
                out.write(chunk)
                h.update(chunk)
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            rc = proc.wait()
        got = tmp.stat().st_size
        if rc != 0:
            raise RuntimeError(f"the copy failed (exit {rc}): {err.strip()[-300:]}")
        if p.get("bytes") is not None and got != int(p["bytes"]):
            raise RuntimeError(f"copied {got} of {p['bytes']} bytes")
        if p.get("sha256") and h.hexdigest() != p["sha256"]:
            raise RuntimeError("the copy's SHA-256 differs from the one the host reported when the recording ended")
        os.replace(tmp, dest)
    except Exception as exc:  # noqa: BLE001 - reported on the attempt
        tmp.unlink(missing_ok=True)
        return {}, [str(exc)]
    return {"bytes": got, "sha256": h.hexdigest(), "verified": "size and SHA-256" if p.get("sha256") else "size",
            "seconds": round(time.monotonic() - t0, 1), "dest": str(dest), "host": p.get("host")}, []


HEARTBEAT_EVERY_S = 15


def _heartbeat(out: pathlib.Path) -> None:
    """Touch <output_dir>/heartbeat while this process runs, so the broker can tell a frozen worker from a slow one
    (Processor._watchdog). It stops with the process, including while the broker holds it with SIGSTOP."""
    import threading

    def beat() -> None:
        hb = out / "heartbeat"
        while True:
            try:
                hb.write_text(dt.datetime.now(dt.timezone.utc).isoformat())
            except OSError:
                pass
            threading.Event().wait(HEARTBEAT_EVERY_S)
    threading.Thread(target=beat, name="heartbeat", daemon=True).start()


def main(argv: list[str]) -> int:
    spec = json.loads(pathlib.Path(argv[1]).read_text())
    out = pathlib.Path(spec["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    _heartbeat(out)
    result: dict[str, Any] = {"attempt_id": spec["attempt_id"], "pid": os.getpid(),
                              "started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "inputs": [], "artifacts": []}
    errors: list[str] = []
    if spec["kind"] == "transfer":
        r, errors = transfer(spec["params"])
        result.update(inputs=[r] if r else [], errors=errors, ok=not errors,
                      finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        diag = out / "result.json"
        tmp = out / ".result.json.tmp"
        tmp.write_text(json.dumps(result, indent=2, default=str))
        os.replace(tmp, diag)
        return 0 if not errors else 1
    multi = len(spec["inputs"]) > 1
    for n, inp in enumerate(spec["inputs"]):
        tag = f"-{n + 1:02d}" if multi else ""
        try:
            if spec["kind"] == "compare":
                r, arts = compare(spec["inputs"], spec["params"], out)
                result["inputs"].append(r)
                result["artifacts"].extend(arts)
                break
            if spec["kind"] == "decode" and spec["params"].get("pipeline") == "sstv":
                from . import sstvdec           # no image is a normal outcome here (no SSTV event running)
                r, arts = sstvdec.decode(inp, spec["params"], out, tag,
                                         lambda stage, i, n: _progress(out, stage, i, n, None))
                errors.extend(f"{pathlib.Path(inp['path']).name}: {e}" for e in r.get("errors") or [])
            elif spec["kind"] == "decode" and spec["params"].get("pipeline") == "aprs":
                from . import aprsdec           # no packet is a normal outcome too (digipeater off, nobody heard)
                r, arts = aprsdec.decode(inp, spec["params"], out, tag,
                                         lambda stage, i, n: _progress(out, stage, i, n, None),
                                         spec.get("settings", {}).get("atest_path"))
            elif spec["kind"] == "decode":
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
    if spec["kind"] == "decode" and spec["params"].get("pipeline") in ("sstv", "aprs"):
        result["ok"] = bool(result["inputs"])        # it ran; whether it found images/packets is in the inputs
    elif spec["kind"] == "decode":
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
