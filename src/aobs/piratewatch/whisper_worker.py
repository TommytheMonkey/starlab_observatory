"""Whisper transcription worker for the satcom pirate watch.

Runs in its own venv (faster-whisper + CUDA libraries), separate from Starlab's, and talks
JSON lines: one request {"id", "path"} per line on stdin, one result per line on stdout.
It loads the model once. Standalone on purpose: it imports nothing from aobs.

Per clip: transcribe with language auto-detection and Silero VAD; if the detected
language is not English, a second pass with task="translate" gives the English text.
A clip is non-speech when VAD keeps nothing, the text is empty, or every segment has
no_speech_prob above the threshold.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback


def _cuda_lib_path() -> None:
    """ctranslate2 needs the pip nvidia-* libraries on the loader path; set it and re-exec once."""
    try:
        import nvidia  # type: ignore[import-not-found]
    except ImportError:
        return
    roots = [os.path.join(p, sub, "lib") for p in nvidia.__path__ for sub in ("cublas", "cudnn", "cuda_nvrtc")]
    roots = [r for r in roots if os.path.isdir(r)]
    current = os.environ.get("LD_LIBRARY_PATH", "")
    if roots and not all(r in current.split(":") for r in roots):
        os.environ["LD_LIBRARY_PATH"] = ":".join(roots + ([current] if current else []))
        os.execv(sys.executable, [sys.executable, *sys.argv])


def load_audio(path: str):  # -> numpy float32 mono 16 kHz
    """Decoded with ffmpeg rather than faster-whisper's PyAV path (faster-whisper 1.2.1 breaks on PyAV 19)."""
    import subprocess

    import numpy as np
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-f", "f32le", "-ac", "1",
                          "-ar", "16000", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def classify(segments: list[dict], no_speech_threshold: float) -> tuple[bool, float]:
    """(is_speech, speech_prob): duration-weighted 1 - no_speech_prob over the kept segments."""
    texted = [s for s in segments if s["text"].strip()]
    if not texted:
        return False, 0.0
    total = sum(max(s["end"] - s["start"], 0.01) for s in texted)
    prob = sum((1 - s["no_speech_prob"]) * max(s["end"] - s["start"], 0.01) for s in texted) / total
    speech = any(s["no_speech_prob"] < no_speech_threshold for s in texted)
    return speech, round(prob, 3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--no-speech-threshold", type=float, default=0.6)
    args = ap.parse_args()
    if args.device == "cuda":
        _cuda_lib_path()
    from faster_whisper import WhisperModel  # imported late: the re-exec above must come first

    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    print(json.dumps({"ready": True, "model": args.model, "device": args.device}), flush=True)

    def run(path: str, task: str, language: str | None) -> tuple[list[dict], object]:
        segs, info = model.transcribe(load_audio(path), task=task, language=language, vad_filter=True,
                                      beam_size=5, condition_on_previous_text=False)
        return [{"start": s.start, "end": s.end, "text": s.text, "no_speech_prob": s.no_speech_prob,
                 "avg_logprob": s.avg_logprob} for s in segs], info

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        try:
            segments, info = run(req["path"], "transcribe", None)
            speech, prob = classify(segments, args.no_speech_threshold)
            out = {"id": req["id"], "ok": True, "speech": speech, "speech_prob": prob,
                   "language": info.language, "language_prob": round(info.language_probability, 3),
                   "transcript": " ".join(s["text"].strip() for s in segments).strip(), "translation": None,
                   "segments": segments}
            if speech and info.language != "en":
                tsegs, _ = run(req["path"], "translate", info.language)
                out["translation"] = " ".join(s["text"].strip() for s in tsegs).strip()
            elif speech:
                out["translation"] = out["transcript"]
        except Exception as exc:  # noqa: BLE001 - one bad clip must not stop the worker
            out = {"id": req.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}",
                   "trace": traceback.format_exc()[-1500:]}
        print(json.dumps(out), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
