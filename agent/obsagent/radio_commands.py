"""Deterministic Slack commands for the unified scheduler (no model in the loop).

See commands.py and /radio-help for the full list; this module keeps /radio-schedule and the wiring.

/radio-schedule [dry-run] <JSON>   validate + atomically book; replies SCHEDULED / NOT SCHEDULED
/radio-capabilities [receiver]     hardware, antennas, settings, availability (+ full JSON file for ChatGPT)
/radio-recordings [k=v ...]        receiver= satellite= date=YYYY-MM-DD job=<id>
/radio-jobs [receiver]             the unified schedule, next 48 h
/radio-cancel <job_id> [reason]    cancel a scheduled (not running) job

The same JSON pasted into a DM with the bot is handled the same way. Replies say
SCHEDULED only when the broker reports the job was stored.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import pathlib
import re
import threading
import zoneinfo
from typing import Any, Callable

from .broker_client import BrokerClient, BrokerError

LOG = logging.getLogger("obsagent.radio")
TZ = zoneinfo.ZoneInfo("America/New_York")
INLINE_JSON_LIMIT = 2800


# ---------------------------------------------------------------- parsing
def parse_request(text: str) -> tuple[dict[str, Any], bool]:
    """Slack text -> (request, dry_run). Undoes Slack escaping, smart quotes and code fences."""
    t = html.unescape(text or "").strip()
    dry = False
    m = re.match(r"^(/?radio-schedule)\s*", t)
    if m:
        t = t[m.end():]
    if re.match(r"^(dry-run|dryrun|--dry-run|validate)\b", t, re.I):
        dry, t = True, re.sub(r"^\S+\s*", "", t)
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip())
    t = t.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", " ": " "}))
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object found; send /radio-schedule {\"jobs\": [ ... ]}")
    try:
        req = json.loads(t[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}") from None
    if "jobs" not in req and "job_name" in req:
        req = {"jobs": [req]}
    if not isinstance(req.get("jobs"), list):
        raise ValueError('the JSON needs a "jobs" list')
    return req, dry


def looks_like_schedule(text: str) -> bool:
    t = html.unescape(text or "").lstrip().lstrip("`").lstrip()
    return t.startswith(("/radio-schedule", "radio-schedule")) or bool(re.match(r'^(json\s*)?\{\s*"jobs"\s*:', t))


def parse_filters(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in (text or "").split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[{"job": "job_id", "rx": "receiver", "sat": "satellite"}.get(k.lower(), k.lower())] = v
        elif re.match(r"^\d{4}-\d\d-\d\d$", tok):
            out["date"] = tok
        elif re.match(r"^[mal]\d{12}[0-9a-f]{6}$", tok):
            out["job_id"] = tok
        elif tok.upper().startswith("OBS-") or tok.upper() == "RSP1B":
            out["receiver"] = tok.upper()
        else:
            out["satellite"] = (out.get("satellite", "") + " " + tok).strip()
    return out


# ---------------------------------------------------------------- formatting
def _local(iso: str | None) -> str:
    if not iso:
        return "-"
    t = dt.datetime.fromisoformat(iso)
    return f"{t.astimezone(TZ):%a %b %d %-I:%M:%S %p %Z}"


def _mhz(hz: Any) -> str:
    return f"{hz / 1e6:.4f} MHz" if isinstance(hz, (int, float)) else "-"


def _size(n: Any) -> str:
    if not isinstance(n, (int, float)):
        return "unknown"
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _gain(radio: dict[str, Any]) -> str:
    if radio.get("gain_elements"):
        ge = radio["gain_elements"]
        mode = "IF AGC" if radio.get("agc") else f"IFGR {ge.get('IFGR')} dB"
        return f"{mode}, RFGR (LNA state) {ge.get('RFGR')}"
    if radio.get("gain_mode") == "auto":
        return "auto (tuner AGC)"
    return f"manual {radio.get('gain_db')} dB"


def format_scheduled(entry: dict[str, Any]) -> str:
    job = entry["job"]
    r, radio, rx, rec = job["resolved"], job["radio"], job["receiver"], job["recording"]
    sd = job.get("satdump") or {}
    paths = r.get("paths") or {}
    target = job.get("target") or {}
    offset = ""
    if isinstance(radio.get("target_frequency_hz"), int):
        offset = f" ({(radio['target_frequency_hz'] - radio['center_frequency_hz']) / 1e3:+.0f} kHz from center)"
    if sd.get("enabled") and sd.get("operation") == "decode_after_recording":
        shift = r.get("satdump_freq_shift_hz")
        sdl = (f"{sd['pipeline']}, decode after recording, baseband {sd['baseband_format']} @ "
               f"{sd['sample_rate_hz'] / 1e6:.3f} MS/s" + (f", freq_shift {shift:+d} Hz" if shift else ""))
    else:
        sdl = "off (raw IQ only)"
    conflict = "CLEAR"
    if entry.get("preempts"):
        conflict += " — preempted " + ", ".join(f"automatic {p['job_name']} ({p['job_id']})" for p in entry["preempts"])
    lines = [
        "*SCHEDULED*",
        f"Job: `{job['job_name']}`",
        f"Target: {r.get('target_name')} ({target.get('type')}{', ' + target['mode'] if target.get('mode') else ''})",
        f"Receiver: {r['receiver_id']} (serial `{r['device_serial']}`, {r['driver']})",
        f"Antenna / signal path: {rx.get('antenna')}"
        + (f" (ANTSW-01 port {r['switch_port']})" if r.get("switch_port") else "") + f" / {r.get('signal_path')}",
        f"Start: {_local(r['record_start'])}",
        f"End: {_local(r['record_end'])}",
        f"Reservation: {_local(r['reservation_start'])} → {_local(r['reservation_end'])}",
        f"Center: {_mhz(radio.get('center_frequency_hz'))}",
        f"Target: {_mhz(radio.get('target_frequency_hz'))}{offset}",
        f"Sample rate: {radio['sample_rate_hz'] / 1e6:.3f} MS/s",
        f"Bandwidth: {_mhz(radio['bandwidth_hz']) if radio.get('bandwidth_hz') else 'none set (rtl_sdr passband = sample rate)'}",
        f"Gain: {_gain(radio)}",
        f"Bias tee: {'ON' if radio.get('bias_tee') else 'off'}",
        f"Recording format: {rec['format']} via {rec['engine']}",
        f"Estimated size: {_size(r.get('estimated_bytes'))}",
        f"Raw output: `{paths.get('raw', '-')}`",
        f"SatDump: {sdl}",
        f"Decode output: `{paths['decode']}`" if paths.get("decode") else "Decode output: -",
        f"Conflict check: {conflict}",
        f"Job ID: `{entry['job_id']}`",
    ]
    if entry.get("warnings"):
        lines.append("Warnings: " + "; ".join(entry["warnings"]))
    if r.get("defaults_applied"):
        lines.append("_Defaults applied: " + ", ".join(r["defaults_applied"]) + "_")
    return "\n".join(lines)


def suggestion(errors: list[str]) -> str:
    text = " ".join(errors)
    if "conflict" in text:
        return ("Pick a time outside the conflicting reservation (shown above), or another receiver that is free then "
                "(`/radio-capabilities` lists each receiver's next 24 h).")
    if "storage" in text:
        return "Shorten the recording, lower the sample rate, or free space under /data."
    if "not connected" in text:
        return "Check the receiver's USB connection; `/radio-capabilities` shows what is connected."
    if "this same request" in text or "all_or_nothing" in text:
        return 'Fix the rejected job, or add "transaction_mode": "allow_partial" to book the valid ones.'
    return "Fix the fields listed above and send the request again (add `dry-run` first to check without booking)."


def format_rejected(entry: dict[str, Any]) -> str:
    job = entry.get("job") or {}
    r = job.get("resolved") or {}
    errs = entry.get("errors") or ["unknown error"]
    kind = ("receiver conflict" if any("conflict" in e for e in errs) else
            "storage" if any("storage" in e for e in errs) else "validation error")
    lines = ["*NOT SCHEDULED*", f"Reason: {kind}", f"Affected job: `{entry.get('job_name')}`",
             f"Receiver: {r.get('receiver_id') or (job.get('receiver') or {}).get('receiver_id') or '-'}",
             f"Requested interval: {_local(r.get('reservation_start'))} → {_local(r.get('reservation_end'))}"
             if r.get("reservation_start") else "Requested interval: -",
             "Conflict or validation error:"] + [f"• {e}" for e in errs] + [f"Suggested correction: {suggestion(errs)}"]
    return "\n".join(lines)


def format_result(res: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    """Slack text plus JSON attachments [(filename, content)] for the normalized jobs actually stored."""
    blocks, files = [], []
    if res.get("dry_run"):
        blocks.append("_Dry run: nothing was booked._")
    for entry in res["results"]:
        if entry.get("stored"):
            blocks.append(format_scheduled(entry))
            files.append((f"{entry['job_id']}.json", json.dumps(entry["job"], indent=2)))
        elif res.get("dry_run") and entry.get("ok"):
            blocks.append(f"*VALID* `{entry['job_name']}` — would book {entry['job']['resolved']['receiver_id']} "
                          f"{_local(entry['job']['resolved']['reservation_start'])} → "
                          f"{_local(entry['job']['resolved']['reservation_end'])}, "
                          f"{_size(entry['job']['resolved'].get('estimated_bytes'))}"
                          + (f"; would preempt {', '.join(p['job_id'] for p in entry['preempts'])}" if entry["preempts"] else ""))
        else:
            blocks.append(format_rejected(entry))
    if len(res["results"]) > 1:
        blocks.insert(0, f"{res['stored']} of {len(res['results'])} job(s) stored ({res['transaction_mode']}).")
    return "\n\n".join(blocks), files


def format_capabilities(caps: dict[str, Any], receiver: str | None = None) -> str:
    out = []
    for r in caps["receivers"]:
        if receiver and r["receiver_id"].upper() != receiver.upper():
            continue
        state = "available now" if r["available_now"] else ("NOT CONNECTED" if not r["connected"] else "busy now")
        ants = ", ".join(a["antenna"] + (f" (port {a['port']})" if "port" in a else "") for a in r["antennas"])
        rates = r.get("sample_rate_hz_ranges") or [r.get("sample_rate_hz_range")]
        out.append(
            f"*{r['receiver_id']}* · {r['hardware']} · serial `{r['serial']}` · {r['driver']} · {state}\n"
            f"  Antennas: {ants}\n  Path: {r['signal_path']}\n"
            f"  Freq {r['frequency_hz'][0] / 1e6:g}-{r['frequency_hz'][1] / 1e6:g} MHz · rates "
            + ", ".join(f"{lo / 1e6:g}-{hi / 1e6:g} MS/s" for lo, hi in rates)
            + f" · formats {', '.join(r['formats'])} · engine {r['engines'][0]}\n"
            f"  Mode: {r['mode']}" + (" (automatic passes can be preempted)" if r["preemptible_automatic_jobs"]
                                     else " (old recorder still runs; its passes are locked)")
            + (f"\n  Shared with: {', '.join(r['shared_with'])}" if r.get("shared_with") else "")
            + (f"\n  Note: {r['notes']}" if r.get("notes") else "")
            + "".join(f"\n  • {_local(j['reservation_start'])[:22]} {j['source']} {j['status']} {j.get('target') or j['job_name']}"
                      for j in r["reservations_next_24h"][:6]))
    return "\n\n".join(out) or f"No receiver called {receiver}."


def format_recordings(recs: list[dict[str, Any]]) -> str:
    if not recs:
        return "No recordings match."
    lines = []
    for r in recs[:15]:
        f = r["files"]
        raw = f.get("raw_iq") or {}
        sd = f.get("satdump") or {}
        bits = [f"*{r.get('target') or r['job_name']}* · {r['receiver']} · {_local(r['record_start'])[:22]} · "
                f"{r['status']} · `{r['job_id']}`"]
        if raw:
            bits.append(f"  raw: `{raw['path']}` ({_size(raw.get('bytes'))}{'' if raw.get('exists') else ', deleted'})")
        for k in ("metadata", "waterfall", "spectrum", "capture_log"):
            if f.get(k):
                bits.append(f"  {k}: `{f[k]['path']}`")
        if sd:
            bits.append(f"  SatDump: {len(sd.get('images', []))} image(s) in `{sd['output_directory']}`"
                        + ("" if sd.get("decoded") else " (no decode)"))
        for lf in (f.get("legacy_files") or [])[:4]:
            bits.append(f"  legacy: `{lf['raw']}` ({lf.get('status')}{'' if lf.get('raw_exists') else ', deleted'})")
        lines.append("\n".join(bits))
    if len(recs) > 15:
        lines.append(f"_{len(recs) - 15} more; narrow with receiver=, satellite=, date= or job=_")
    return "\n\n".join(lines)


def format_jobs(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "Nothing scheduled."
    return "\n".join(f"{_local(j['record_start'])[:22]}  {j['receiver_id']}  {j['source']}  *{j['status']}*  "
                     f"{j.get('target') or j['job_name']}  `{j['job_id']}`" for j in jobs[:40])


# ---------------------------------------------------------------- Slack wiring
class RadioCommands:
    """Registers every /radio-* command. Authorization uses Slack's authenticated user id from the
    request (Socket Mode: requests arrive over the app's authenticated websocket, so there is no
    HTTP signature to check). Ids inside command text are never trusted as identity."""

    def __init__(self, app: Any, broker: BrokerClient, owner: str, state_dir: pathlib.Path,
                 operators: set[str] | None = None, viewers: set[str] | None = None, channel: str | None = None):
        from .commands import Handlers, help_text
        self.app, self.broker, self.owner = app, broker, owner
        self.channel = channel                # home channel for replies/notifications; None = owner DM
        self.operators = (operators or set()) | {owner}
        self.viewers = (viewers or set()) | self.operators
        self.cursor_path = state_dir / "notification_cursor"
        self.threads_path = state_dir / "notification_threads.json"
        self._dm: dict[str, str] = {}
        h = Handlers(broker)
        self.handlers = {
            "/radio-schedule": self.schedule, "/radio-capabilities": self.capabilities, "/radio-recordings": h.recordings,
            "/radio-jobs": h.jobs, "/radio-cancel": h.cancel, "/radio-status": h.status, "/radio-health": h.health,
            "/radio-results": h.results, "/radio-decode": h.decode, "/radio-analyze": h.analyze,
            "/radio-reprocess": h.reprocess, "/radio-waterfall": h.waterfall,
            "/radio-help": lambda text: (help_text(text), [], []),
        }
        for name, fn in self.handlers.items():
            app.command(name)(self._wrap(name, fn))

    def allowed(self, user: str, name: str) -> bool:
        from .commands import READ, TIER
        tier = TIER.get(name, "disrupt")
        return user in (self.viewers if tier == READ else self.operators)

    def dm(self, user: str | None = None) -> str:
        if self.channel:
            return self.channel
        user = user or self.owner
        if user not in self._dm:
            self._dm[user] = self.app.client.conversations_open(users=user)["channel"]["id"]
        return self._dm[user]

    def _wrap(self, name: str, fn: Callable[[str], Any]):
        def handler(ack: Any, command: dict[str, Any]) -> None:
            user = command.get("user_id", "")
            if not self.allowed(user, name):
                ack(f"You are not authorized to use `{name}` on this observatory.")
                LOG.warning("refused %s for %s", name, user)
                return
            ack(f"`{name}` received; replying in <#{self.channel}>." if self.channel else f"`{name}` received; replying in our DM.")
            threading.Thread(target=self._run, args=(name, fn, command.get("text", ""), user), daemon=True).start()
        return handler

    def _run(self, name: str, fn: Callable[..., Any], text: str, user: str | None = None) -> None:
        try:
            reply, files, binaries = fn(text)
        except (ValueError, BrokerError) as exc:
            msg = str(exc)
            head = "NOT SCHEDULED" if name == "/radio-schedule" else "Error"
            reply, files, binaries = f"*{head}*\nReason: {msg}\n(`/radio-help {name.replace('/radio-', '')}` for usage)", [], []
        except Exception as exc:  # noqa: BLE001 - always answer
            LOG.exception("%s failed", name)
            reply, files, binaries = f":warning: `{name}` failed: `{exc}`", [], []
        self.post(reply, files, binaries, user)

    def post(self, text: str, files: Any = (), binaries: Any = (), user: str | None = None,
             thread_ts: str | None = None) -> str | None:
        client, channel = self.app.client, self.dm(user)
        first_ts = None
        for chunk in _chunks(text):
            r = client.chat_postMessage(channel=channel, text=chunk, thread_ts=thread_ts)
            first_ts = first_ts or r.get("ts")
        for fname, content in files:
            if len(content) <= INLINE_JSON_LIMIT:
                client.chat_postMessage(channel=channel, text=f"`{fname}`\n```{content}```", thread_ts=thread_ts)
            else:
                client.files_upload_v2(channel=channel, content=content, filename=fname, title=fname, thread_ts=thread_ts)
        for fname, data, title in binaries:
            client.files_upload_v2(channel=channel, file=data, filename=fname, title=title, thread_ts=thread_ts)
        return first_ts

    # ---- commands: each returns (text, json files, binary files)
    def schedule(self, text: str) -> tuple[str, list[tuple[str, str]], list[tuple[str, bytes, str]]]:
        req, dry = parse_request(text)
        res = self.broker.schedule(req["jobs"], req.get("transaction_mode", "all_or_nothing"), dry, f"slack:{self.owner}")
        reply, files = format_result(res)
        return reply, files, []

    def capabilities(self, text: str) -> tuple[str, list[tuple[str, str]], list[tuple[str, bytes, str]]]:
        caps = self.broker.capabilities()
        return (format_capabilities(caps, text.strip() or None),
                [("observatory-capabilities.json", json.dumps(caps, indent=2))], [])

    # ---- outbox: broker notifications -> owner DM, one thread per recording/job
    def start_notifier(self, interval_s: float = 20) -> None:
        def loop() -> None:
            cursor = self._load_cursor()
            threads = self._load_threads()
            while True:
                try:
                    if cursor < 0:          # first run: do not replay history
                        notes = self.broker.notifications(0)
                        while notes:
                            cursor = notes[-1]["id"]
                            notes = self.broker.notifications(cursor)
                    notes = self.broker.notifications(cursor)
                    for n in notes:
                        icon = {"preempted": ":warning:", "skipped": ":fast_forward:", "failed": ":x:", "partial": ":scissors:",
                                "missed": ":x:", "completed": ":white_check_mark:", "attempt_completed": ":white_check_mark:",
                                "attempt_failed": ":x:", "scheduled": ":calendar:", "recording": ":red_circle:",
                                "health": ":rotating_light:", "rf_layout": ":electric_plug:"}.get(n["kind"], ":satellite_antenna:")
                        key = n.get("job_id")
                        parent = threads.get(key) if key else None
                        if n["kind"] == "health":       # receiver warnings go to the channel, never only a thread
                            self.post(n["text"] if n["text"].startswith(":") else f"{icon} {n['text']}")
                            cursor = n["id"]
                            continue
                        ts = self.post(f"{icon} {n['text']}", thread_ts=parent)
                        if key and not parent and ts:
                            threads[key] = ts
                        cursor = n["id"]
                    if notes:
                        self._save_cursor(cursor)
                        self._save_threads(threads)
                except Exception:  # noqa: BLE001 - keep polling
                    LOG.exception("notification poll failed")
                threading.Event().wait(interval_s)
        threading.Thread(target=loop, name="notifier", daemon=True).start()

    def _load_cursor(self) -> int:
        try:
            return int(self.cursor_path.read_text().strip())
        except (OSError, ValueError):
            return -1

    def _save_cursor(self, cursor: int) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(str(cursor))

    def _load_threads(self) -> dict[str, str]:
        try:
            return json.loads(self.threads_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_threads(self, threads: dict[str, str]) -> None:
        keep = dict(list(threads.items())[-500:])
        self.threads_path.write_text(json.dumps(keep))


def _chunks(text: str, limit: int = 3500) -> list[str]:
    out, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    return out + ([cur] if cur else [])
