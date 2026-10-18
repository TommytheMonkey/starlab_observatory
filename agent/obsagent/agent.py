"""Slack front end for the observatory: DM @observatory (or @-mention it in a channel) and it
drives the receivers through the broker with Claude as the brain.

Run: python -m obsagent.agent   (reads ~/.config/observatory/{slack,broker}.env)
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import threading
import zoneinfo
from collections import defaultdict
from typing import Any

import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .broker_client import BrokerClient, ReceiverSession
from .radio_commands import RadioCommands, looks_like_schedule
from .satnogs import CONFIRM_RE, SatnogsGate
from .tools import TOOLS, ToolContext, run_tool

LOG = logging.getLogger("obsagent")
MODEL = "claude-opus-5-5"
TZ = zoneinfo.ZoneInfo("America/New_York")
MAX_STEPS = 25
STATE_DIR = pathlib.Path(os.path.expanduser("~/observatory-agent/state/conversations"))
PENDING_DIR = STATE_DIR.parent / "pending"
BETAS = ["server-side-fallback-2026-07-01", "compact-2026-01-12"]

SYSTEM = """You are the radio operator for a home radio observatory, talking with its owner over Slack.

Hardware you control (through tools):
- OBS-R05, an RTL-SDR (NESDR SMArt v5, 24 MHz-1.766 GHz, HF below 28.8 MHz via direct sampling), fed by an 8-port antenna switch (ANTSW-01).
- OBS-R05 is also the ADS-B aircraft-tracking receiver. While you use it, ADS-B is paused; it resumes automatically when you release the receiver (or after 2 idle minutes). So work efficiently, and release the receiver when a request is clearly finished.
- Antenna ports: 1 V-DIPOLE (tunable, best ~137 MHz weather sats, usable 70 MHz-1 GHz); 2 SPRING-SHORT (UHF 400-600 MHz); 3 SPRING-TALL (VHF/UHF 144-450 MHz); 6 ADS Airband (aviation); 7 SPRING-XTRA-TALL (general VHF/UHF); 8 MA-30-LOOP (HF/AM 0.5-30 MHz, needs direct_sampling). Ports 4 and 5 are empty.
- Signal paths (antenna > filters/amps > receiver) live in the owner's drag-and-drop editor; rf_editor_link gives a sign-in link. Every recording stores the path it was made with (recording_results shows it). The antenna-switch port list in observatory_status follows the editor and overrides the port list above.
- The SatNOGS ground station comet (station 5254, its own RTL-SDR, run by SatNOGS Network): satnogs_status and satnogs_logs inspect it; satnogs_restart_client only asks the owner to confirm a restart with a code the bot posts. You cannot restart it yourself or see the code; never say it was restarted unless the owner reports it. You cannot schedule its observations or use its radio.
- Separately, METEOR weather-satellite passes are recorded automatically on other receivers; satellite_passes shows the schedule.

Scheduled recordings (any receiver, including OBS-R01, OBS-R02, OBS-R05 and the SDRplay RSP1B):
- The broker keeps one schedule for every receiver. radio_capabilities lists the real hardware limits; use only settings it allows (RTL-SDR: gain_db steps, no bandwidth; RSP1B: gain_elements IFGR/RFGR, bandwidth from its list, cs16/cf32).
- To book a recording, write the /radio-schedule job JSON, call schedule_recording with dry_run true, show the owner the key settings and any warnings, and book (dry_run false) only after the owner clearly says yes. Never guess a critical setting the owner did not give or that radio_capabilities does not settle; ask.
- center frequency is where the SDR tunes; target frequency is the signal inside the passband. Keep them distinct. Never enable a bias tee unless the owner asks for it explicitly.
- Manual jobs take priority over automatic passes on receivers the broker runs; the result lists anything preempted, so tell the owner. Running recordings are never interrupted.
- Say a recording is scheduled only when schedule_recording returns stored: true for it, and give its job ID.
- The owner can also use the /radio-* slash commands directly (/radio-help lists them).

