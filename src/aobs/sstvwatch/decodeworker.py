"""One transmission's decode, in its own process: `python -m aobs.sstvwatch.decodeworker <job.json>`.

The watch's own lightweight path (an SSTV image takes seconds to decode; it never queues behind the broker's
METEOR decodes), with the processing queue's protections: the worker touches <job dir>/heartbeat while it runs and
the parent (run_decode) stops it on decode_timeout_s, or when the heartbeat goes quiet for decode_heartbeat_stale_s
(SIGTERM, then SIGKILL).

Job: {"wav", "out_dir", "stem", "vis_mode", "min_quality"}. Result (<out_dir>/<stem>.result.json): the best image's
mode, size, quality, and the PNG and thumbnail paths; status decoded / noise / failed.
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
from typing import Any

THUMB_PX = 320


def library_mode(name: str | None) -> Any:
    """The sstv (sstv-py) Mode for one of modes.VIS_MODES's names ("PD 120" -> Mode.PD_120), or None."""
    try:
        import sstv
    except ImportError:
        return None
    key = str(name or "").upper().replace(" ", "_").replace("-", "_").replace("WRAASE", "WRASSE")
    return getattr(getattr(sstv, "Mode", None), key, None)


def decode_samples(wav: pathlib.Path, vis_mode: str | None) -> list[dict[str, Any]]:
    """Decode with the mode the VIS header named (sstv-py's mode=), so a header the library would misread cannot
    change the mode; if that finds nothing, let the library read the header itself. Other libraries: decode_wav."""
    from ..scheduling.sstvdec import _mode_name, _read_wav, decode_wav, library
    lib = library()
    if not lib or lib["kind"] != "rust":
        return decode_wav(wav)
    import sstv
    x, sr = _read_wav(wav)
    forced = library_mode(vis_mode)
    imgs = sstv.decode(x, sr, mode=forced) if forced is not None else []
    if not imgs:
        imgs = sstv.decode(x, sr)
    return [{"image": im, "mode": _mode_name(im.info.get("sstv_mode")), "complete": bool(im.info.get("sstv_complete", True)),
             "forced": forced is not None} for im in imgs]


def decode_job(job: dict[str, Any]) -> dict[str, Any]:
    from .modes import normalise
    from .quality import image_quality
    out = pathlib.Path(job["out_dir"])
    stem = job["stem"]
    try:
        found = decode_samples(pathlib.Path(job["wav"]), job.get("vis_mode"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    scored = []
    for f in found:
        if f.get("image") is None:
            continue
        q = image_quality(f["image"])
        scored.append((q["score"], f, q))
    if not scored:
        return {"status": "failed", "error": "the decoder found no image after the VIS header", "images": 0}
    # the decoder reads the VIS itself; prefer its image whose mode is the one our header check named
    want = normalise(job.get("vis_mode"))
    scored.sort(key=lambda t: (normalise(t[1]["mode"]) == want, t[0]), reverse=True)
    score, best, q = scored[0]
    img = best["image"].convert("RGB")
    res = {"decoded_mode": normalise(best["mode"]), "mode_agrees": normalise(best["mode"]) == want,
           "complete": bool(best.get("complete", True)), "quality": score, "quality_detail": q,
           "width": img.width, "height": img.height, "images": len(scored)}
    if score < float(job.get("min_quality", 0.35)):
        return {**res, "status": "noise", "error": f"decode-quality score {score:.2f} is below "
                                                   f"{float(job.get('min_quality', 0.35)):.2f}: mostly noise"}
    png, thumb = out / f"{stem}.png", out / f"{stem}.thumb.jpg"
    img.save(png)
    t = img.copy()
    t.thumbnail((THUMB_PX, THUMB_PX))
    t.save(thumb, quality=85)
    return {**res, "status": "decoded", "image_path": str(png), "thumb_path": str(thumb)}


def main(argv: list[str]) -> int:
    job_path = pathlib.Path(argv[1])
    job = json.loads(job_path.read_text())
    hb = job_path.with_name(job_path.stem + ".heartbeat")

    def beat() -> None:
        while True:
            try:
                hb.write_text(str(time.time()))
            except OSError:
                pass
            time.sleep(2)
    threading.Thread(target=beat, daemon=True).start()
    res = decode_job(job)
    out = pathlib.Path(job["out_dir"]) / f"{job['stem']}.result.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, indent=2))
    os.replace(tmp, out)
    return 0


def run_decode(job: dict[str, Any], job_dir: pathlib.Path, timeout_s: float, stale_s: float,
               python: str = sys.executable, poll_s: float = 0.5, grace_s: float = 5.0) -> dict[str, Any]:
    """Run one decode in a child process and wait for it, with the timeout and heartbeat watchdog."""
    job_dir.mkdir(parents=True, exist_ok=True)
    jp = job_dir / f"{job['stem']}.job.json"
    jp.write_text(json.dumps(job))
    hb = jp.with_name(jp.stem + ".heartbeat")
    result = pathlib.Path(job["out_dir"]) / f"{job['stem']}.result.json"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(pathlib.Path(__file__).resolve().parents[2]),
                                                                    os.environ.get("PYTHONPATH")])))
    p = subprocess.Popen([python, "-m", "aobs.sstvwatch.decodeworker", str(jp)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True, env=env)
    t0 = time.monotonic()
    why = None
    while p.poll() is None:
        time.sleep(poll_s)
        age = time.monotonic() - t0
        try:
            quiet = time.time() - hb.stat().st_mtime
        except OSError:
            quiet = age                      # no heartbeat yet: counts from the start
        if age > timeout_s:
            why = f"timed out after {age:.0f} s (limit {timeout_s:.0f} s)"
        elif quiet > stale_s:
            why = f"worker stopped responding: no heartbeat for {quiet:.0f} s"
        if why:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    p.wait(grace_s)
                    break
                except subprocess.TimeoutExpired:
                    continue
            break
    err = (p.stderr.read().decode(errors="replace")[-500:] if p.stderr else "").strip()
    for f in (jp, hb):
        f.unlink(missing_ok=True)
    if why:
        result.unlink(missing_ok=True)
        return {"status": "failed", "error": f"decode {why}"}
    try:
        return json.loads(result.read_text())
    except (OSError, ValueError):
        return {"status": "failed", "error": f"decode worker ended without a result (exit {p.returncode}): {err}"}
    finally:
        result.unlink(missing_ok=True)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.exit(main(sys.argv))
