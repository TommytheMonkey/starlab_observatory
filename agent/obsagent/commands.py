"""Slash-command handlers beyond /radio-schedule. Each turns Slack text into one broker call and
formats the broker's answer; all logic (validation, locking, processing) lives in the broker.

Every handler returns (text, json_files, binary_files) like radio_commands' handlers.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import zoneinfo
from typing import Any

from .broker_client import BrokerClient

TZ = zoneinfo.ZoneInfo("America/New_York")
Reply = tuple[str, list[tuple[str, str]], list[tuple[str, bytes, str]]]
MAX_UPLOADS = 6
MAX_UPLOAD_BYTES = 15 * 1024**2

# permission tiers (see RadioCommands.TIERS)
READ, OPERATE, DISRUPT = "read", "operate", "disrupt"
TIER = {"/radio-status": READ, "/radio-health": READ, "/radio-recordings": READ, "/radio-results": READ,
        "/radio-jobs": READ, "/radio-capabilities": READ, "/radio-help": READ,
        "/radio-schedule": OPERATE, "/radio-decode": OPERATE, "/radio-analyze": OPERATE, "/radio-reprocess": OPERATE,
        "/radio-waterfall": OPERATE, "/radio-cancel": DISRUPT}


# ---------------------------------------------------------------- parsing helpers
def parse_args(text: str) -> tuple[list[str], dict[str, Any], dict[str, Any] | None]:
    """'latest receiver=OBS-R01 decoded=false' -> (['latest'], {...}, None); a JSON object -> ([], {}, obj)."""
    t = html.unescape(text or "").strip()
    t = t.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    if t.startswith("{"):
        try:
            return [], {}, json.loads(t)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}") from None
    words, kv = [], {}
    for tok in re.findall(r'(\w[\w.-]*="[^"]*"|\S+)', t):
        if "=" in tok:
            k, v = tok.split("=", 1)
            kv[k.lower()] = v.strip('"')
        else:
            words.append(tok)
    return words, kv, None


def num(v: Any) -> Any:
    if isinstance(v, str):
        if re.fullmatch(r"-?\d+", v):
            return int(v)
        if re.fullmatch(r"-?\d*\.\d+(e\d+)?|-?\d+e\d+", v, re.I):
            f = float(v)
            return int(f) if f.is_integer() and abs(f) > 1000 else f
        if v.lower() in ("true", "yes"):
            return True
        if v.lower() in ("false", "no"):
            return False
    return v


def hz(v: Any) -> int:
    """137.9 / 137.9MHz / 137900000 -> Hz."""
    s = str(v).lower().replace("mhz", "e6").replace("khz", "e3").replace("hz", "")
    f = float(s)
    return int(f * 1e6) if f < 10_000 else int(f)


def local(iso: str | None, fmt: str = "%a %b %d %H:%M %Z") -> str:
    if not iso:
        return "-"
    return dt.datetime.fromisoformat(iso).astimezone(TZ).strftime(fmt)


def size(n: Any) -> str:
    if not isinstance(n, (int, float)) or not n:
        return "-"
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


def mhz(v: Any) -> str:
    return f"{v / 1e6:.4f} MHz" if isinstance(v, (int, float)) else "-"


def dur(s: Any) -> str:
    if not isinstance(s, (int, float)):
        return "-"
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def rec_id(words: list[str], kv: dict[str, Any], obj: dict[str, Any] | None) -> str:
    rid = (obj or {}).get("recording_id") or kv.get("recording") or kv.get("rec") or kv.get("recording_id")
    if not rid:
        rid = next((w for w in words if re.fullmatch(r"[malr]\d{12}[0-9a-f]{6}", w)), None)
    if not rid:
        raise ValueError("which recording? give recording=<id> (find ids with /radio-recordings)")
    return rid


# ---------------------------------------------------------------- handlers
class Handlers:
    def __init__(self, broker: BrokerClient):
        self.b = broker

    # ---- /radio-status
    def status(self, text: str) -> Reply:
        words, kv, _ = parse_args(text)
        if kv.get("receiver"):
            r = self.b.get("/v1/status", receiver=kv["receiver"])
            return format_receiver_detail(r), [], []
        what = words[0].lower() if words else "all"
        if what == "health":
            return self.health("")
        st = self.b.get("/v1/status")
        return format_status(st, what), [("observatory-status.json", json.dumps(st, indent=2))] if what == "all" else [], []

    def health(self, text: str) -> Reply:
        h = self.b.get("/v1/health")
        return format_health(h), [], []

    # ---- /radio-recordings
    def recordings(self, text: str) -> Reply:
        words, kv, _ = parse_args(text)
        if words and words[0].lower() == "import":
            res = self.b.post("/v1/catalog/import", {"dry_run": "apply" not in [w.lower() for w in words]})
            return format_import(res), [], []
        q: dict[str, Any] = {"per_page": int(kv.get("limit", kv.get("per_page", 10))), "page": int(kv.get("page", 1))}
        for w in (x.lower() for x in words):
            if w == "latest":
                q["per_page"] = 1
            elif w == "today":
                q["date"] = dt.datetime.now(TZ).date().isoformat()
            elif w == "yesterday":
                q["date"] = (dt.datetime.now(TZ).date() - dt.timedelta(days=1)).isoformat()
            elif w == "failed":
                q["status"] = "failed"
            elif w == "partial":
                q["status"] = "partial"
            elif re.fullmatch(r"\d{4}-\d\d-\d\d", w):
                q["date"] = w
        mapping = {"receiver": "receiver", "satellite": "satellite", "target": "satellite", "date": "date",
                   "since": "since", "until": "until", "format": "format", "status": "status", "decoded": "decoded",
                   "analyzed": "analyzed", "min_size_gb": "min_size_gb", "experiment": "experiment",
                   "recording": "recording_id"}
        for k, v in kv.items():
            if k in mapping:
                q[mapping[k]] = num(v) if k in ("decoded", "analyzed", "min_size_gb") else v
            elif k in ("frequency", "freq"):
                q["frequency_hz"] = hz(v)
        res = self.b.get("/v1/catalog/recordings", **q)
        return format_recordings(res), [], []

    # ---- /radio-results
    def results(self, text: str) -> Reply:
        words, kv, obj = parse_args(text)
        rid = rec_id(words, kv, obj)
        typ = kv.get("type") or next((w for w in words if w in ("images", "waterfall", "spectrum", "plots", "diagnostics",
                                                                   "logs", "metadata", "all", "products")), None)
        attempt = kv.get("attempt") or next((w for w in words if re.fullmatch(r"p\d{14}[0-9a-f]{6}", w)), None)
        res = self.b.get(f"/v1/catalog/recordings/{rid}/results", type=typ, attempt_id=attempt)
        text_out = format_results(res, typ)
        uploads: list[tuple[str, bytes, str]] = []
        if typ in ("images", "waterfall", "spectrum", "plots", "diagnostics", "all", None):
            want = [a for a in res["artifacts"] if a["kind"] in ("image", "waterfall", "spectrum", "diagnostics")
                    and (a.get("bytes") or 0) <= MAX_UPLOAD_BYTES and a["path"].lower().endswith((".png", ".jpg", ".jpeg"))]
            if typ is None:
                want = [a for a in want if a["kind"] in ("image", "waterfall")]
            if typ == "images":
                want = [a for a in want if a["kind"] == "image" and "(blank)" not in (a.get("label") or "")]
            want = list(reversed(want))[:MAX_UPLOADS]          # newest first
            for a in want:
                try:
                    uploads.append((a["path"].rsplit("/", 1)[-1], self.b.artifact_bytes(a["artifact_id"]),
                                    f"{rid} · {a['kind']} · {a.get('label') or ''} · {a['artifact_id']}"))
                except Exception as exc:  # noqa: BLE001 - list it, keep going
                    text_out += f"\n(could not fetch {a['artifact_id']}: {exc})"
        if kv.get("link"):
            link = self.b.get(f"/v1/artifacts/{kv['link']}/link", ttl_s=int(kv.get("ttl", 3600)))
            text_out += f"\n\nDownload link (tailnet only, expires {local(link['expires_at'])}): {link['url']}"
        return text_out, [], uploads

    # ---- /radio-decode, /radio-reprocess
    def decode(self, text: str) -> Reply:
        words, kv, obj = parse_args(text)
        rid = rec_id(words, kv, obj)
        if obj:
            params = {k: v for k, v in obj.items() if k not in ("recording_id", "operation")}
            params.update(params.pop("parameters", {}) or {})
        else:
            params = {k: num(v) for k, v in kv.items() if k not in ("recording", "rec", "recording_id")}
            for k in ("target_frequency_hz", "frequency", "target"):
                if k in params:
                    params["target_frequency_hz"] = hz(params.pop(k))
            if "offset" in params:
                params["frequency_offset_hz"] = int(params.pop("offset"))
            if "output_rate" in params:
                params["output_sample_rate_hz"] = int(params.pop("output_rate"))
            for flag in [w for w in words if not re.fullmatch(r"[malr]\d{12}[0-9a-f]{6}", w)]:
                params.setdefault("additional_parameters", {})[flag.lstrip("-")] = True
            if "start" in params:
                params["start_s"] = params.pop("start")
            if "end" in params:
                params["end_s"] = params.pop("end")
        params.pop("priority", None)
        att = self.b.post(f"/v1/catalog/recordings/{rid}/process", {"kind": "decode", "params": params, "origin": "slack"})
        return format_attempt_queued(att), [], []

    def reprocess(self, text: str) -> Reply:
        words, kv, obj = parse_args(text)
        op = (obj or {}).get("operation") or kv.get("operation") or "satdump_decode"
        if op in ("satdump_decode", "decode"):
            return self.decode(text)
        if op in ("analyze", "analysis", "waterfall"):
            return self.analyze(text)
        raise ValueError("operation must be satdump_decode or analyze")

    # ---- /radio-analyze, /radio-waterfall
    def analyze(self, text: str, waterfall_only: bool = False) -> Reply:
        words, kv, obj = parse_args(text)
        if kv.get("compare") or (obj or {}).get("compare"):
            ids = (obj or {}).get("compare") or kv["compare"].split(",")
            ids = [i.strip() for i in ids if i.strip()]
            params = {"recordings": ids}
            for k in ("fft_size", "start_s", "end_s", "target_frequency_hz"):
                if k in kv:
                    params[k] = num(kv[k])
            att = self.b.post(f"/v1/catalog/recordings/{ids[0]}/process", {"kind": "compare", "params": params, "origin": "slack"})
            return format_attempt_queued(att), [], []
        rid = rec_id(words, kv, obj)
        if obj:
            params = {k: v for k, v in obj.items() if k not in ("recording_id", "operation")}
            params.update(params.pop("parameters", {}) or {})
        else:
            params = {}
            alias = {"fft": "fft_size", "fft_size": "fft_size", "window": "fft_window", "overlap": "overlap",
                     "start": "start_s", "end": "end_s", "dynamic_range": "dynamic_range_db", "colormap": "colormap",
                     "rows": "rows", "width": "width", "height": "height", "threshold": "threshold_db",
                     "frequency_min_hz": "frequency_min_hz", "frequency_max_hz": "frequency_max_hz"}
            for k, v in kv.items():
                if k in alias:
                    params[alias[k]] = num(v)
            if "analysis" in kv:
                params["analysis"] = kv["analysis"].split(",")
            if "frequency" in kv:
                f = hz(kv["frequency"])
                bw = hz(kv.get("bandwidth", kv.get("span", "200000")))
                params["frequency_min_hz"], params["frequency_max_hz"] = f - bw // 2, f + bw // 2
            if "target" in kv:
                params["target_frequency_hz"] = hz(kv["target"])
        if waterfall_only:
            params["analysis"] = ["waterfall"]
        att = self.b.post(f"/v1/catalog/recordings/{rid}/process", {"kind": "analyze", "params": params, "origin": "slack"})
        return format_attempt_queued(att), [], []

    def waterfall(self, text: str) -> Reply:
        return self.analyze(text, waterfall_only=True)

    # ---- /radio-jobs
    def jobs(self, text: str) -> Reply:
        words, kv, obj = parse_args(text)
        if kv.get("job") or (words and re.fullmatch(r"[malr]\d{12}[0-9a-f]{6}", words[0])):
            jid = kv.get("job") or words[0]
            d = self.b.get(f"/v1/catalog/jobs/{jid}")
            return format_job_detail(d), [(f"{jid}-settings.json", json.dumps(d.get("spec"), indent=2))], []
        if kv.get("clone"):
            body = {"start": kv["start"], "end": kv.get("end"), "duration_seconds": int(kv["duration"]) if kv.get("duration") else None,
                    "receiver_id": kv.get("receiver"), "job_name": kv.get("name"),
                    "dry_run": "book" not in [w.lower() for w in words], "requested_by": "slack"}
            from .radio_commands import format_result
            res = self.b.post(f"/v1/catalog/jobs/{kv['clone']}/clone", body)
            text_out, files = format_result(res)
            if res.get("dry_run"):
                text_out += "\n\nAdd the word `book` to schedule it."
            return text_out, files, []
        when = next((w.lower() for w in words if w.lower() in ("today", "tomorrow")), None)
        state = next((w.lower() for w in words if w.lower() in ("active", "queued", "scheduled", "failed", "preempted")), None)
        res = self.b.get("/v1/catalog/jobs", when=when, state=state, receiver=kv.get("receiver"), limit=int(kv.get("limit", 30)))
        return format_job_list(res["jobs"]), [], []

    # ---- /radio-cancel
    def cancel(self, text: str) -> Reply:
        words, kv, obj = parse_args(text)
        body: dict[str, Any] = {"confirm": kv.get("confirm"), "reason": kv.get("reason", "cancelled from Slack")}
        if kv.get("processing") or (words and re.fullmatch(r"p\d{14}[0-9a-f]{6}", words[0])):
            body["attempt_id"] = kv.get("processing") or words[0]
        elif words and words[0].lower() == "batch":
            body["batch"] = {k: kv.get(k) for k in ("receiver", "source", "start", "end")}
            if kv.get("jobs"):
                body["batch"]["job_ids"] = kv["jobs"].split(",")
        else:
            jid = kv.get("job") or (words[0] if words else None)
            if not jid:
                raise ValueError("usage: /radio-cancel <job_id> [confirm=code] | processing=<attempt> | batch receiver=… start=… end=…")
            body["job_id"] = jid
        res = self.b.post("/v1/catalog/cancel", body)
        return format_cancel(res), [], []


# ---------------------------------------------------------------- formatting
def format_status(st: dict[str, Any], what: str = "all") -> str:
    lines = []
    if what in ("all", "services"):
        cal = st["calendar"]
        lines += ["*RADIO OBSERVATORY STATUS*", f"Host: {st['host']} · Broker: {st['broker']['state']} "
                  f"(up {dur(st['broker']['uptime_s'])}) · Slack bot: ONLINE (this reply) · Calendar sync: {cal['state']}"]
    if what in ("all", "receivers"):
        lines.append("\n*RECEIVERS*")
        for r in st["receivers"]:
            line = f"*{r['receiver_id']}* · {r['state']} · mode {r['mode']} · {r['antenna']}"
            cj = r.get("current_job")
            if cj:
                line += f"\n    now: {cj['target'] or cj['job_name']} ({cj['source']})"
                if cj.get("elapsed_s") is not None:
                    line += f" · elapsed {dur(cj['elapsed_s'])} · remaining {dur(cj['remaining_s'])}"
            if r.get("next_job"):
                n = r["next_job"]
                line += f"\n    next: {local(n['record_start'], '%H:%M')} {n['target'] or n['job_name']} ({n['source']})"
            if r.get("last_error"):
                line += f"\n    last error: {local(r['last_error']['record_start'], '%b %d %H:%M')} {r['last_error']['reason'] or ''}"[:220]
            if r.get("iq_health"):
                line += f"\n    IQ health: {iq_brief(r['iq_health'])}"
            lines.append(line)
    if what in ("all", "processing"):
        p = st["processing"]
        lines.append(f"\n*PROCESSING* queued {p['queued']} · running {p['running']} · paused {p['paused']} · failed {p['failed']}")
    if what in ("all", "storage"):
        s = st["storage"]
        lines.append(f"\n*STORAGE* {s.get('free_gb', '?')} GB free of {s.get('total_gb', '?')} GB on /data · {s.get('state')}"
                     + ("" if s.get("mounted") else " · NOT MOUNTED"))
    if what in ("all", "jobs"):
        lines.append("\n*NEXT SCHEDULED*")
        for j in st["next_jobs"][: 8 if what == "all" else 20]:
            lines.append(f"{local(j['start'], '%H:%M')}  {j['target']}  ({', '.join(j['receivers'])})")
        if not st["next_jobs"]:
            lines.append("nothing in the next 24 h")
    if what in ("all", "jobs") and st.get("recent_failures"):
        lines.append("\n*FAILURES (24 h)*")
        for f in st["recent_failures"]:
            lines.append(f"{local(f['record_start'], '%H:%M')} {f['target'] or f['job_name']} `{f['job_id']}`: {(f['reason'] or '')[:120]}")
    return "\n".join(lines)


def format_receiver_detail(r: dict[str, Any]) -> str:
    s = r.get("settings") or {}
    gain = s.get("gain_db") if s.get("gain_db") is not None else s.get("gain_elements")
    procs = ", ".join(f"{p['owner']}[{p['pid']}]" for p in r.get("processes", [])) or "none visible"
    lines = [f"*{r['receiver_id']}* · {r['state']}",
             f"Driver: {r['driver']} · serial `{r['serial']}` · {r['hardware']} · connected: {'yes' if r['connected'] else 'NO'}",
             f"Mode: {r['mode']}" + (f" · legacy service {r['legacy_unit']} is {r['legacy_unit_state']}" if r.get("legacy_unit") else ""),
             f"Antenna: {r['antenna']} · path: {r['signal_path']}",
             f"Processes using it: {procs}" + (f" ({r['unreadable_processes']} processes not inspectable)" if r.get("unreadable_processes") else ""),
             f"Lease: {r['lease']['owner']} until {local(r['lease']['expires_at'], '%H:%M')}" if r.get("lease") else "Lease: none",
             f"Current job: {r['current_job']['target'] or r['current_job']['job_name']} `{r['current_job']['job_id']}`"
             if r.get("current_job") else "Current job: none",
             f"Settings: center {mhz(s.get('center_frequency_hz'))} · rate {s.get('sample_rate_hz', 0) / 1e6:.3f} MS/s · gain {gain} · bias tee {s.get('bias_tee')}"
             if s else "Settings: -",
             f"Next job: {local(r['next_job']['record_start'])} {r['next_job']['target']} `{r['next_job']['job_id']}`" if r.get("next_job") else "Next job: none",
             f"Last good recording: {local(r['last_successful_recording']['record_start'])} `{r['last_successful_recording']['job_id']}`"
             if r.get("last_successful_recording") else "Last good recording: none on record",
             f"Last error: {r['last_error']['reason']}" if r.get("last_error") else "Last error: none (48 h)"]
    if r.get("iq_health"):
        h = r["iq_health"]
        lines.append(f"IQ health (last recording `{h['recording_id']}`): {iq_brief(h)}"
                     + (f" · {h['gain_response']['note']}" if h.get("gain_response") else "")
                     + (f" · tuner {h['tuner_status']}" if h.get("tuner_status") else ""))
    if r.get("notes"):
        lines.append(f"Note: {r['notes']}")
    return "\n".join(lines)


def iq_brief(h: dict[str, Any]) -> str:
    icon = {"healthy": "", "weak_signal": ":warning: ", "possible_overload": ":warning: ",
            "suspicious_flat": ":rotating_light: ", "capture_failure": ":x: "}.get(h.get("status"), "")
    codes = h.get("unique_codes") or [None, None]
    return (f"{icon}{h.get('label')} · RMS {h.get('rms_dbfs')} dBFS · spread {h.get('sample_spread_lsb')} LSB · "
            f"{codes[0]}/{codes[1]} I/Q codes · gain {h.get('requested_gain_db')} dB")


def format_health(h: dict[str, Any]) -> str:
    icon = {"PASS": ":white_check_mark:", "WARNING": ":warning:", "FAIL": ":x:", "UNKNOWN": ":grey_question:"}
    lines = [f"*OBSERVATORY HEALTH: {h['overall']}*"]
    for c in h["checks"]:
        line = f"{icon[c['status']]} *{c['area']}* · {c['check']}: {c['detail']}"
        if c.get("action"):
            line += f"\n      → {c['action']}"
        lines.append(line)
    return "\n".join(lines)


def format_recordings(res: dict[str, Any]) -> str:
    if not res["recordings"]:
        return "No recordings match."
    lines = []
    for r in res["recordings"]:
        arts = ", ".join(f"{v} {k}" for k, v in r["artifacts"].items()) or "none"
        lines.append(f"`{r['recording_id']}` · *{r['target'] or '?'}* · {r['receiver']} · {r['start_local']} · {dur(r['duration_s'])}\n"
                     f"    center {mhz(r['center_frequency_hz'])} · target {mhz(r['target_frequency_hz'])} · "
                     f"{(r['sample_rate_hz'] or 0) / 1e6:.3f} MS/s {r['format'] or '?'} · {size(r['size_bytes'])} · {r['status']}\n"
                     f"    decode: {r['decode']} · analysis: {r['analysis']} · artifacts: {arts}"
                     + (f"\n    :warning: uncertain metadata: {', '.join(r['uncertain_metadata'])}" if r.get("uncertain_metadata") else ""))
    lines.append(f"_page {res['page']} of {res['pages']} ({res['total']} recordings); add page={res['page'] + 1} for more_"
                 if res["page"] < res["pages"] else f"_{res['total']} recording(s)_")
    return "\n".join(lines)


def format_results(res: dict[str, Any], typ: str | None) -> str:
    r = res["recording"]
    lines = [f"*RECORDING {r['recording_id']}*",
             f"Target: {r['target'] or '?'} · Receiver: {r['receiver']} · Status: {r['status']}",
             f"Start: {r['start_local']} · Duration: {dur(r['duration_s'])} · Size: {size(r['size_bytes'])}",
             f"Center: {mhz(r['center_frequency_hz'])} · Target: {mhz(r['target_frequency_hz'])} · "
             f"Rate: {(r['sample_rate_hz'] or 0) / 1e6:.3f} MS/s · Format: {r['format']}",
             f"Signal path: {r['signal_path']}" + (f" _(layout v{r['rf_path']['layout_version']})_" if r.get("rf_path") else "")
             if r.get("signal_path") else "Signal path: not recorded (made before the RF layout existed)"]
    if res["checksums_sha256"]:
        lines.append(f"SHA-256: `{res['checksums_sha256'][0]}`")
    lines.append("\n*PROCESSING*" if res["attempts"] else "\nNo processing attempts yet. Try /radio-decode or /radio-analyze.")
    for a in res["attempts"]:
        head = f"`{a['attempt_id']}` · {a['kind']} · *{a['status']}* · {a['origin']}"
        if a["kind"] == "decode":
            p = a.get("parameters") or {}
            head += (f"\n    {p.get('pipeline')} · target {mhz(p.get('target_frequency_hz'))} ({p.get('shift_method')}) · "
                     f"images {a.get('images', 0)} · decoded: {'YES' if a.get('decoded') else 'no'}"
                     + (f" · SNR max {a['snr_db_max']:.1f} dB" if a.get("snr_db_max") else ""))
        elif a.get("summary"):
            s = a["summary"]
            bits = []
            if s.get("target"):
                bits.append(f"target {s['target']['mean_over_floor_db']} dB over floor (mean)")
            if s.get("spectrum"):
                bits.append(f"floor {s['spectrum']['noise_floor_dbfs_per_hz']} dBFS/Hz, peak {mhz(s['spectrum']['peak_frequency_hz'])}")
            if s.get("findings"):
                bits.append("findings: " + "; ".join(f"{f['finding']} {f['status']}" for f in s["findings"]))
            if s.get("integrity") and not s["integrity"]["consistent"]:
                bits.append("integrity: " + "; ".join(s["integrity"]["issues"]))
            if s.get("warnings"):
                bits.append("note: " + "; ".join(s["warnings"][:2]))
            if bits:
                head += "\n    " + "\n    ".join(bits)
        if a.get("reason") and a["status"] != "completed":
            head += f"\n    reason: {a['reason'][:200]}"
        lines.append(head)
    if res["latest_successful"]:
        lines.append("Latest successful: " + ", ".join(f"{k} `{v}`" for k, v in res["latest_successful"].items()))
    lines.append(f"\n*ARTIFACTS*{' (' + typ + ')' if typ else ''}")
    for i, a in enumerate(res["artifacts"][:40], 1):
        lines.append(f"{i}. {a['path'].rsplit('/', 1)[-1]} · {a['kind']} · {size(a.get('bytes'))} · `{a['artifact_id']}`")
    if len(res["artifacts"]) > 40:
        lines.append(f"…{len(res['artifacts']) - 40} more; filter with type=")
    if not res["artifacts"]:
        lines.append("none")
    lines.append("_Large files (raw IQ): `/radio-results <rec> link=<artifact_id>` gives an expiring tailnet download link._")
    return "\n".join(lines)


def format_attempt_queued(a: dict[str, Any]) -> str:
    p = a.get("resolved") or {}
    lines = [f"*QUEUED* {a['kind']} `{a['attempt_id']}` for recording `{a['recording_id']}`"]
    if a["kind"] == "decode":
        lines.append(f"Pipeline {p.get('pipeline')} · center {mhz(p.get('center_frequency_hz'))} → target {mhz(p.get('target_frequency_hz'))} "
                     f"({p.get('frequency_offset_hz', 0) / 1e3:+.0f} kHz) · shift: {p.get('shift_method')} · "
                     f"SatDump input {p.get('output_sample_rate_hz', 0) / 1e3:.0f} kS/s")
    elif a["kind"] == "compare":
        lines.append(f"Comparing {', '.join(p.get('recordings', []))}")
    else:
        lines.append(f"Analyses: {', '.join(p.get('analysis', []))} · FFT {p.get('fft_size')}")
    lines.append("Runs at low priority (pauses during recordings). I'll post when it finishes; "
                 f"`/radio-results {a['recording_id']} {a['attempt_id']}` any time.")
    return "\n".join(lines)


def format_job_list(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "No jobs match."
    out = []
    for j in jobs:
        out.append(f"{local(j['start'], '%a %H:%M')}–{local(j['end'], '%H:%M')} {j['receiver']} *{j['status']}* "
                   f"{j['target']} `{j['job_id']}`\n    {j['priority']} · conflict: {j['conflict']} · calendar: {j['calendar']}"
                   + (f" · recording `{j['recording_id']}`" if j.get("recording_id") else ""))
    return "\n".join(out)


def format_job_detail(d: dict[str, Any]) -> str:
    spec = d.get("spec") or {}
    radio = spec.get("radio") or {}
    lines = [f"*JOB {d['job_id']}* · {d['job_name']} · {d['source']} · *{d['status']}*",
             f"Reason: {d.get('status_reason') or '-'}",
             f"Receiver {d['receiver_id']} (serial `{d['device_serial']}`) · target {d.get('target') or '-'}",
             f"Recording {local(d['record_start'])} → {local(d['record_end'], '%H:%M:%S %Z')} · reservation "
             f"{local(d['reservation_start'], '%H:%M:%S')} → {local(d['reservation_end'], '%H:%M:%S')}",
             f"Center {mhz(radio.get('center_frequency_hz'))} · target {mhz(radio.get('target_frequency_hz'))} · "
             f"{(radio.get('sample_rate_hz') or 0) / 1e6:.3f} MS/s · gain {radio.get('gain_db', radio.get('gain_elements'))} · "
             f"bias tee {radio.get('bias_tee')}",
             f"Calendar: {d['calendar']['state']}" + (f" ({d['calendar']['error']})" if d["calendar"].get("error") else "")]
    if d.get("processing"):
        lines.append(f"Processing: decode {d['processing']['decode']} · analysis {d['processing']['analysis']}")
    lines.append("History: " + " → ".join(f"{e['new_status']} {local(e['at'], '%H:%M:%S')}" for e in d.get("events", [])))
    lines.append(f"_Exact settings attached. Clone: `/radio-jobs clone={d['job_id']} start=<ISO time> duration=<s>` (add `book` to schedule)._")
    return "\n".join(lines)


def format_cancel(res: dict[str, Any]) -> str:
    if res.get("needs_confirmation"):
        if "preview" in res:
            lines = [f"*{res['matched']} scheduled job(s) would be cancelled:*"]
            lines += [f"• {local(p['start'], '%a %H:%M')} {p['receiver']} {p['target']} `{p['job_id']}`" for p in res["preview"][:30]]
            lines.append(f"{res.get('note', '')}\nRepeat the same command with `confirm={res['confirm']}` to cancel exactly these.")
            return "\n".join(lines)
        return f":warning: {res['warning']}"
    if res.get("already"):
        return f"`{res.get('job_id') or res.get('attempt_id')}` was already {res['status']}."
    if "cancelled" in res and isinstance(res["cancelled"], list):
        return f"Cancelled {len(res['cancelled'])} of {res['matched']} job(s): {', '.join(res['cancelled']) or 'none'}"
    return f"`{res.get('job_id') or res.get('attempt_id')}`: {res['status']}" + (f" ({res['note']})" if res.get("note") else "")


def format_import(res: dict[str, Any]) -> str:
    lines = [f"*{'DRY RUN: would import' if res['dry_run'] else 'Imported'} {res['found']} recording(s)* from {', '.join(res['roots'])}"]
    for f in res["files"][:20]:
        m = f["metadata"]
        lines.append(f"• {f['path']} · {size(f['bytes'])} · {m.get('receiver', '?')} · center {mhz(m.get('center_frequency_hz'))} · "
                     f"rate {m.get('sample_rate_hz') or '?'}" + (f" · uncertain: {', '.join(f['uncertain'])}" if f["uncertain"] else ""))
    for s in res["skipped"][:10]:
        lines.append(f"skipped {s['path']}: {s['why']}")
    if res["dry_run"] and res["found"]:
        lines.append("Run `/radio-recordings import apply` to create these records (files are not moved or copied).")
    return "\n".join(lines)


HELP = {
    "": """*Radio observatory commands* (`/radio-help <command>` for details)
