"""Great-circle distance, bearing and look angle on a spherical Earth (good to ~0.5 % for tracking, not surveying)."""

from __future__ import annotations

import math
from typing import Iterable

R_EARTH_M = 6_371_008.8


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def elevation_deg(lat1: float, lon1: float, alt1_m: float, lat2: float, lon2: float, alt2_m: float) -> float:
    """Look angle above the horizon from point 1 to point 2, with Earth curvature (no refraction)."""
    d = distance_m(lat1, lon1, lat2, lon2)
    if d < 1:
        return 90.0 if alt2_m > alt1_m else -90.0 if alt2_m < alt1_m else 0.0
    theta = d / R_EARTH_M
    r1, r2 = R_EARTH_M + alt1_m, R_EARTH_M + alt2_m
    # slant vector in the observer's local frame
    horiz = r2 * math.sin(theta)
    vert = r2 * math.cos(theta) - r1
    return math.degrees(math.atan2(vert, horiz))


def slant_range_m(lat1: float, lon1: float, alt1_m: float, lat2: float, lon2: float, alt2_m: float) -> float:
    theta = distance_m(lat1, lon1, lat2, lon2) / R_EARTH_M
    r1, r2 = R_EARTH_M + alt1_m, R_EARTH_M + alt2_m
    return math.sqrt(r1 * r1 + r2 * r2 - 2 * r1 * r2 * math.cos(theta))


def path_length_m(points: Iterable[tuple[float, float]]) -> float:
    total, prev = 0.0, None
    for lat, lon in points:
        if prev is not None:
            total += distance_m(prev[0], prev[1], lat, lon)
        prev = (lat, lon)
    return total


COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass(deg: float) -> str:
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]
