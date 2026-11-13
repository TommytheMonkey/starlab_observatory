"""The satcom pirate watch job: sweep 243-270 MHz on OBS-R05, record voice-width bursts, transcribe them.

Loop:
  1. Hold R05 through the broker (LeaseKeeper). Without a lease nothing touches the dongle.
  2. Run rtl_power continuously; every sweep goes to the history and the baseline.
  3. A candidate (15-25 kHz wide, >8 dB over the floor, not a learned steady carrier) stops the
     sweep; rtl_fm records NFM on that channel until 10 s without a carrier, then sweeping resumes.
  4. Each clip is queued for Whisper; hits and the activity heatmap are updated.

A receiver that stays on USB but streams nothing (rtl_power no data, usb_reset_after times in a row) gets a USB
reset of its port while the lease is still held; the watch resumes once the serial is back. Past
usb_resets_per_hour it stops (control.json usb_halted) and alerts Slack through the broker; `resume` lifts it.

`release` (control.json) stops the radio and hands the lease back so SDR++ server can use R05;
`resume` takes it back once no other process holds the dongle. Losing the lease (a scheduled
recording) stops the radio at once and retries later.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import signal
import threading
import time
from typing import Any, Callable

import numpy as np

from . import heatmap
from .audio import ClipState, NoiseSquelch, write_wav
from .config import Settings
from .lease import BrokerClient, LeaseError, LeaseKeeper, read_token
from .radio import (BiasForced, BiasOff, DeviceIndex, Holders, RadioError, Stream, UsbPort, UsbReset, audio_command,
                    default_bias_off, default_device_index, default_holders, default_usb_port, default_usb_reset,
                    eeprom_bias_forced, sweep_command)
from .spectrum import Baseline, Candidate, Sweep, SweepAssembler, channel_of
from .store import History, Store, iso
from .transcribe import Transcriber

LOG = logging.getLogger("aobs.piratewatch")
UTC = dt.timezone.utc


class Interrupted(Exception):
    """The radio must stop now (release requested, lease lost, shutdown)."""


class NoData(RadioError):
    """rtl_power runs but gives nothing: the dongle dropped off USB, or is wedged on it."""


class BiasTeeForced(RadioError):
    """The EEPROM switches the bias tee on at every open: DC would reach the antenna whatever the watch asks."""

    def __init__(self, receiver: str, index: int):
        super().__init__(
            f"{receiver}'s EEPROM forces its bias tee on at every open, which would put DC on the antenna; the watch "
            f"will not start. Clear the flag once (rtl_eeprom -d {index} -b 0), then unplug and replug {receiver}.")


# ------------------------------------------------------------ control file (release / resume)
def control_path(s: Settings) -> pathlib.Path:
    return s.path("control.json")


def read_control(s: Settings) -> dict[str, Any]:
    try:
        return json.loads(control_path(s).read_text())
    except (OSError, ValueError):
        return {}


def write_control(s: Settings, **fields: Any) -> dict[str, Any]:
    ctl = {**read_control(s), **fields}
    p = control_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ctl, indent=2))
    tmp.replace(p)
    return ctl


def released(ctl: dict[str, Any], now: dt.datetime) -> bool:
    if not ctl.get("released"):
        return False
    until = ctl.get("until")
    return not until or dt.datetime.fromisoformat(until.replace("Z", "+00:00")) > now


class Watch:
    def __init__(self, s: Settings, keeper: LeaseKeeper | None = None, device_index: DeviceIndex | None = None,
                 holders: Holders | None = None, transcriber: Transcriber | None = None,
                 bias_forced: BiasForced | None = None, bias_off: BiasOff | None = None,
                 usb_port: UsbPort | None = None, usb_reset: UsbReset | None = None,
                 alert: Callable[[str, dict[str, Any]], None] | None = None,
                 clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(UTC)):
        self.s = s
        self.clock = clock
        s.root.mkdir(parents=True, exist_ok=True)
        self.store = Store(s.path("piratewatch.sqlite3"))
        if self.store.migrated_local_stamps:
            LOG.info("hit log: detection times moved from local time to UTC")
        self.history = History(s.path("history"), s.history_days)
        if self.history.fixed_files:
            LOG.info("history: %d timestamp file(s) moved from local time to UTC", self.history.fixed_files)
        self.baseline = Baseline(s.learn_sweeps, s.threshold_db, s.duty_mask, static_ranges=s.static_ranges())
        self.baseline_path = s.path("baseline.npz")
        if self.baseline.load(self.baseline_path):
            LOG.info("baseline loaded: %d/%d sweeps learned", self.baseline.learned, s.learn_sweeps)
        if keeper is None:
            client = BrokerClient(s.broker_url, read_token(s.broker_token_file))
            keeper = LeaseKeeper(client, s.receiver, s.lease_owner, s.lease_port, s.lease_ttl_s, s.renew_every_s)
        self.keeper = keeper
        self.alert = alert or (lambda text, data: BrokerClient(s.broker_url, read_token(s.broker_token_file))
                               .alert(text, data))
        self.usb_port = usb_port or default_usb_port
        self.usb_reset = usb_reset or default_usb_reset
        self.device_index = device_index or default_device_index
        self.holders = holders or default_holders
        self.bias_forced = bias_forced or (lambda index: eeprom_bias_forced(index, s.tool_prefix))
        self.bias_off = bias_off or default_bias_off
        self.transcriber = transcriber or Transcriber(s, self.store, on_done=lambda _h: self._heatmap())
        self.stopping = threading.Event()
        self.state = "starting"
        self.detail = ""
        self.started_at = clock()
        self.current_hz: int | None = None          # the channel being recorded, if any
        self.last_sweep: dict[str, Any] = {}
        self._status_at = 0.0
        self._sweeps = 0
        self._no_data = 0                           # rtl_power no-data failures since the last good sweep
        self._usb_port: str | None = None           # where the receiver was last seen, for a reset after it vanishes

    # ------------------------------------------------------------ status
    def set_state(self, state: str, detail: str = "") -> None:
        if (state, detail) != (self.state, self.detail):
            LOG.info("state %s %s", state, detail)
        self.state, self.detail = state, detail
        self.write_status(force=True)

    def write_status(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._status_at < 2:
            return
        self._status_at = time.monotonic()
        doc = {"at": iso(self.clock()), "pid": os.getpid(), "started_at": iso(self.started_at), "state": self.state,
               "detail": self.detail, "recording_hz": self.current_hz if self.state == "recording" else None,
               "receiver": self.s.receiver, "lease_held": self.keeper.held,
               "lease_expires": iso(self.keeper.expires_at) if self.keeper.expires_at else None,
               "baseline": {"learned": self.baseline.learned, "of": self.s.learn_sweeps,
                            "excluded_ranges": len(self.baseline.excluded_ranges())},
               "sweeps_this_run": self._sweeps, "last_sweep": self.last_sweep, "hits": self.store.counts(),
               "no_data_in_a_row": self._no_data, "usb_resets_last_hour": len(self.recent_resets())}
        p = self.s.path("status.json")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2))
        tmp.replace(p)

    def _heatmap(self) -> None:
        try:
            heatmap.render_html(self.store, self.s.path("heatmap.html"))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("heatmap render failed: %s", exc)

    # ------------------------------------------------------------ checks
    def check(self) -> None:
        if self.stopping.is_set():
            raise Interrupted("shutdown")
        if self.keeper.lost.is_set():
            raise Interrupted(f"lease lost: {self.keeper.lost_reason}")
        ctl = read_control(self.s)
        if released(ctl, self.clock()):
            raise Interrupted("released for live use")
        if ctl.get("relearn"):
            write_control(self.s, relearn=False)
            self.baseline.relearn()
            LOG.info("baseline relearn requested")

    def wait(self, seconds: float) -> None:
        self.stopping.wait(seconds)

    # ------------------------------------------------------------ main loop
    def run(self) -> None:
        self.transcriber.start()
        self._heatmap()
        try:
            while not self.stopping.is_set():
                self.cycle()
        finally:
            self.shutdown()

    def cycle(self) -> None:
        """One pass of the outer loop: wait while released/busy, hold the lease, watch until interrupted."""
        now = self.clock()
        ctl = read_control(self.s)
        if released(ctl, now):
            if self.keeper.token:
                self.keeper.release()
            until = ctl.get("until")
            self.set_state("released", f"{self.s.receiver} free for live use" + (f" until {until}" if until else
                                                                                   "; run `resume` to restart"))
            self.wait(2)
            return
        if ctl.get("released"):          # timed release ran out
            write_control(self.s, released=False, until=None)
        if ctl.get("usb_halted"):
            self.set_state("blocked", f"{ctl['usb_halted']}; run `resume` once {self.s.receiver} is fixed")
            self.wait(5)
            return
        busy = self.holders(self.s.receiver)
        if busy:
            self.set_state("waiting", f"{self.s.receiver} is held by {', '.join(busy)} (outside the broker)")
            self.wait(10)
            return
        try:
            self.keeper.acquire()
        except LeaseError as exc:
            self.set_state("waiting", f"no lease: {exc}")
            self.wait(self.s.retry_lease_s)
            return
        self._usb_port = self.usb_port(self.s.receiver) or self._usb_port
        try:
            index = self.device_index(self.s.receiver)
            self.no_bias_tee(index)
            while True:
                cand, sweep = self.sweep(index)
                self.record(index, cand, sweep)
        except Interrupted as why:
            LOG.info("radio stopped: %s", why)
        except BiasTeeForced as exc:
            self.set_state("blocked", str(exc))
            LOG.error("%s", exc)
            self.keeper.release()
            self.wait(300)
            return
        except NoData as exc:
            self._no_data += 1
            LOG.error("radio error: %s (%d in a row)", exc, self._no_data)
            self.set_state("error", str(exc))
            recovered = False
            if self.s.usb_reset_after and self._no_data >= self.s.usb_reset_after:
                recovered = self.recover_usb(str(exc))      # still under the lease: nobody else opens the dongle
            self.keeper.release()
            self.wait(5 if recovered else 30)
            return
        except RadioError as exc:
            self.set_state("error", str(exc))
            LOG.error("radio error: %s", exc)
            self.keeper.release()
            self.wait(30)
            return
        finally:
            self.baseline.save(self.baseline_path)
        lost = self.keeper.lost.is_set()
        self.keeper.release()
        if lost:
            self.set_state("waiting", f"lease ended ({self.keeper.lost_reason}); retrying")
            self.wait(self.s.retry_lease_s)

    # ------------------------------------------------------------ USB recovery
    def reset_log_path(self) -> pathlib.Path:
        return self.s.path("usb-resets.jsonl")

    def recent_resets(self) -> list[dict[str, Any]]:
        """Reset attempts in the last hour (the log survives restarts, so a restart does not refill the budget)."""
        since = self.clock() - dt.timedelta(hours=1)
        out = []
        try:
            lines = self.reset_log_path().read_text().splitlines()[-200:]
        except OSError:
            return out
        for line in lines:
            try:
                e = json.loads(line)
                if dt.datetime.fromisoformat(e["at"].replace("Z", "+00:00")) > since:
                    out.append(e)
            except (ValueError, KeyError, TypeError):
                continue
        return out

    def log_reset(self, entry: dict[str, Any]) -> None:
        with self.reset_log_path().open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def recover_usb(self, why: str) -> bool:
        """Reset the receiver's USB port and wait for its serial to come back. True = resume watching; False = the
        watch is stopped (usb_halted) and Slack has been told."""
        rx, s = self.s.receiver, self.s
        recent = self.recent_resets()
        if len(recent) >= s.usb_resets_per_hour:
            self.halt_usb(f"{rx} still gives no data after {len(recent)} USB resets in the last hour ({why})")
            return False
        port = self.usb_port(rx) or self._usb_port
        if port is None:
            self.halt_usb(f"{rx} is not on USB and its port is unknown, so it cannot be reset ({why})")
            return False
        n = len(recent) + 1
        self.set_state("resetting", f"USB reset {n}/{s.usb_resets_per_hour} this hour: {rx} on port {port}")
        entry: dict[str, Any] = {"at": iso(self.clock()), "receiver": rx, "port": port, "reason": why,
                                 "no_data_in_a_row": self._no_data, "reset_this_hour": n}
        t0 = time.monotonic()
        try:
            entry["node"] = self.usb_reset(port)
        except RadioError as exc:
            entry.update(ok=False, error=str(exc))
            self.log_reset(entry)
            LOG.error("USB reset %d/%d this hour of %s on port %s failed: %s", n, s.usb_resets_per_hour, rx, port, exc)
            self.halt_usb(f"the USB reset of {rx} on port {port} failed: {exc}")
            return False
        back = self.wait_for_serial()
        entry.update(ok=back is not None, back_on_port=back, took_s=round(time.monotonic() - t0, 1))
        self.log_reset(entry)
        if back is None:
            LOG.error("USB reset %d/%d this hour: %s did not re-enumerate within %.0f s of resetting port %s",
                      n, s.usb_resets_per_hour, rx, s.usb_reenumerate_s, port)
            self.halt_usb(f"{rx} did not come back on USB within {s.usb_reenumerate_s:.0f} s of a reset of port {port}")
            return False
        LOG.warning("USB reset %d/%d this hour: %s (port %s, %s) after %d no-data failures; serial back on port %s "
                    "in %.1f s, resuming", n, s.usb_resets_per_hour, rx, port, entry["node"], self._no_data, back,
                    entry["took_s"])
        self._usb_port, self._no_data = back, 0
        self.set_state("waiting", f"{rx} reset on USB port {port}; resuming")
        return True

    def wait_for_serial(self) -> str | None:
        """The port the receiver's serial is on once it re-enumerates and librtlsdr can see it; None if it does not."""
        deadline = time.monotonic() + self.s.usb_reenumerate_s
        self.wait(min(2.0, self.s.usb_reenumerate_s))     # the old sysfs entry can outlive the reset briefly
        while not self.stopping.is_set():
            port = self.usb_port(self.s.receiver)
            if port:
                try:
                    self.device_index(self.s.receiver)
                    return port
                except RadioError:
                    pass
            if time.monotonic() >= deadline:
                return None
            self.wait(0.5)
        return None

    def halt_usb(self, why: str) -> None:
        LOG.error("watch stopped, USB recovery gave up: %s", why)
        write_control(self.s, usb_halted=why, usb_halted_at=iso(self.clock()))
        self.set_state("blocked", f"{why}; run `resume` once {self.s.receiver} is fixed")
        text = (f":rotating_light: Pirate watch stopped: {why}. {self.s.receiver} is off the air until someone checks "
                f"it (power-cycle or replug it), then resumes the watch (pirate_watch resume, the /piratewatch page, "
                f"or `python -m aobs.piratewatch resume`).")
        try:
            self.alert(text, {"source": "pirate-watch", "receiver": self.s.receiver, "usb_halted": why,
                              "resets_last_hour": self.recent_resets()})
        except (LeaseError, OSError) as exc:
            LOG.error("could not alert Slack through the broker: %s", exc)

    def no_bias_tee(self, index: int) -> None:
        """Before any radio work under a fresh lease: the bias tee must be off and must stay off. The rtl_* commands
        never pass -T (Stream refuses it), but an EEPROM "always on" flag would switch it on at every open anyway,
        and the GPIO keeps whatever the previous user of the dongle left."""
        if self.bias_forced(index):
            raise BiasTeeForced(self.s.receiver, index)
        self.bias_off(self.s.receiver)

    def shutdown(self) -> None:
        self.keeper.release()
        self.baseline.save(self.baseline_path)
        self.set_state("stopped")
        self.transcriber.stop()
        self.store.close()

    # ------------------------------------------------------------ sweep
    def sweep(self, index: int) -> tuple[Candidate, Sweep]:
        self.set_state("sweeping" if self.baseline.complete else "learning")
        stream = Stream(sweep_command(self.s, index), self.s.tool_prefix, lines=True)
        assembler = SweepAssembler(self.s.stop_hz)
        last_data = time.monotonic()
        try:
            while True:
                self.check()
                item = stream.get(0.5)
                if item is False:
                    if time.monotonic() - last_data > self.s.stall_s + 2 * self.s.integration_s:
                        raise NoData(f"rtl_power gave no data for {self.s.stall_s + 2 * self.s.integration_s:.0f} s "
                                         f"({self.s.receiver} dropped off USB?)")
                    continue
                last_data = time.monotonic()
                if item is None:
                    raise RadioError(f"rtl_power exited ({stream.proc.wait()}): {stream.stderr.strip()[-300:]}")
                sweep = assembler.feed(str(item))
                if sweep is None:
                    continue
                cand = self.on_sweep(sweep)
                if cand:
                    return cand, sweep
        finally:
            stream.stop()

    def on_sweep(self, sweep: Sweep) -> Candidate | None:
        self._sweeps += 1
        self._no_data = 0
        self.history.append(sweep)
        learning = not self.baseline.complete or not self.baseline.matches(sweep)
        if learning:
            self.baseline.add(sweep)
            if self.baseline.learned % 50 == 0 or self.baseline.complete:
                self.baseline.save(self.baseline_path)
            if self.baseline.complete:
                LOG.info("baseline complete: excluded %s", self.baseline.excluded_ranges())
                self.set_state("sweeping")
        cands = self.baseline.detect(sweep, self.s.min_width_hz, self.s.max_width_hz)
        floor = float(np.nanmedian(sweep.db))
        self.last_sweep = {"at": iso(sweep.at), "bins": int(len(sweep.db)), "bin_hz": round(sweep.bin_hz, 1),
                           "median_db": round(floor, 1), "candidates": len(cands)}
        self.write_status()
        return cands[0] if cands else None

    # ------------------------------------------------------------ record
    def record(self, index: int, cand: Candidate, sweep: Sweep) -> None:
        s = self.s
        channel = channel_of(cand.center_hz, s.channel_raster_hz)
        hit_id = self.store.add_hit(detected_at=iso(sweep.at), freq_hz=cand.center_hz, channel_hz=channel,
                                    width_hz=cand.width_hz, peak_db=cand.peak_db, snr_db=cand.snr_db, status="recording")
        self.current_hz = cand.center_hz
        self.set_state("recording", f"{cand.center_hz / 1e6:.4f} MHz, {cand.width_hz / 1e3:.0f} kHz, +{cand.snr_db} dB")
        clip = ClipState(NoiseSquelch(s.audio_bw_hz, s.squelch_hf_cut_hz, s.squelch_quiet_ratio, s.squelch_noise_ref),
                         s.silence_s, s.max_clip_s)
        stream = Stream(audio_command(s, index, cand.center_hz), s.tool_prefix, lines=False)
        blocks: list[np.ndarray] = []
        started: dt.datetime | None = None
        carry = b""
        interrupted: Interrupted | None = None
        stalled: str | None = None
        last_data = time.monotonic()
        try:
            while not clip.done:
                try:
                    self.check()
                except Interrupted as why:
                    interrupted = why
                    break
                self.write_status()
                item = stream.get(0.5)
                if item is False:
                    if time.monotonic() - last_data > self.s.stall_s:
                        # a dongle that drops off USB leaves rtl_fm hung, not exited: give up on it
                        stalled = f"rtl_fm gave no audio for {self.s.stall_s:.0f} s ({self.s.receiver} dropped off USB?)"
                        interrupted = Interrupted(stalled)
                        break
                    continue
                last_data = time.monotonic()
                if item is None:
                    if started is None:
                        self.store.update_hit(hit_id, status="error", note=f"rtl_fm: {stream.stderr.strip()[-300:]}")
                        raise RadioError(f"rtl_fm exited ({stream.proc.wait()}): {stream.stderr.strip()[-300:]}")
                    break
                if started is None:
                    started = self.clock()
                data = carry + bytes(item)  # type: ignore[arg-type]
                cut = len(data) // 2 * 2
                carry = data[cut:]
                blocks.extend(clip.feed(np.frombuffer(data[:cut], dtype="<i2")))
        finally:
            stream.stop()
        self.current_hz = None
        self.finish_clip(hit_id, cand, channel, clip, blocks, started or self.clock(), interrupted)
        if stalled:
            raise RadioError(stalled)
        if interrupted:
            raise interrupted

    def finish_clip(self, hit_id: int, cand: Candidate, channel: int, clip: ClipState, blocks: list[np.ndarray],
                    started: dt.datetime, interrupted: Interrupted | None) -> None:
        s = self.s
        if clip.first_present is None or clip.last_present is None:
            self.store.update_hit(hit_id, status="missed", clip_s=round(clip.elapsed, 1),
                                  note="no carrier after tuning" + (f" ({interrupted})" if interrupted else ""))
            self.store.add_activity(channel, started, 0.0)
            return
        keep = int(round((clip.last_present + 1.0) / 0.1))       # trim the silent tail to 1 s
        pcm = np.concatenate(blocks[:keep]) if blocks else np.zeros(0, np.int16)
        start = started + dt.timedelta(seconds=clip.first_present)
        end = started + dt.timedelta(seconds=clip.last_present)
        day = s.path("clips", f"{start:%Y-%m-%d}")
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"{start:%Y%m%dT%H%M%SZ}_{cand.center_hz / 1e3:.1f}kHz_{hit_id}.wav"
        write_wav(str(path), pcm, s.audio_bw_hz)
        note = []
        if interrupted:
            note.append(f"cut short: {interrupted}")
        if clip.hit_max:
            half = max(cand.width_hz, s.max_width_hz) / 2
            self.baseline.exclude(cand.center_hz - half, cand.center_hz + half)
            self.baseline.save(self.baseline_path)
            note.append(f"carrier never dropped in {s.max_clip_s:.0f} s: channel masked as a steady carrier")
            LOG.warning("masked %.4f MHz as a steady carrier", cand.center_hz / 1e6)
        self.store.update_hit(hit_id, start_utc=iso(start), end_utc=iso(end), duration_s=round(clip.active_s, 1),
                              clip_s=round(len(pcm) / s.audio_bw_hz, 1), audio_path=str(path), status="pending",
                              note="; ".join(note) or None)
        self.store.add_activity(channel, start, clip.active_s)
        LOG.info("hit %d: %.4f MHz %.1f s on air, peak %.1f dB (+%.1f), %s", hit_id, cand.center_hz / 1e6,
                 clip.active_s, cand.peak_db, cand.snr_db, path)
        self.transcriber.submit(hit_id)
        self._heatmap()


def main(s: Settings) -> int:
    from .control import ControlServer
    watch = Watch(s)
    token = read_token(s.broker_token_file)
    servers = [ControlServer(s, token)]
    if s.control_local and s.control_host not in ("127.0.0.1", "0.0.0.0"):
        servers.append(ControlServer(s, token, host="127.0.0.1"))      # Starlab's web page, through the broker
    for server in servers:
        server.start()

    def _term(signum: int, _frame: Any) -> None:
        LOG.info("signal %d: stopping", signum)
        watch.stopping.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    try:
        watch.run()
    finally:
        for server in servers:
            server.stop()
    return 0