Recordings, processing and status:
- Every recording has a recording_id (the job that made it). Find them with list_recordings; look at one with recording_results.
- Decoding, analysis and comparison run as asynchronous low-priority attempts via start_processing; each attempt is kept, so re-running with other settings never loses earlier results. Say the attempt id and that the owner will be notified when it finishes.
- A decode is successful only if its attempt says decoded: true (CADU frames and non-blank images). SatDump's exit code means nothing here (it exits 1 even after good decodes). To explain a failure, read the attempt's satdump log or result JSON with read_artifact (try contains "Lines", "Deframer", "SNR", "error") and its search results (CADU frames per centre frequency).
- If a METEOR decode found no frames, suggest checking the signal with /radio-analyze (waterfall around the target) and decoding a time window where the signal is visible; a few kHz of frequency error is normal and the decoder already searches ±6 kHz.
- Use observatory_overview for "what is recording / which receiver is free", observatory_health for "is everything OK".
- Never invent recording ids, job ids, passes, availability, file paths, decode results, measurements or calendar events. Every such fact must come from a tool result in this conversation.
- If a request matches several recordings or jobs (e.g. three captures yesterday), list them briefly and ask which one.
- Before anything disruptive or that books hardware (scheduling, cloning with dry_run false), show what will happen and wait for an explicit yes. You cannot cancel running recordings yourself; point the owner to /radio-cancel.
- Signal levels from analysis are relative (dBFS or dB over floor), not calibrated power; a signal near a satellite's frequency is not proof it is that satellite.

How to work:
- Pick the antenna that suits the band. Sweep first to find activity, then listen to the strongest or most interesting signals.
- Sweep power values are relative dB from an auto-gain receiver, not calibrated dBm; judge signals by SNR above the noise floor. Signals sitting exactly on round MHz values (144.000, 145.000) at constant strength are usually the receiver's own spurs.
- You cannot hear audio. listen gives activity statistics and posts the clip to Slack for the owner. Treat an active_fraction near 0 as silence/noise and say so plainly.

Getting a clean signal (you control gain, bandwidth, squelch):
- Gain: tuner AGC (null) tends to run hot here and raise the noise floor. For anything weak or noisy, run optimize_gain first and use its best_gain_db. Too little gain buries the signal; too much overloads the dongle, which shows as a rising noise floor, spurs and ghost signals all over the band. Near strong FM broadcast or pager transmitters, lower gain (15-30 dB) usually helps. Typical good values: 25-40 dB on VHF/UHF, 40-49.6 dB on weak satellite or distant signals.
- Bandwidth: match the channel. NFM land mobile/GMRS/ham 12.5 kHz channels: 12-16 kHz. Older 25 kHz-spaced FM, or a drifting signal: 20-32 kHz. Aviation AM: 8-12 kHz. HF SSB voice: 4-6 kHz. Too wide adds noise; too narrow clips the audio or loses an off-centre signal.
- Centring: sweep bins are coarse. Before a long listen to a narrow signal, sweep about ±30 kHz around it with a 1-2 kHz bin and tune to the peak.
- Squelch: for intermittent traffic (repeaters, aviation, public service) listen longer (60-120 s) with squelch 100-300, so the clip holds only the transmissions. If nothing breaks squelch, it posts nothing; lower it or drop it to 0 to hear the raw channel.
- Modes: nfm for 2 m/70 cm/GMRS/FRS/public service/weather radio (162.4-162.55 MHz); am for 118-137 MHz aviation and AM broadcast; usb above 10 MHz and lsb below 10 MHz for ham HF voice; wfm for FM broadcast.
- When a first try sounds empty (active_fraction near 0), adjust before giving up: better antenna, optimize_gain, recentre, then a longer squelched listen. Tell the owner the settings you used.
- Never record or relay the content of cellular or cordless-phone traffic; the listen tool enforces this.
- If a tool fails, say what failed in one line and try a sensible alternative if there is one.

Replying: you are writing Slack messages. Use Slack mrkdwn (*bold*, _italic_, `code`, bullet lists with •); no # headers or tables. Lead with the answer. Give frequencies in MHz with units and times in the owner's local time (America/New_York). Keep it short unless asked for detail."""


