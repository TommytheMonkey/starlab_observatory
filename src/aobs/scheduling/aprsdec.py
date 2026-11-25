"""APRS decoding of a recording's raw IQ (the ISS digipeater on 145.825 MHz, packets on 437.825 MHz).

Recordings choose this decoder with the built-in pipeline name `aprs` (the ISS's APRS downlinks do), so bookings,
profiles and decode requests treat it like any SatDump pipeline.

  1. FM audio exactly as the SSTV decoder makes it (sstvdec.iq_to_wav): the target shifted to 0 Hz, ±20 kHz
     low-passed and FM-demodulated, then 48 kHz mono with the Doppler drift removed by the DC block. The WAV is
     kept as an artifact;
  2. Direwolf's offline decoder `atest` (installed with Direwolf) demodulates AFSK 1200 (Bell 202, AX.25) from it;
  3. every decoded frame is saved next to the recording: aprs.txt (one TNC2 line per packet, as APRS-IS shows
     them, with the time into the recording) and aprs.json (the same, split into source, path and payload, with
     each packet's fields from aprslib and its UTC time: aprsparse.enrich).

No packet is a normal outcome: the digipeater is not always on and a pass may hear no station.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import shutil
import subprocess
from typing import Any

from . import aprsparse, sstvdec

PIPELINE = "aprs"
BAUD = 1200
NO_PACKETS = "no APRS packets (normal when the digipeater is off or no station was heard)"
TIMEOUT_S = 900
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_DECODED = re.compile(r"^DECODED\[\d+\]\s+(?:(\d+):)?(\d+(?:\.\d+)?)\b")
_FRAME = re.compile(r"^\[\d+(?:\.\d+)?[^\]]*\]\s(.+)$")
_TNC2 = re.compile(r"^([A-Z0-9-]{1,9}\*?)>([^:,]+)((?:,[^:]*)?):(.*)$", re.S)


def binary(path: str | None = None) -> str | None:
    """Direwolf's offline decoder, or None when Direwolf is not installed."""
    return shutil.which(path or "atest")


def available(path: str | None = None) -> bool:
    return binary(path) is not None


def parse_atest(text: str) -> list[dict[str, Any]]:
    """The frames atest printed: [{"at_s", "tnc2", "source", "destination", "path", "info"}], in order."""
    out: list[dict[str, Any]] = []
    at: float | None = None
    for line in _ANSI.sub("", text).splitlines():
        line = line.rstrip()
        m = _DECODED.match(line)
        if m:
            at = int(m.group(1) or 0) * 60 + float(m.group(2))
            continue
        m = _FRAME.match(line)
        if not m:
            continue
        tnc2 = m.group(1)
        p = _TNC2.match(tnc2)
        out.append({"at_s": round(at, 3) if at is not None else None, "tnc2": tnc2,
                    "source": p.group(1).rstrip("*") if p else None, "destination": p.group(2) if p else None,
                    "path": [x for x in (p.group(3) or "").split(",") if x] if p else [],
                    "info": p.group(4) if p else None})
        at = None
    return out


def run_atest(wav: pathlib.Path, path: str | None = None) -> tuple[list[dict[str, Any]], str]:
    """Decode AFSK 1200 from the WAV with Direwolf (atest). Returns the frames and atest's own output."""
    exe = binary(path)
    if exe is None:
        raise RuntimeError("Direwolf is not installed (its offline decoder atest is not on PATH)")
    r = subprocess.run([exe, "-B", str(BAUD), str(wav)], capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    text = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 and not parse_atest(text):
        raise RuntimeError(f"atest exited with {r.returncode}: {_ANSI.sub('', text).strip()[-300:]}")
    return parse_atest(text), text


def _clock(s: float | None) -> str:
    return f"{int(s // 60):02d}:{s % 60:06.3f}" if s is not None else "--:--.---"


def decode(inp: dict[str, Any], p: dict[str, Any], out: pathlib.Path, tag: str, progress: Any = None,
           atest_path: str | None = None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """procworker entry: one input file -> WAV (audio artifact) + the decoded packets (aprs.txt, aprs.json)."""
    t0 = dt.datetime.now(dt.timezone.utc)
    src = pathlib.Path(inp["path"])
    plan = sstvdec.channel_plan(int(inp["sample_rate_hz"]), int(inp["center_frequency_hz"]), int(p["target_frequency_hz"]))
    wav = out / f"aprs{tag}.wav"
    if progress:
        progress("demodulating FM to audio", 0, 2)
    stage = sstvdec.iq_to_wav(src, inp["format"], plan, wav, p.get("start_s"), p.get("end_s"))
    arts: list[dict[str, str]] = [{"kind": "audio", "path": str(wav), "label": "FM audio, 48 kHz mono (WAV)"}]
    if progress:
        progress("decoding APRS (Direwolf, AFSK 1200)", 1, 2)
    packets, raw = run_atest(wav, atest_path)
    packets = aprsparse.enrich(packets, str(src))        # fields parsed once, here; a packet that does not parse stays
    txt, js, log = out / f"aprs{tag}.txt", out / f"aprs{tag}.json", out / f"aprs{tag}.atest.log"
    txt.write_text("".join(f"{_clock(k['at_s'])}  {k['tnc2']}\n" for k in packets))
    js.write_text(json.dumps({"recording": str(src), "target_frequency_hz": plan["target_frequency_hz"],
                              "decoder": f"direwolf atest -B {BAUD}", "packets": packets}, indent=1))
    log.write_text(_ANSI.sub("", raw))
    stations = sorted({k["source"] for k in packets if k["source"]})
    arts += [{"kind": "product", "path": str(txt), "label": f"APRS packets, {len(packets)} (TNC2 text)"},
             {"kind": "product", "path": str(js), "label": "APRS packets (JSON)"},
             {"kind": "log", "path": str(log), "label": "Direwolf atest output"}]
    res = {"input": str(src), "decoder": PIPELINE, "library": "direwolf atest", "baud": BAUD, "channel_plan": plan,
           "audio": {**stage, "wav": str(wav)}, "packets": len(packets), "stations": stations,
           "packets_file": str(txt), "decoded": bool(packets), "images": 0, "cadu_frames": 0, "errors": [],
           "note": None if packets else NO_PACKETS,
           "duration_s": round((dt.datetime.now(dt.timezone.utc) - t0).total_seconds(), 1)}
    return res, arts
