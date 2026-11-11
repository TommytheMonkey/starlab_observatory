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
from .store import Store

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


class Transcriber:
    """Background thread: transcribes queued hits; deletes the audio of clips Whisper calls non-speech."""

    def __init__(self, s: Settings, store: Store, on_done: Callable[[dict[str, Any]], None] | None = None,
                 worker: WhisperProcess | None = None):
        self.store = store
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

    def _loop(self) -> None:
        while (hit_id := self.q.get()) is not None:
            row = self.store.hit(hit_id)
            if not row or row["status"] != "pending" or not row["audio_path"]:
                continue
            try:
                res = self.worker.transcribe(hit_id, row["audio_path"])
            except Exception as exc:  # noqa: BLE001 - leave it pending; the next start retries it
                LOG.error("transcription of hit %s failed: %s", hit_id, exc)
                continue
            self.apply(row, res)

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
            pathlib.Path(row["audio_path"]).unlink(missing_ok=True)
            self.store.update_hit(hit_id, status="no_speech", audio_path=None, speech_prob=res.get("speech_prob"),
                                  language=res.get("language"), note="discarded: Whisper found no speech")
        if self.on_done:
            self.on_done(self.store.hit(hit_id) or {})
