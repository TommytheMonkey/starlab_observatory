"""The Starlab Observatory logo, inlined as a data: URI so pages and saved reports stay self-contained."""

from __future__ import annotations

import base64
import functools
import pathlib

LOGO = pathlib.Path(__file__).resolve().parent / "assets" / "starlab_logo.png"
LOGO_ALT = "Starlab Observatory"


@functools.cache
def logo_data_uri() -> str:
    return "data:image/png;base64," + base64.b64encode(LOGO.read_bytes()).decode("ascii")
