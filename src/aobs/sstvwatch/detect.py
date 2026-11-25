"""Find SSTV transmissions in a channel's audio stream and cut each one out (audio and channel IQ).

Every detect_every_s the last detect_window_s of audio goes through sstvdec.find_vis_headers, the parity-checked VIS
validation the archive decoder uses (leader >= 240 ms, 1200 Hz break, leader, start bit, 7 data bits, even parity,
stop bit), so speech, carriers, AFSK and noise are not transmissions. A header seen in overlapping windows counts once.
After a header the detector keeps everything until the image time of the mode the VIS names (plus post_image_s),
from pre_vis_s before the header, then hands the transmission over.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..scheduling.sstvdec import find_vis_headers
from .modes import mode_of

VIS_HEADER_S = 0.91        # leader 300 + break 10 + leader 300 + start, 7 bits, parity, stop (30 ms each)


@dataclass
class Transmission:
    vis: int
    mode: str
    mode_known: bool
    start: dt.datetime                  # the VIS header's start (UTC)
    audio: np.ndarray                   # float32 at audio_rate, from pre_vis_s before the header
    iq: np.ndarray                      # complex64 channel baseband at audio_rate, same span
    audio_rate: int
    header_offset_s: float              # where the header starts inside `audio`
    truncated: str | None = None        # why it ended before the image did (lease lost, hop, max length)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def end(self) -> dt.datetime:
        return self.start + dt.timedelta(seconds=len(self.audio) / self.audio_rate - self.header_offset_s)


class Detector:
    """Feed (iq, audio, time of the block's first sample) blocks; transmissions come back from feed() when complete."""

    def __init__(self, audio_rate: int, every_s: float = 1.0, window_s: float = 3.0, pre_s: float = 2.0,
                 post_s: float = 5.0, max_s: float = 330.0):
        self.rate = int(audio_rate)
        self.every = int(every_s * self.rate)
        self.window = int(window_s * self.rate)
        self.pre = int(pre_s * self.rate)
        self.post_s, self.max_s = post_s, max_s
        self.keep = max(self.window, self.pre) + self.every
        self.audio = np.zeros(0, np.float32)
        self.iq = np.zeros(0, np.complex64)
        self.t0: dt.datetime | None = None        # time of self.audio[0]
        self.since_check = 0
        self.last_header: dt.datetime | None = None
        self.cur: dict[str, Any] | None = None     # the transmission being captured

    @property
    def busy(self) -> bool:
        """A transmission is being captured: the watch must not hop away now."""
        return self.cur is not None

    def _time(self, i: int) -> dt.datetime:
        assert self.t0 is not None
        return self.t0 + dt.timedelta(seconds=i / self.rate)

    def feed(self, iq: np.ndarray, audio: np.ndarray, at: dt.datetime) -> list[Transmission]:
        if self.t0 is None:
            self.t0 = at
        self.audio = np.concatenate([self.audio, audio.astype(np.float32)])
        self.iq = np.concatenate([self.iq, iq.astype(np.complex64)])
        self.since_check += len(audio)
        out: list[Transmission] = []
        if self.cur is not None:
            if len(self.audio) >= self.cur["until"]:
                out.append(self._cut())
        elif self.since_check >= self.every and len(self.audio) >= self.rate:
            self.since_check = 0
            lo = max(0, len(self.audio) - self.window)
            for h in find_vis_headers(self.audio[lo:], self.rate):
                i = lo + int(h["at_s"] * self.rate)
                when = self._time(i)
                if self.last_header and abs((when - self.last_header).total_seconds()) < 2 * VIS_HEADER_S:
                    continue                        # the same header, seen again in the overlapping window
                if i + int(VIS_HEADER_S * self.rate) > len(self.audio):
                    continue                        # not complete yet; the next window sees it whole
                self.last_header = when
                name, secs, known = mode_of(h["vis"])
                length = min(self.max_s, VIS_HEADER_S + secs + self.post_s)
                start = max(0, i - self.pre)
                self.cur = {"vis": h["vis"], "mode": name, "known": known, "header_i": i, "start_i": start,
                            "until": i + int(length * self.rate)}
                break
        if self.cur is None:                        # idle: keep only what a future header's pre-roll needs
            drop = max(0, len(self.audio) - self.keep)
            if drop:
                self.audio, self.iq = self.audio[drop:], self.iq[drop:]
                self.t0 = self._time(drop)
        return out

    def _cut(self, truncated: str | None = None) -> Transmission:
        c = self.cur
        assert c is not None
        a, b = c["start_i"], min(len(self.audio), c["until"])
        tx = Transmission(vis=c["vis"], mode=c["mode"], mode_known=c["known"], start=self._time(c["header_i"]),
                          audio=self.audio[a:b].copy(), iq=self.iq[a:b].copy(), audio_rate=self.rate,
                          header_offset_s=(c["header_i"] - a) / self.rate, truncated=truncated)
        self.audio, self.iq = self.audio[b:], self.iq[b:]
        self.t0 = self._time(b)
        self.cur = None
        self.since_check = 0
        return tx

    def flush(self, why: str) -> Transmission | None:
        """The stream stops (lease lost, shutdown): whatever was being captured, cut short."""
        tx = self._cut(why) if self.cur is not None else None
        self.audio = np.zeros(0, np.float32)
        self.iq = np.zeros(0, np.complex64)
        self.t0 = None
        return tx