*Read:* /radio-status · /radio-health · /radio-recordings · /radio-results · /radio-jobs · /radio-capabilities
*Operate:* /radio-schedule · /radio-decode · /radio-analyze · /radio-waterfall · /radio-reprocess
*Disrupt:* /radio-cancel (asks for a confirmation code before stopping anything running)
Everything is tied to a recording id (`m…` manual, `a…` automatic, `l…` legacy recorder, `r…` imported).
Typical flow: `/radio-recordings latest` → `/radio-decode recording=<id>` → `/radio-analyze recording=<id>` → `/radio-results <id> type=images`""",
    "status": """*/radio-status* [receivers|jobs|processing|storage|services|health] [receiver=<id>]
Live state of every receiver (who is using it, legacy services, current/next job), processing queue, storage and upcoming jobs.""",
    "health": "*/radio-health* runs functional checks (hardware, software, storage, scheduling, processing): PASS/WARNING/FAIL/UNKNOWN with an action for each.",
    "recordings": """*/radio-recordings* [latest|today|yesterday|failed|partial|YYYY-MM-DD] [receiver= satellite= frequency=137.9 format=cu8 status= decoded=true|false analyzed= min_size_gb= experiment= since= until= limit= page=]
`/radio-recordings import` previews indexing historical IQ files from the configured folders; `import apply` records them (files stay where they are).""",
    "results": """*/radio-results* <recording_id> [type=images|waterfall|spectrum|plots|diagnostics|logs|metadata|products|all] [attempt=<id>] [link=<artifact_id> ttl=3600]
