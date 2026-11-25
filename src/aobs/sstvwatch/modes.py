"""SSTV modes by VIS code: the name and how long its image takes, so the watch records a transmission to its end.

Durations are the image time after the VIS header (rounded up), from the mode specifications (JL Barber, "Proposal
for SSTV mode specifications", Dayton 2000, and the PD modes' author's tables).
"""

from __future__ import annotations

# VIS code -> (mode name, image seconds)
VIS_MODES: dict[int, tuple[str, float]] = {
    8: ("Robot 36", 36.0),
    12: ("Robot 72", 72.0),
    44: ("Martin 1", 114.3),
    40: ("Martin 2", 58.1),
    60: ("Scottie 1", 109.6),
    56: ("Scottie 2", 71.1),
    76: ("Scottie DX", 268.9),
    55: ("Wraase SC2-180", 182.0),
    93: ("PD 50", 49.7),
    99: ("PD 90", 90.0),
    95: ("PD 120", 126.1),
    98: ("PD 160", 160.9),
    96: ("PD 180", 187.1),
    97: ("PD 240", 248.0),
    94: ("PD 290", 288.7),
    113: ("Pasokon P3", 203.0),
    114: ("Pasokon P5", 305.0),
    115: ("Pasokon P7", 406.0),
}
UNKNOWN_IMAGE_S = 120.0


def mode_of(vis: int | None) -> tuple[str, float, bool]:
    """(name, image seconds, known) for a VIS code."""
    if vis in VIS_MODES:
        name, secs = VIS_MODES[vis]
        return name, secs, True
    return (f"VIS {vis}" if vis is not None else "unknown"), UNKNOWN_IMAGE_S, False


def normalise(name: str | None) -> str:
    """A decoder's mode name in the same spelling as VIS_MODES ("Mode.PD_120", "PD120", "pd 120" -> "PD 120")."""
    t = str(name or "").split(".")[-1].replace("_", " ").strip()
    key = "".join(ch for ch in t.lower() if ch.isalnum())
    for mode, _ in VIS_MODES.values():
        if "".join(ch for ch in mode.lower() if ch.isalnum()) == key:
            return mode
    return t or "unknown"
