"""Slack for good decodes: a thumbnail with frequency, mode and time, through the broker (kind "sstv").

Up to batch_max decodes in any batch_window_s go out one by one; past that they are held and sent together as one
message (count, bands, modes, the best few thumbnails) when the window ends, so a busy band does not flood Slack.
ISS pass images are posted by the broker itself as soon as their decode finishes (always, see SlackService), so the
watch only files them in its gallery.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import deque
from typing import Any, Callable
from zoneinfo import ZoneInfo

LOG = logging.getLogger("aobs.sstvwatch.notify")
Send = Callable[[str, dict[str, Any]], None]
MAX_BATCH_THUMBS = 6


def describe(img: dict[str, Any], tz: str) -> str:
    t = dt.datetime.fromisoformat(img["start_utc"].replace("Z", "+00:00"))
    local = t.astimezone(ZoneInfo(tz))
    snr = f", SNR {img['snr_db']:.0f} dB" if img.get("snr_db") is not None else ""
    return (f"{img['freq_hz'] / 1e6:.3f} MHz {str(img.get('sideband') or '').upper()} ({img['band']}) · {img['mode']} · "
            f"{local:%Y-%m-%d %H:%M} {local.tzname()} · {img.get('receiver') or ''} · quality {img['quality']:.2f}{snr}")


class Notifier:
    def __init__(self, send: Send, window_s: float = 600.0, batch_max: int = 5, tz: str = "UTC",
                 clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)):
        self.send, self.window, self.max, self.tz, self.clock = send, window_s, batch_max, tz, clock
        self.sent: deque[dt.datetime] = deque()        # when each one-by-one message went out
        self.pending: list[dict[str, Any]] = []
        self.pending_since: dt.datetime | None = None

    def _trim(self, now: dt.datetime) -> None:
        while self.sent and (now - self.sent[0]).total_seconds() >= self.window:
            self.sent.popleft()

    def add(self, img: dict[str, Any]) -> str:
        """A good decode. Returns "sent" or "batched"."""
        now = self.clock()
        self._trim(now)
        if not self.pending and len(self.sent) < self.max:
            self._send_one(img)
            self.sent.append(now)
            return "sent"
        if not self.pending:
            self.pending_since = now
        self.pending.append(img)
        return "batched"

    def tick(self) -> bool:
        """Send the held batch once its window has passed. True if a batch went out."""
        if not self.pending or self.pending_since is None:
            return False
        if (self.clock() - self.pending_since).total_seconds() < self.window:
            return False
        self.flush()
        return True

    def flush(self) -> None:
        if not self.pending:
            return
        batch, self.pending, self.pending_since = self.pending, [], None
        best = sorted(batch, key=lambda i: -(i.get("quality") or 0))[:MAX_BATCH_THUMBS]
        bands = ", ".join(sorted({i["band"] for i in batch}))
        modes = ", ".join(sorted({i["mode"] for i in batch}))
        text = (f":frame_with_picture: SSTV watch: {len(batch)} more images in the last {self.window / 60:.0f} min "
                f"({bands}; {modes}). Best {len(best)}:\n" + "\n".join(f"• {describe(i, self.tz)}" for i in best))
        self._deliver(text, {"images": [self._thumb(i) for i in best], "ids": [i["id"] for i in batch], "batch": True})

    def _send_one(self, img: dict[str, Any]) -> None:
        self._deliver(f":frame_with_picture: SSTV {describe(img, self.tz)}",
                      {"images": [self._thumb(img)], "ids": [img["id"]], "batch": False})

    @staticmethod
    def _thumb(img: dict[str, Any]) -> dict[str, Any]:
        return {"path": img.get("thumb_path") or img.get("image_path"),
                "title": f"{img['freq_hz'] / 1e6:.3f} MHz {img['mode']} {img['start_utc']}"}

    def _deliver(self, text: str, data: dict[str, Any]) -> None:
        try:
            self.send(text, {"source": "sstv-watch", **data})
        except Exception as exc:  # noqa: BLE001 - Slack being down never stops the watch
            LOG.warning("SSTV notification failed: %s", exc)
