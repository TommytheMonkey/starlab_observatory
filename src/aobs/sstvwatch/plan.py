"""Which channel a watcher listens to next, and for how long.

Weighted rotation (deficit round robin): every visit adds each open channel's weight to its credit; the watcher takes
the open channel with the most credit that no other watcher is on, pays its weight back, and dwells
dwell_s x weight (clamped). So 20 m by day (weight 4) gets four times the visits and time of a weight-1 channel, and
two watchers never sit on the same frequency. A transmission that starts during a dwell is always heard to its end
(the watch extends the dwell); the plan only decides between transmissions.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .config import Channel, Settings


@dataclass(frozen=True)
class Dwell:
    channel: Channel
    seconds: float
    daytime: bool
    weight: float


def is_daytime(s: Settings, now: dt.datetime) -> bool:
    local = now.astimezone(ZoneInfo(s.timezone))
    h = local.hour + local.minute / 60
    return s.day_start_hour <= h < s.day_end_hour


class Planner:
    def __init__(self, s: Settings):
        self.s = s
        self.credit: dict[str, float] = {c.name: 0.0 for c in s.channels}

    def next(self, now: dt.datetime, busy: set[str] = frozenset()) -> Dwell | None:
        """The next dwell, avoiding channels in `busy` (held by another watcher). None if nothing is open."""
        day = is_daytime(self.s, now)
        open_ = [(c, c.weight(day)) for c in self.s.channels if c.weight(day) > 0]
        if not open_:
            return None
        for c, w in open_:
            self.credit[c.name] = self.credit.get(c.name, 0.0) + w
        free = [(c, w) for c, w in open_ if c.name not in busy]
        if not free:
            return None
        # most credit first; ties to the heavier channel, then the order in the settings (20 m first)
        order = {c.name: i for i, c in enumerate(self.s.channels)}
        c, w = max(free, key=lambda cw: (self.credit[cw[0].name], cw[1], -order[cw[0].name]))
        self.credit[c.name] -= sum(x for _, x in open_)
        secs = min(self.s.max_dwell_s, max(self.s.min_dwell_s, self.s.dwell_s * w))
        return Dwell(c, secs, day, w)
