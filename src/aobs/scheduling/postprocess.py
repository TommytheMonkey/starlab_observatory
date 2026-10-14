"""After a recording: checksum, waterfall/spectrum images, SatDump decode, metadata sidecar."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import subprocess
from typing import Any

import numpy as np

DTYPES = {"cu8": (np.uint8, 2), "cs8": (np.int8, 2), "cs16": (np.int16, 2), "cf32": (np.float32, 2)}
PRODUCT_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".cadu", ".cbor"}


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _iq(raw: bytes, fmt: str) -> np.ndarray:
    dtype, _ = DTYPES[fmt]
    a = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if fmt == "cu8":
        a = (a - 127.4) / 128.0
    elif fmt == "cs8":
        a = a / 128.0
    elif fmt == "cs16":
        a = a / 32768.0
    return a[0::2] + 1j * a[1::2]


def plots(raw_path: pathlib.Path, fmt: str, rate: int, center: int, waterfall: pathlib.Path | None,
          spectrum: pathlib.Path | None, rows: int = 600, nfft: int = 2048) -> dict[str, Any]:
    """Waterfall from `rows` evenly spaced FFT frames across the file, and the mean spectrum."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dtype, comps = DTYPES[fmt]
    bps = np.dtype(dtype).itemsize * comps
    total = raw_path.stat().st_size // bps
    frames = 8                                   # averaged FFTs per waterfall row
    need = nfft * frames
    if total < need:
        raise RuntimeError("recording too short to plot")
    rows = min(rows, total // need)
    window = np.hanning(nfft).astype(np.float32)
    img = np.empty((rows, nfft), np.float32)
    with raw_path.open("rb") as fh:
        for i in range(rows):
            fh.seek(int(i * (total - need) / max(1, rows - 1)) * bps)
            x = _iq(fh.read(need * bps), fmt)[: need].reshape(frames, nfft)
            p = np.abs(np.fft.fftshift(np.fft.fft(x * window, axis=1), axes=1)) ** 2
            img[i] = 10 * np.log10(p.mean(axis=0) + 1e-12)
    freqs = (center + np.fft.fftshift(np.fft.fftfreq(nfft, 1 / rate))) / 1e6
    seconds = total / rate
    lo, hi = np.percentile(img, [5, 99.7])
    out: dict[str, Any] = {"rows": rows, "nfft": nfft, "seconds": round(seconds, 1)}
    if waterfall:
        fig, ax = plt.subplots(figsize=(12, 8), dpi=100)
        ax.imshow(img, aspect="auto", cmap="viridis", vmin=lo, vmax=hi, origin="upper",
                  extent=(freqs[0], freqs[-1], seconds, 0))
        ax.set_xlabel("Frequency (MHz)")
        ax.set_ylabel("Time from start (s)")
        ax.set_title(raw_path.name, fontsize=9)
        fig.tight_layout()
        fig.savefig(waterfall)
        plt.close(fig)
        out["waterfall"] = str(waterfall)
    if spectrum:
        mean = 10 * np.log10(np.mean(10 ** (img / 10), axis=0))
        peak = img.max(axis=0)
        fig, ax = plt.subplots(figsize=(12, 4), dpi=100)
        ax.plot(freqs, mean, lw=0.8, label="mean")
        ax.plot(freqs, peak, lw=0.6, alpha=0.6, label="peak hold")
        ax.set_xlabel("Frequency (MHz)")
        ax.set_ylabel("Relative power (dB)")
        ax.legend(loc="upper right")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(spectrum)
        plt.close(fig)
        out["spectrum"] = str(spectrum)
        out["noise_floor_db"] = round(float(np.median(mean)), 2)
        out["peak_over_floor_db"] = round(float(peak.max() - np.median(mean)), 2)
    return out


def satdump_command(settings: dict[str, Any], sd: dict[str, Any], raw: str, out_dir: str, freq_shift: int | None) -> list[str]:
    cmd = ["nice", "-n", str(int(settings.get("satdump_nice", 10))), settings.get("satdump_path", "/usr/bin/satdump"),
           sd["pipeline"], "baseband", raw, out_dir, "--samplerate", str(sd["sample_rate_hz"]),
           "--baseband_format", sd["baseband_format"],
           # SatDump 1.2.2 here: with the demodulator and decoder fused ("run at once"), the decoder reads
           # the raw baseband as if it were symbols and nothing ever syncs. Keep the stages separate.
           "--disable_multi_modules"]
    if freq_shift:
        cmd += ["--freq_shift", str(freq_shift)]
    for k, v in (sd.get("additional_parameters") or {}).items():
        if v is True:
            cmd.append(f"--{k}")
        elif v is not False:
            cmd += [f"--{k}", str(v)]
    return cmd


def run_satdump(settings: dict[str, Any], cmd: list[str], out_dir: pathlib.Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "satdump.log"
    started = dt.datetime.now(dt.timezone.utc)
    with log.open("wb") as fh:
        try:
            rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                timeout=int(settings.get("satdump_timeout_seconds", 3600))).returncode
            error = None if rc == 0 else f"satdump exit {rc}"
        except subprocess.TimeoutExpired:
            rc, error = None, "satdump timed out"
    products = sorted(str(p.relative_to(out_dir)) for p in out_dir.rglob("*") if p.suffix.lower() in PRODUCT_EXT)
    images = [p for p in products if pathlib.Path(p).suffix.lower() in {".png", ".jpg", ".jpeg"}]
    return {"command": cmd, "return_code": rc, "error": error, "log": str(log), "output_directory": str(out_dir),
            "products": products, "images": len(images), "started_at": started.isoformat(),
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "decoded": rc == 0 and bool(images)}


def postprocess(job: dict[str, Any], capture: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Runs every requested stage; a failing stage is recorded and the others still run."""
    spec, paths = job["spec"], job["paths"]
    pp = spec.get("postprocess") or {}
    radio, rec = spec["radio"], spec["recording"]
    raw = pathlib.Path(paths["raw"])
    out: dict[str, Any] = {"errors": []}
    if not raw.exists():
        out["errors"].append(f"raw file {raw} is missing")
    else:
        if pp.get("checksum", True):
            out["sha256"] = sha256(raw)
        if pp.get("generate_waterfall") or pp.get("generate_spectrum"):
            try:
                out["plots"] = plots(raw, rec["format"], int(radio["sample_rate_hz"]), int(radio["center_frequency_hz"]),
                                     raw.with_name(raw.name + ".waterfall.png") if pp.get("generate_waterfall") else None,
                                     raw.with_name(raw.name + ".spectrum.png") if pp.get("generate_spectrum") else None)
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"plots: {exc}")
        sd = spec.get("satdump") or {}
        if sd.get("enabled") and sd.get("operation") == "decode_after_recording":
            cmd = satdump_command(settings, sd, str(raw), paths["decode"], spec["resolved"].get("satdump_freq_shift_hz"))
            try:
                out["satdump"] = run_satdump(settings, cmd, pathlib.Path(paths["decode"]))
                if out["satdump"]["error"]:
                    out["errors"].append(out["satdump"]["error"])
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"satdump: {exc}")
            if pp.get("retain_raw_iq") is False and (out.get("satdump") or {}).get("decoded"):
                raw.unlink()
                out["raw_deleted"] = "retain_raw_iq=false and the decode produced images"
    write_sidecar(job, capture, out)
    return out


def write_sidecar(job: dict[str, Any], capture: dict[str, Any], post: dict[str, Any]) -> None:
    spec, paths = job["spec"], job["paths"]
    r, radio, rx = spec["resolved"], spec["radio"], spec["receiver"]
    sd = spec.get("satdump") or {}
    data = {
        "job_id": job["job_id"], "job_name": job["job_name"], "source": job["source"],
        "target": spec.get("target"), "satellite": (spec.get("target") or {}).get("satellite") or job.get("target"),
        "receiver": r["receiver_id"], "receiver_serial": r["device_serial"], "sdr_driver": r["driver"],
        "engine": spec["recording"]["engine"], "antenna": rx.get("antenna"), "signal_path": rx.get("signal_path"),
        "switch_port": r.get("switch_port"), "rf_path": r.get("rf_path"),
        "center_frequency_hz": radio["center_frequency_hz"], "target_frequency_hz": radio.get("target_frequency_hz"),
        "sample_rate_hz": radio["sample_rate_hz"], "bandwidth_hz": radio.get("bandwidth_hz"),
        "gain_mode": radio.get("gain_mode"), "gain_db": radio.get("gain_db"), "gain_elements": radio.get("gain_elements"),
        "agc": radio.get("agc"), "bias_tee": radio.get("bias_tee"), "ppm_correction": radio.get("ppm_correction"),
        "start_time": r["record_start"], "end_time": r["record_end"],
        "reservation_start": r.get("reservation_start"), "reservation_end": r.get("reservation_end"),
        "actual_recording_start": capture.get("actual_start"), "actual_recording_end": capture.get("actual_end"),
        "file_format": spec["recording"]["format"], "recording_filename": paths["raw"],
        "recording_size": capture.get("bytes"), "expected_size": capture.get("expected_bytes"),
        "capture": capture, "postprocess": post,
        "satdump_pipeline": sd.get("pipeline") if sd.get("enabled") else None,
        "satdump_settings": sd if sd.get("enabled") else None, "decode_output": paths.get("decode"),
        "notes": (spec.get("metadata") or {}).get("notes") or (spec.get("target") or {}).get("notes"),
        "request": spec,
        "written_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    side = pathlib.Path(paths["sidecar"])
    side.parent.mkdir(parents=True, exist_ok=True)
    tmp = side.with_name("." + side.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    os.replace(tmp, side)
