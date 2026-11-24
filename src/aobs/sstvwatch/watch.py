"""The SSTV watch: listen on the HF SSTV calling frequencies, decode every image, archive it and tell Slack.

Loop, per HF watcher (OBS-R10 by default), the pirate watch's way:
  1. Hold the receiver through the broker (piratewatch.lease.LeaseKeeper). The broker ends the lease 30 s before
     any scheduled recording on that receiver and refuses one inside a reservation, so the watch always yields to
     bookings and comes back after them. Without a lease nothing touches the receiver. The lease selects the
     receiver's ANTSW-01 port when one is configured.
  2. The planner picks the next channel (plan.Planner: weighted by band and local time, never a channel another
     watcher is on); the receiver is tuned a little below it and demod.ChannelDemod turns the IQ into USB/LSB/FM
     audio.
  3. detect.Detector looks for parity-checked VIS headers and cuts each transmission out (audio + channel IQ) to
     the end of the image time of the mode the VIS names. A dwell never ends in the middle of a transmission.
  4. Each transmission is decoded in its own process (decodeworker.run_decode: timeout + heartbeat), scored, and
     kept as an image when its quality score passes min_quality; noise is logged but not shown. Good decodes go to
     Slack (notify.Notifier, batched past batch_max in batch_window_s).
ISS passes (145.800 / 437.550 FM) are recorded and decoded by the broker on the receiver that has them; the watch
imports those images into its gallery (iss.py-style scan of the broker's results), and the broker posts them.

`release` / `resume` (control.json) stop and restart the radio like the pirate watch's.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import queue
import signal
import threading
import time
import wave
from typing import Any, Callable

import numpy as np

from ..piratewatch.daemon import read_control, released, write_control
from ..piratewatch.lease import BrokerClient, LeaseError, LeaseKeeper, read_token
from .config import Channel, Settings, Watcher
from .decodeworker import run_decode
from .demod import ChannelDemod
from .detect import Detector, Transmission
from .notify import Notifier
from .plan import Planner
from .quality import audio_snr_db, image_quality
from .radio import RadioError, Source, open_source
from .store import Store, iso

LOG = logging.getLogger("aobs.sstvwatch")
UTC = dt.timezone.utc
TUNE_BELOW_HZ = 12_000            # the receiver sits this far below the channel: its DC spike stays off the audio
ISS_ROOTS = ("/data/archive/starlab/recordings/jobs", "/data/archive/starlab/recordings/auto")

__all__ = ["Station", "HFWatcher", "main", "read_control", "write_control", "released"]


class Interrupted(Exception):
    pass


class Station:
    """What the watchers share: the archive, the planner, the decode queue, notifications, status."""

    def __init__(self, s: Settings, keeper: Callable[[Watcher], LeaseKeeper] | None = None,
                 source: Callable[[Watcher], Source] | None = None,
                 send: Callable[[str, dict[str, Any]], None] | None = None,
                 decoder: Callable[[dict[str, Any], pathlib.Path], dict[str, Any]] | None = None,
                 clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(UTC),
                 iss_roots: tuple[str, ...] = ISS_ROOTS):
        self.s, self.clock, self.iss_roots = s, clock, iss_roots
        s.root.mkdir(parents=True, exist_ok=True)
        os.chmod(s.root, 0o755)                        # the broker (another account) reads thumbnails for Slack
        self.store = Store(s.path("sstvwatch.sqlite3"))
        self.planner = Planner(s)
        self.lock = threading.Lock()
        self.on: dict[str, str] = {}                   # receiver -> channel it is on
        self.stopping = threading.Event()
        self._token: str | None = None
        self.keeper_for = keeper or self._keeper
        self.source_for = source or open_source
        self.notifier = Notifier(send if send is not None else self._send, s.batch_window_s, s.batch_max, s.timezone,
                                 clock)
        self.decoder = decoder or (lambda job, d: run_decode(job, d, s.decode_timeout_s, s.decode_heartbeat_stale_s))
        self.jobs: queue.Queue[tuple[int, dict[str, Any]] | None] = queue.Queue()
        self.watchers = [HFWatcher(self, w) for w in s.watchers]
        self.started_at = clock()
        self.decoded_this_run = 0
        self._status_at = 0.0

    # ------------------------------------------------------------ broker
    def token(self) -> str:
        if self._token is None:
            self._token = read_token(self.s.broker_token_file)
        return self._token

    def client(self) -> BrokerClient:
        return BrokerClient(self.s.broker_url, self.token())

    def _keeper(self, w: Watcher) -> LeaseKeeper:
        return LeaseKeeper(self.client(), w.receiver, self.s.lease_owner, w.lease_port, self.s.lease_ttl_s,
                           self.s.renew_every_s, purpose="SSTV watch (HF calling frequencies)")

    def _send(self, text: str, data: dict[str, Any]) -> None:
        if self.s.notify:
            self.client().call("POST", "/v1/notifications", {"kind": "sstv", "text": text, "data": data})

    # ------------------------------------------------------------ channels
    def take(self, receiver: str, now: dt.datetime):
        with self.lock:
            busy = {c for r, c in self.on.items() if r != receiver}
            d = self.planner.next(now, busy)
            if d is not None:
                self.on[receiver] = d.channel.name
            else:
                self.on.pop(receiver, None)
            return d

    def leave(self, receiver: str) -> None:
        with self.lock:
            self.on.pop(receiver, None)

    # ------------------------------------------------------------ transmissions
    def day_dir(self, t: dt.datetime) -> pathlib.Path:
        d = self.s.path("images", f"{t:%Y}", f"{t:%m}", f"{t:%d}")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def submit(self, tx: Transmission, ch: Channel, receiver: str) -> int:
        """Archive one transmission's audio and IQ and queue its decode. Returns its row id."""
        snr = audio_snr_db(tx.audio[int(tx.header_offset_s * tx.audio_rate):], tx.audio_rate)
        iid = self.store.add(source="watch", status="decoding", start_utc=iso(tx.start), end_utc=iso(tx.end),
                             freq_hz=ch.hz, band=ch.band, channel=ch.name, sideband=ch.mode, receiver=receiver,
                             vis=tx.vis, mode=tx.mode, snr_db=snr,
                             extra={"truncated": tx.truncated, "mode_known": tx.mode_known,
                                    "seconds": round(len(tx.audio) / tx.audio_rate, 1)})
        d = self.day_dir(tx.start)
        stem = f"{iid:06d}"
        wav = d / f"{stem}.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(tx.audio_rate)
            w.writeframes((np.clip(tx.audio, -1, 1) * 32767).astype("<i2").tobytes())
        iq = None
        if self.s.keep_iq:
            iq = d / f"{stem}.cf32"
            tx.iq.astype(np.complex64).tofile(iq)
        self.store.update(iid, audio_path=str(wav), iq_path=str(iq) if iq else None)
        LOG.info("SSTV transmission %d: %s (VIS %s) on %.3f MHz %s, %s", iid, tx.mode, tx.vis, ch.hz / 1e6, ch.mode,
                 receiver + (f", cut short: {tx.truncated}" if tx.truncated else ""))
        self.jobs.put((iid, {"wav": str(wav), "out_dir": str(d), "stem": stem, "vis_mode": tx.mode,
                             "min_quality": self.s.min_quality}))
        return iid

    def decode_loop(self) -> None:
        while True:
            item = self.jobs.get()
            if item is None:
                return
            iid, job = item
            try:
                self.finish(iid, self.decoder(job, self.s.path("work")))
            except Exception as exc:  # noqa: BLE001 - one bad transmission never stops the watch
                LOG.exception("decode of transmission %d failed", iid)
                self.store.update(iid, status="failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                self.jobs.task_done()

    def finish(self, iid: int, res: dict[str, Any]) -> None:
        row = self.store.get(iid) or {}
        extra = {**row.get("extra", {}), **{k: res[k] for k in ("mode_agrees", "complete", "quality_detail", "images")
                                             if k in res}}
        self.store.update(iid, status=res.get("status", "failed"), quality=res.get("quality"),
                          decoded_mode=res.get("decoded_mode"), width=res.get("width"), height=res.get("height"),
                          image_path=res.get("image_path"), thumb_path=res.get("thumb_path"), error=res.get("error"),
                          extra=extra)
        if res.get("status") == "decoded":
            self.decoded_this_run += 1
            LOG.info("SSTV image %d: %s, quality %.2f", iid, res.get("decoded_mode"), res.get("quality") or 0)
            img = self.store.get(iid)
            if img and self.notifier.add(img):
                self.store.update(iid, notified_at=iso(self.clock()))
        else:
            LOG.info("SSTV transmission %d not kept: %s", iid, res.get("error"))

    # ------------------------------------------------------------ ISS (decoded by the broker)
    def import_iss(self) -> int:
        """File the broker's ISS SSTV images (any receiver, any downlink) in the gallery. Returns how many were new."""
        new = 0
        for root in self.iss_roots:
            for res_path in sorted(pathlib.Path(root).glob("*/processing/*/*/result.json")):
                try:
                    res = json.loads(res_path.read_text())
                    att = json.loads(res_path.with_name("attempt.json").read_text())
                except (OSError, ValueError):
                    continue
                if (att.get("params") or {}).get("pipeline") != "sstv":
                    continue
                inp = (att.get("inputs") or [{}])[0]
                first = (res.get("inputs") or [{}])[0]
                checks = {c.get("file"): c for c in first.get("image_checks") or []}
                for a in res.get("artifacts") or []:
                    if a.get("kind") != "image" or "(blank)" in (a.get("label") or ""):
                        continue
                    new += self._import_one(a, att, inp, checks.get(pathlib.Path(a["path"]).name, {}))
        return new

    def _import_one(self, a: dict[str, Any], att: dict[str, Any], inp: dict[str, Any], check: dict[str, Any]) -> int:
        from PIL import Image
        path = pathlib.Path(a["path"])
        if not path.is_file():
            return 0
        if self.store.db.execute("SELECT 1 FROM images WHERE source='iss' AND attempt_id=? AND image_path=?",
                                 (att["attempt_id"], str(path))).fetchone():
            return 0
        try:
            with Image.open(path) as im:
                im = im.convert("RGB")
                q = image_quality(im)
                start = dt.datetime.fromisoformat(inp.get("record_start") or iso(self.clock()).replace("Z", "+00:00"))
                d = self.day_dir(start)
                thumb = d / f"iss-{att['attempt_id']}-{path.stem}.thumb.jpg"
                t = im.copy()
                t.thumbnail((320, 320))
                t.save(thumb, quality=85)
                w, h = im.size
        except OSError:
            return 0
        hz = int((att.get("params") or {}).get("target_frequency_hz") or inp.get("target_frequency_hz") or 0)
        from .config import band_of
        return 1 if self.store.add(source="iss", status="decoded", start_utc=iso(start), end_utc=None, freq_hz=hz,
                                   band=band_of(hz), channel="ISS", sideband="fm", receiver=inp.get("receiver_id"),
                                   mode=check.get("mode") or "unknown", decoded_mode=check.get("mode"),
                                   quality=q["score"], width=w, height=h, image_path=str(path),
                                   thumb_path=str(thumb), audio_path=None, iq_path=inp.get("path"),
                                   recording_id=inp.get("recording_id"), attempt_id=att["attempt_id"],
                                   notified_at="broker", extra={"quality_detail": q, "target": inp.get("target")}) else 0

    # ------------------------------------------------------------ status
    def write_status(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._status_at < 2:
            return
        self._status_at = time.monotonic()
        doc = {"at": iso(self.clock()), "pid": os.getpid(), "started_at": iso(self.started_at),
               "state": self.overall_state(), "watchers": [w.status() for w in self.watchers],
               "decoded_this_run": self.decoded_this_run, "counts": self.store.facets()["counts"],
               "pending_batch": len(self.notifier.pending), "decode_queue": self.jobs.qsize()}
        p = self.s.path("status.json")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2))
        tmp.replace(p)

    def overall_state(self) -> str:
        states = [w.state for w in self.watchers]
        for st in ("receiving", "listening", "released", "waiting", "error", "blocked", "starting"):
            if st in states:
                return st
        return states[0] if states else "stopped"

    # ------------------------------------------------------------ main loop
    def run(self) -> None:
        threading.Thread(target=self.decode_loop, name="sstv-decode", daemon=True).start()
        for w in self.watchers:
            w.start()
        last_iss = 0.0
        try:
            while not self.stopping.wait(1.0):
                self.notifier.tick()
                if self.s.iss_import and time.monotonic() - last_iss > self.s.iss_poll_s:
                    last_iss = time.monotonic()
                    try:
                        n = self.import_iss()
                        if n:
                            LOG.info("filed %d ISS SSTV image(s) from the broker's archive", n)
                    except Exception:  # noqa: BLE001
                        LOG.exception("ISS import failed")
                self.write_status()
        finally:
            for w in self.watchers:
                w.join(timeout=30)
            self.jobs.put(None)
            self.notifier.flush()
            self.write_status(force=True)