def _env(path: pathlib.Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


class Agent:
    def __init__(self, cfg_dir: pathlib.Path):
        slack, broker = _env(cfg_dir / "slack.env"), _env(cfg_dir / "broker.env")
        self.owner = slack["SLACK_OWNER_ID"]
        self.app_token = slack["SLACK_APP_TOKEN"]
        self.app = App(token=slack["SLACK_BOT_TOKEN"])
        self.claude = anthropic.Anthropic(api_key=slack["ANTHROPIC_API_KEY"])
        self.broker = BrokerClient(broker["BROKER_URL"], broker["BROKER_TOKEN"])
        self.session = ReceiverSession(self.broker)
        sat_env = cfg_dir / "satnogs.env"
        self.satnogs = SatnogsGate.from_env(_env(sat_env)) if sat_env.exists() else None
        self.histories: dict[str, list[dict[str, Any]]] = {}
        self.locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        split = lambda v: {x.strip() for x in (v or "").split(",") if x.strip()}  # noqa: E731
        # SLACK_CHANNEL_ID: the bot's home channel; replies and notifications go there instead of DMs
        self.channel = slack.get("SLACK_CHANNEL_ID") or None
        self.radio = RadioCommands(self.app, self.broker, self.owner, STATE_DIR.parent,
                                   operators=split(slack.get("SLACK_OPERATORS")), viewers=split(slack.get("SLACK_VIEWERS")),
                                   channel=self.channel)
        self._seen: list[str] = []            # Slack may redeliver an event; handle each once
        self.app.event("app_mention")(self._on_mention)
        self.app.event("message")(self._on_message)

    # ---------------------------------------------------------------- Slack
    def _on_mention(self, event: dict[str, Any], client: Any) -> None:
        if event.get("channel") == self.channel:
            self._on_message({**event, "channel_type": "home"}, client)   # same as a plain message there
            return
        thread = event.get("thread_ts") or event["ts"]
        self._handle(event, client, key=f"{event['channel']}:{thread}", thread_ts=thread)

    def _on_message(self, event: dict[str, Any], client: Any) -> None:
        if event.get("subtype") or event.get("bot_id"):
            return  # ignore edits, joins, our own posts
        home = self.channel is not None and event.get("channel") == self.channel
        if event.get("channel_type") != "im" and not home:
            return  # other channels reach the bot only as app_mention
        thread = event.get("thread_ts")
        if event.get("user") == self.owner and looks_like_schedule(event.get("text", "")) and not self._duplicate(event):
            # Pasted /radio-schedule JSON is booked deterministically, without the model.
            threading.Thread(target=self.radio._run, args=("/radio-schedule", self.radio.schedule, event["text"]),
                             daemon=True).start()
            return
        # One running conversation for the top level of the DM/home channel; each thread is its own conversation.
        key = f"{event['channel']}:{thread}" if (home and thread) else f"{event['channel']}:dm"
        self._handle(event, client, key=key, thread_ts=thread)

    def _duplicate(self, event: dict[str, Any]) -> bool:
        key = event.get("client_msg_id") or f"{event.get('channel')}:{event.get('ts')}"
        if key in self._seen:
            return True
        self._seen = (self._seen + [key])[-500:]
        return False

    def _handle(self, event: dict[str, Any], client: Any, key: str, thread_ts: str | None) -> None:
        if self._duplicate(event):
            LOG.info("ignoring redelivered event %s", event.get("ts"))
            return
        if event.get("user") != self.owner:
            LOG.info("ignoring message from non-owner %s", event.get("user"))
            return
        text = event.get("text", "").replace(f"<@{self._bot_id(client)}>", "").strip()
        if not text:
            return
        channel, ts = event["channel"], event["ts"]
        if (m := CONFIRM_RE.match(text)) and self.satnogs is not None:
            # The owner's own typed code is the only path to a restart; the model is not involved.
            self._react(client, channel, ts, "add")
            try:
                reply = self.satnogs.confirm_restart(m[1])
            except Exception as exc:  # noqa: BLE001
                LOG.exception("satnogs restart failed")
                reply = f":warning: The SatNOGS restart failed: `{exc}`"
            finally:
                self._react(client, channel, ts, "remove")
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=reply)
            return
        self._react(client, channel, ts, "add")
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        pending = PENDING_DIR / f"{ts}.json"
        pending.write_text(json.dumps({"channel": channel, "thread_ts": thread_ts, "ts": ts, "text": text[:200]}))
        try:
            with self.locks[key]:
                reply, ctx = self._converse(key, text)
            for att in ctx.attachments:
                client.files_upload_v2(channel=channel, thread_ts=thread_ts, file=att.data,
                                       filename=att.filename, title=att.title)
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=reply or "(no reply)")
            for notice in ctx.notices:
                client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=notice)
        except Exception as exc:  # noqa: BLE001 - always answer the owner
            LOG.exception("request failed")
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f":warning: Something broke: `{exc}`")
        finally:
            pending.unlink(missing_ok=True)
            self._react(client, channel, ts, "remove")

    _bot_user: str | None = None

    def _bot_id(self, client: Any) -> str:
        if self._bot_user is None:
            self._bot_user = client.auth_test()["user_id"]
        return self._bot_user

    @staticmethod
    def _react(client: Any, channel: str, ts: str, verb: str) -> None:
        try:
            getattr(client, f"reactions_{verb}")(channel=channel, timestamp=ts, name="satellite_antenna")
        except Exception:  # noqa: BLE001 - a missing reaction is cosmetic
            pass

    # ---------------------------------------------------------------- memory
    @staticmethod
    def _path(key: str) -> pathlib.Path:
        return STATE_DIR / (key.replace(":", "_").replace("/", "_") + ".json")

    def _history(self, key: str) -> list[dict[str, Any]]:
        """Conversation history survives restarts: it is saved to disk after every turn."""
        if key not in self.histories:
            try:
                self.histories[key] = json.loads(self._path(key).read_text())
            except (OSError, ValueError):
                self.histories[key] = []
        return self.histories[key]

    def _save(self, key: str) -> None:
        def plain(content: Any) -> Any:
            if isinstance(content, list):
                return [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b for b in content]
            return content
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = [{**m, "content": plain(m["content"])} for m in self.histories[key]]
        tmp = self._path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self._path(key))

    # ---------------------------------------------------------------- Claude
    def _converse(self, key: str, text: str) -> tuple[str, ToolContext]:
        """One owner message -> tool loop -> reply text. History is append-only per conversation."""
        try:
            return self._turn(key, text)
        finally:
            self._save(key)

    def _turn(self, key: str, text: str) -> tuple[str, ToolContext]:
        messages = self._history(key)
        now = dt.datetime.now(TZ).strftime("%a %Y-%m-%d %-I:%M %p %Z")
        messages.append({"role": "user", "content": f"[{now}] {text}"})
        ctx = ToolContext(self.broker, self.session, satnogs=self.satnogs)
        response = None
        for _ in range(MAX_STEPS):
            response = self.claude.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=[{"type": "text", "text": SYSTEM}],
                tools=TOOLS,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                cache_control={"type": "ephemeral"},
                betas=BETAS,
                fallbacks="default",
                # Long conversations get summarized server-side instead of growing without bound.
                context_management={"edits": [{"type": "compact_20260112"}]},
            )
            if response.stop_reason == "refusal":
                messages.pop()  # leave history valid for the next message
                return "I can't help with that one.", ctx
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                break
            results = []
            for block in tool_uses:
                LOG.info("tool %s %s", block.name, block.input)
                try:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": run_tool(ctx, block.name, block.input)})
                except Exception as exc:  # noqa: BLE001 - report the failure to Claude
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"Error: {exc}", "is_error": True})
            messages.append({"role": "user", "content": results})
        else:
            LOG.warning("hit MAX_STEPS in %s", key)
        usage = response.usage if response else None
        if usage:
            LOG.info("usage in=%s cache_read=%s out=%s", usage.input_tokens, usage.cache_read_input_tokens,
                     usage.output_tokens)
        reply = "\n".join(b.text for b in response.content if b.type == "text") if response else ""
        return reply.strip(), ctx

    def _report_interrupted(self) -> None:
        """Requests that were mid-flight when the previous process stopped get an honest note, not silence."""
        for path in sorted(PENDING_DIR.glob("*.json")) if PENDING_DIR.exists() else []:
            try:
                p = json.loads(path.read_text())
                self.app.client.chat_postMessage(
                    channel=p["channel"], thread_ts=p.get("thread_ts"),
                    text=f":arrows_counterclockwise: I was restarted while working on \"{p['text']}\" and lost that "
                         "request partway through. Please send it again.")
                self._react(self.app.client, p["channel"], p["ts"], "remove")
            except Exception:  # noqa: BLE001
                LOG.exception("could not report interrupted request %s", path.name)
            path.unlink(missing_ok=True)

    def run(self) -> None:
        self._report_interrupted()
        self.radio.start_notifier()
        LOG.info("observatory agent connecting to Slack (model %s)", MODEL)
        SocketModeHandler(self.app, self.app_token).start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Agent(pathlib.Path(os.path.expanduser("~/.config/observatory"))).run()


if __name__ == "__main__":
    main()
