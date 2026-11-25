"""APRS packet fields (aprslib), worked out once when a decode is written and stored next to the raw TNC2 line.

aprsdec writes every packet's parse into aprs.json ("aprs" per packet), and scripts/backfill_aprs_packets.py does the
same for decodes written before this existed, so the Recordings page shows fields without parsing anything itself.
A packet aprslib cannot parse keeps its raw TNC2 line with parsed=False and the reason; no packet is ever dropped.

Direwolf prints control bytes as <0xNN>; they are turned back into bytes before parsing (Mic-E packs position, speed
and course into them) and the stored tnc2/info keep Direwolf's form for display.

Mic-E (APRS 1.0.1 ch. 10, and the Mic-E type codes of the APRS device-identification list): the status message from
the three message bits, and the radio or tracker from the type code at the start and end of the comment, e.g.
"]…=" for a Kenwood TM-D710, which is then removed from the comment.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

PLACEHOLDER_DEG = 1.0                # within this of 0,0: a tracker without a GPS fix, not a position
_CTRL = re.compile(r"<0x([0-9a-fA-F]{2})>")
_FILE_START = re.compile(r"_(\d{8}T\d{6}Z)_")

# Mic-E type codes: (where, prefix, suffix, vendor, model). Kenwood radios mark the start of the comment ("comment"
# prefix); the others are known by the data type byte (` or ') and the comment's last two characters. "" means none.
MIC_E_DEVICES: list[tuple[str, str, str, str, str]] = [
    ("comment", ">", "=", "Kenwood", "TH-D72"), ("comment", ">", "^", "Kenwood", "TH-D74"),
    ("comment", ">", "&", "Kenwood", "TH-D75"), ("comment", ">", "", "Kenwood", "TH-D7A"),
    ("comment", "]", "=", "Kenwood", "TM-D710"), ("comment", "]", "", "Kenwood", "TM-D700"),
    ("type", "`", "_ ", "Yaesu", "VX-8"), ("type", "`", '_"', "Yaesu", "FTM-350"), ("type", "`", "_#", "Yaesu", "VX-8G"),
    ("type", "`", "_$", "Yaesu", "FT1D"), ("type", "`", "_%", "Yaesu", "FTM-400DR"),
    ("type", "`", "_)", "Yaesu", "FTM-100D"), ("type", "`", "_(", "Yaesu", "FT2D"), ("type", "`", "_0", "Yaesu", "FT3D"),
    ("type", "`", "_3", "Yaesu", "FT5D"), ("type", "`", "_1", "Yaesu", "FTM-300D"), ("type", "`", "_2", "Yaesu", "FTM-200D"),
    ("type", "`", "_4", "Yaesu", "FTM-500D"), ("type", "`", "(5", "Anytone", "D578UV"),
    ("type", "`", "(8", "Anytone", "D878UV"), ("type", "`", ":4", "SCS", "P4dragon DR-7400 modem"),
    ("type", "`", ":8", "SCS", "P4dragon DR-7800 modem"), ("type", "'", "|3", "Byonics", "TinyTrack3"),
    ("type", "'", "|4", "Byonics", "TinyTrack4"), ("type", "'", ":4", "SCS", "P4dragon DR-7400 modem"),
    ("type", "'", ":8", "SCS", "P4dragon DR-7800 modem"),
]


def unescape(text: str) -> str:
    """Direwolf's <0xNN> notation back to the bytes it stands for."""
    return _CTRL.sub(lambda m: chr(int(m.group(1), 16)), text)


def recording_start(path: str | None) -> dt.datetime | None:
    """The recording's start time from its file name (…_20261124T180816Z_…), as Starlab names recordings."""
    m = _FILE_START.search(path or "")
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)


def mic_e_device(comment: str, data_type: str = "") -> tuple[dict[str, str] | None, str]:
    """The Mic-E type code of a packet: ({"vendor", "model", "code"} or None, the comment without the code).
    data_type is the info field's first character (` or ')."""
    c = comment or ""
    for where, pre, suf, vendor, model in sorted(MIC_E_DEVICES, key=lambda d: -len(d[2])):
        if where == "comment" and c.startswith(pre) and (not suf or c.endswith(suf)) and len(c) >= len(pre) + len(suf):
            return {"vendor": vendor, "model": model, "code": pre + ("…" + suf if suf else "")}, c[len(pre):len(c) - len(suf)]
        if where == "type" and data_type == pre and suf and c.endswith(suf):
            return {"vendor": vendor, "model": model, "code": pre + "…" + suf}, c[:len(c) - len(suf)]
    return None, c


def _num(v: Any, nd: int = 6) -> float | None:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def parse(packet: dict[str, Any]) -> dict[str, Any]:
    """One packet as aprsdec wrote it ({"tnc2", "source", "destination", "path", "info", "at_s"}) -> its fields.

    Never raises: {"parsed": False, "error": …} when aprslib cannot make sense of it."""
    tnc2 = packet.get("tnc2") or ""
    try:
        import aprslib
    except ImportError:
        return {"parsed": False, "error": "aprslib is not installed"}
    raw = unescape(tnc2).rstrip("\r\n")
    try:
        f = aprslib.parse(raw)
    except Exception as exc:  # noqa: BLE001 - aprslib raises its own and plain errors; the raw line is still shown
        return {"parsed": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    out: dict[str, Any] = {"parsed": True, "error": None, "format": f.get("format")}
    lat, lon = _num(f.get("latitude")), _num(f.get("longitude"))
    if lat is not None and lon is not None:
        placeholder = abs(lat) < PLACEHOLDER_DEG and abs(lon) < PLACEHOLDER_DEG
        out["position"] = {"latitude": lat, "longitude": lon, "placeholder": placeholder,
                           "note": "placeholder position (no GPS fix)" if placeholder else None,
                           "ambiguity": f.get("posambiguity")}
    for key, name, nd in (("speed", "speed_kmh", 1), ("course", "course_deg", 0), ("altitude", "altitude_m", 1)):
        if f.get(key) is not None:
            out[name] = _num(f[key], nd)
    if f.get("symbol") is not None:
        out["symbol"] = {"table": f.get("symbol_table"), "code": f.get("symbol")}
    comment = f.get("comment")
    if f.get("format") == "mic-e":
        bits, mtype = f.get("mbits"), f.get("mtype") or ""
        code, _, status = mtype.partition(": ")
        out["mic_e"] = {"message_bits": bits, "code": code or None, "status": status or mtype or None}
        dev, comment = mic_e_device(comment or "", unescape(packet.get("info") or "")[:1])
        out["device"] = dev
    if comment is not None:
        out["comment"] = comment.strip() or None
    for key in ("text", "status", "message_text", "addresse", "msgNo", "object_name", "alive", "telemetry", "weather",
                "phg", "rng", "daodatumbyte"):
        if f.get(key) not in (None, "", {}):
            out[{"addresse": "addressee", "msgNo": "message_number"}.get(key, key)] = f[key]
    return out


def enrich(packets: list[dict[str, Any]], recording: str | None) -> list[dict[str, Any]]:
    """Every packet with its fields ("aprs") and UTC time (recording start from the file name + at_s), in order."""
    start = recording_start(recording)
    out = []
    for p in packets:
        q = dict(p)
        q["aprs"] = parse(p)
        q["time_utc"] = ((start + dt.timedelta(seconds=float(p["at_s"]))).isoformat()
                         if start is not None and p.get("at_s") is not None else None)
        out.append(q)
    return out