class HFWatcher(threading.Thread):
    def __init__(self, st: Station, w: Watcher):
        super().__init__(name=f"sstv-{w.receiver}", daemon=True)
        self.st, self.w, self.s = st, w, st.s
        self.keeper: LeaseKeeper | None = None
        self.state, self.detail = "starting", ""
        self.channel: Channel | None = None
        self.dwell_until: dt.datetime | None = None
        self._ctl_at = 0.0                 # control.json is read at most once a second (not on every IQ block)

    def set_state(self, state: str, detail: str = "") -> None:
        if (state, detail) != (self.state, self.detail):
            LOG.info("%s: %s %s", self.w.receiver, state, detail)
        self.state, self.detail = state, detail

    def status(self) -> dict[str, Any]:
        k = self.keeper
        return {"receiver": self.w.receiver, "state": self.state, "detail": self.detail,
                "channel": self.channel.name if self.channel else None,
                "freq_hz": self.channel.hz if self.channel else None,
                "sideband": self.channel.mode if self.channel else None,
                "dwell_until": iso(self.dwell_until), "lease_held": bool(k and k.held),
                "lease_expires": iso(k.expires_at) if k and k.expires_at else None}

    def check(self) -> None:
        if self.st.stopping.is_set():
            raise Interrupted("shutdown")
        if self.keeper and self.keeper.lost.is_set():
            raise Interrupted(f"lease lost: {self.keeper.lost_reason}")
        if time.monotonic() - self._ctl_at >= 1.0:
            self._ctl_at = time.monotonic()
            if released(read_control(self.s), self.st.clock()):
                raise Interrupted("released for live use")

    def run(self) -> None:
        while not self.st.stopping.is_set():
            try:
                self.cycle()
            except Exception as exc:  # noqa: BLE001 - keep watching
                LOG.exception("%s: watcher cycle failed", self.w.receiver)
                self.set_state("error", f"{type(exc).__name__}: {exc}")
                self.st.stopping.wait(30)

    def cycle(self) -> None:
        ctl = read_control(self.s)
        if released(ctl, self.st.clock()):
            self.set_state("released", f"{self.w.receiver} free" + (f" until {ctl['until']}" if ctl.get("until")
                                                                    else "; run `resume` to restart"))
            self.st.stopping.wait(2)
            return
        if ctl.get("released"):
            write_control(self.s, released=False, until=None)
        self.keeper = self.st.keeper_for(self.w)
        try:
            self.keeper.acquire()
        except LeaseError as exc:
            self.set_state("waiting", f"no lease: {exc}")
            self.st.stopping.wait(self.s.retry_lease_s)
            return
        src: Source | None = None
        try:
            src = self.st.source_for(self.w)
            while True:
                self.check()
                dwell = self.st.take(self.w.receiver, self.st.clock())
                if dwell is None:
                    self.set_state("waiting", "no channel open at this hour")
                    self.st.stopping.wait(30)
                    continue
                self.listen(src, dwell.channel, dwell.seconds)
        except Interrupted as why:
            LOG.info("%s: radio stopped: %s", self.w.receiver, why)
        except RadioError as exc:
            self.set_state("error", str(exc))
            LOG.error("%s: radio error: %s", self.w.receiver, exc)
            self.st.stopping.wait(30)
        finally:
            self.st.leave(self.w.receiver)
            self.channel = None
            if src is not None:
                src.close()
            lost = self.keeper.lost.is_set()
            self.keeper.release()
        if lost:
            self.set_state("waiting", f"lease ended ({self.keeper.lost_reason}); back after the booking")
            self.st.stopping.wait(self.s.retry_lease_s)

    def listen(self, src: Source, ch: Channel, seconds: float) -> None:
        """One dwell on one channel; extended while a transmission is being captured."""
        center = ch.hz - TUNE_BELOW_HZ
        src.tune(center)
        demod = ChannelDemod(src.rate, ch.hz - center, ch.mode, self.s.audio_rate, self.s.ssb_low_hz, self.s.ssb_high_hz)
        det = Detector(self.s.audio_rate, self.s.detect_every_s, self.s.detect_window_s, self.s.pre_vis_s,
                       self.s.post_image_s, self.s.max_transmission_s)
        self.channel = ch
        t0 = self.st.clock()
        self.dwell_until = t0 + dt.timedelta(seconds=seconds)
        self.set_state("listening", f"{ch.name} {ch.hz / 1e6:.3f} MHz {ch.mode.upper()} for {seconds / 60:.0f} min")
        n = 0
        try:
            while True:
                self.check()
                block = src.read()
                at = t0 + dt.timedelta(seconds=n / src.rate)
                n += len(block)
                base, audio = demod(block)
                for tx in det.feed(base, audio, at):
                    self.st.submit(tx, ch, self.w.receiver)
                if det.busy and self.state != "receiving":
                    self.set_state("receiving", f"SSTV on {ch.name} {ch.hz / 1e6:.3f} MHz")
                elif not det.busy and self.state == "receiving":
                    self.set_state("listening", f"{ch.name} {ch.hz / 1e6:.3f} MHz {ch.mode.upper()}")
                if n / src.rate >= seconds and not det.busy:
                    return
        except Interrupted as why:
            tx = det.flush(str(why))
            if tx is not None:
                self.st.submit(tx, ch, self.w.receiver)
            raise


def main(s: Settings) -> int:
    from .control import ControlServer
    st = Station(s)
    server = ControlServer(s, st.token())
    server.start()

    def _term(signum: int, _frame: Any) -> None:
        LOG.info("signal %d: stopping", signum)
        st.stopping.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    try:
        st.run()
    finally:
        server.stop()
    return 0
