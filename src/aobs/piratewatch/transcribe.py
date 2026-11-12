"""Drives whisper_worker.py (in its own venv) and files each result against its hit."""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import queue
import subprocess
import threading
from typing import Any, Callable

from .config import Settings
from .store import Store, iso

LOG = logging.getLogger("aobs.piratewatch.transcribe")
WORKER = pathlib.Path(__file__).with_name("whisper_worker.py")


class WhisperProcess:
    def __init__(self, s: Settings, command: list[str] | None = None):
        self.command = command or [str(pathlib.Path(s.whisper_python).expanduser()), "-I", str(WORKER),
                                   "--model", s.whisper_model, "--device", s.whisper_device,
                                   "--compute-type", s.whisper_compute_type,
                                   "--no-speech-threshold", str(s.no_speech_threshold)]
        self.proc: subprocess.Popen[str] | None = None

    def _start(self) -> None:
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        assert self.proc.stdout is not None
        while True:   # skip anything printed before the ready line (model download progress goes to stderr)
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"whisper worker exited during start-up (exit {self.proc.wait()})")
            if line.strip().startswith("{") and json.loads(line).get("ready"):
                LOG.info("whisper worker ready: %s", line.strip())
                return

    def transcribe(self, hit_id: int, path: str) -> dict[str, Any]:
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        assert self.proc is not None and self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps({"id": hit_id, "path": path}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            self.proc = None
            raise RuntimeError("whisper worker died mid-clip")
        return json.loads(line)

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            assert self.proc.stdin is not None
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


PRUNE_EVERY_S = 300.0


class Transcriber:
    """Background thread: transcribes queued hits. The audio of clips Whisper calls non-speech is kept for
    `no_speech_keep_h` (so its calls can be checked by ear), then deleted."""

    def __init__(self, s: Settings, store: Store, on_done: Callable[[dict[str, Any]], None] | None = None,
                 worker: WhisperProcess | None = None,
                 clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)):
        self.store = store
        self.keep = dt.timedelta(hours=s.no_speech_keep_h)
        self.clock = clock
        self.worker = worker or WhisperProcess(s)
        self.on_done = on_done
        self.q: queue.Queue[int | None] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="pirate-whisper", daemon=True)

    def start(self) -> None:
        for row in self.store.pending():       # clips left pending by a previous run
            self.q.put(row["id"])
        self._thread.start()

    def submit(self, hit_id: int) -> None:
        self.q.put(hit_id)

    def stop(self) -> None:
        self.q.put(None)
        self._thread.join(timeout=60)
        self.worker.close()

    def prune(self) -> int:
        """Delete kept non-speech audio past its time. Returns how many clips were removed."""
        cutoff = iso(self.clock() - self.keep)
        n = 0
        for row in self.store.expired_no_speech(cutoff or ""):
            pathlib.Path(row["audio_path"]).unlink(missing_ok=True)
            self.store.update_hit(row["id"], audio_path=None,
                                  note=f"Whisper found no speech; audio deleted after {self.keep.total_seconds() / 3600:g} h")
            n += 1
        return n

    def _loop(self) -> None:
        self._safe_prune()
        while True:
            try:
                hit_id = self.q.get(timeout=PRUNE_EVERY_S)
            except queue.Empty:
                self._safe_prune()
                continue
            if hit_id is None:
                return
            row = self.store.hit(hit_id)
            if not row or row["status"] != "pending" or not row["audio_path"]:
                continue
            try:
                res = self.worker.transcribe(hit_id, row["audio_path"])
            except Exception as exc:  # noqa: BLE001 - leave it pending; the next start retries it
                LOG.error("transcription of hit %s failed: %s", hit_id, exc)
                continue
            self.apply(row, res)
            self._safe_prune()

    def _safe_prune(self) -> None:
        try:
            if n := self.prune():
                LOG.info("deleted the audio of %d non-speech clip(s) kept past %s", n, self.keep)
        except Exception as exc:  # noqa: BLE001 - pruning must never stop transcription
            LOG.warning("pruning non-speech audio failed: %s", exc)

    def apply(self, row: dict[str, Any], res: dict[str, Any]) -> None:
        hit_id = row["id"]
        if not res.get("ok"):
            self.store.update_hit(hit_id, status="error", note=res.get("error"))
        elif res.get("speech"):
            self.store.update_hit(hit_id, status="speech", language=res.get("language"),
                                  language_prob=res.get("language_prob"), transcript=res.get("transcript"),
                                  translation=res.get("translation"), speech_prob=res.get("speech_prob"))
            when = dt.datetime.fromisoformat((row["start_utc"] or row["detected_at"]).replace("Z", "+00:00"))
            self.store.add_speech(row["channel_hz"], when)
        else:
            keep_h = self.keep.total_seconds() / 3600
            if keep_h <= 0:
                pathlib.Path(row["audio_path"]).unlink(missing_ok=True)
            self.store.update_hit(hit_id, status="no_speech", speech_prob=res.get("speech_prob"),
                                  language=res.get("language"), language_prob=res.get("language_prob"), transcript=res.get("transcript") or None,
                                  **({"audio_path": None} if keep_h <= 0 else {}),
                                  note="Whisper found no speech" + (f"; audio kept {keep_h:g} h" if keep_h > 0 else ""))
        if self.on_done:
            self.on_done(self.store.hit(hit_id) or {})