Every processing attempt with its status and parameters, every artifact; small images are uploaded here, large files get an expiring tailnet link.""",
    "decode": """*/radio-decode* recording=<id> [pipeline=meteor_m2-x_lrpt] [target=137.9 | offset=400000] [output_rate=500000] [shift_method=internal|satdump] [start=60 end=600] [dc_block]
Defaults come from the recording (center, rate, format, target). With the target off-center the channel is shifted to 0 Hz and decimated before SatDump. JSON form: `{"recording_id": "...", "pipeline": "...", "target_frequency_hz": 137900000, "parameters": {}}`.
A decode counts as successful only when SatDump produced non-blank images.""",
    "reprocess": "*/radio-reprocess* recording=<id> operation=satdump_decode|analyze [parameters…]: a new attempt with other settings; earlier results are kept.",
    "analyze": """*/radio-analyze* recording=<id> [analysis=waterfall,spectrum,signal_detection,frequency_offset,signal_quality,clipping,noise_floor,integrity,checksum|all] [fft=8192 window=hann overlap=0.5 frequency=137.9 bandwidth=250000 start= end= dynamic_range=60 colormap=viridis]
*Compare:* `/radio-analyze compare=<id1>,<id2>` (each normalized to its own noise floor, with comparability warnings). Levels are dBFS, not calibrated power.""",
    "waterfall": "*/radio-waterfall* recording=<id> [fft=16384] [frequency=137.9 bandwidth=250000] [start= end=] [dynamic_range=60 colormap=inferno]: same engine as /radio-analyze.",
    "jobs": """*/radio-jobs* [today|tomorrow|active|queued|failed|preempted] [receiver=<id>]
`/radio-jobs <job_id>` shows exact settings (JSON attached) and history. `/radio-jobs clone=<job_id> start=2026-10-14T09:00:00-04:00 duration=900 [receiver=…]` validates a copy; add `book` to schedule it.""",
    "cancel": """*/radio-cancel* <job_id> | job=<id> [confirm=<code>] | processing=<attempt_id> | batch receiver=… start=… end=… [confirm=<code>]
Scheduled jobs are cancelled at once (the calendar shows it). A running recording needs the confirmation code it replies with; what was captured is kept as a partial recording. Repeating a cancel is harmless.""",
    "schedule": """*/radio-schedule* [dry-run] {"jobs": [...]} books recordings atomically (all_or_nothing unless "transaction_mode": "allow_partial"). Run /radio-capabilities first for each receiver's limits. Pasting the same JSON in this DM works too.""",
    "capabilities": "*/radio-capabilities* [receiver]: hardware limits, antennas, settings and reservations; the attached JSON is what ChatGPT needs to write valid jobs.",
}


def help_text(topic: str) -> str:
    t = topic.strip().lower().replace("/radio-", "")
    return HELP.get(t, HELP[""] if not t else f"No help for {t!r}.\n" + HELP[""])
