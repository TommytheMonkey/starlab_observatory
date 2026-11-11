"""Per-channel activity heatmaps: channel x UTC hour of day, and channel x day (seconds on the air)."""

from __future__ import annotations

import datetime as dt
import html
import pathlib
from collections import defaultdict
from typing import Any

from .store import Store

UTC = dt.timezone.utc
SHADES = " .:-=+*#%@"


def grids(rows: list[dict[str, Any]], days: int, now: dt.datetime) -> dict[str, Any]:
    day_keys = [(now - dt.timedelta(days=d)).strftime("%Y-%m-%d") for d in range(days - 1, -1, -1)]
    by_hour: dict[int, list[float]] = defaultdict(lambda: [0.0] * 24)
    by_day: dict[int, dict[str, float]] = defaultdict(dict)
    totals: dict[int, dict[str, float]] = defaultdict(lambda: {"seconds": 0.0, "detections": 0, "speech": 0})
    for r in rows:
        ch, day, hour = r["channel_hz"], r["hour_utc"][:10], int(r["hour_utc"][11:13])
        if day not in day_keys:
            continue
        by_hour[ch][hour] += r["seconds"]
        by_day[ch][day] = by_day[ch].get(day, 0.0) + r["seconds"]
        t = totals[ch]
        t["seconds"] += r["seconds"]
        t["detections"] += r["detections"]
        t["speech"] += r["speech"]
    channels = sorted(totals)
    return {"channels": channels, "days": day_keys, "by_hour": dict(by_hour), "by_day": dict(by_day),
            "totals": dict(totals)}


def ascii(store: Store, days: int = 7, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(UTC)
    g = grids(store.activity((now - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H")), days, now)
    if not g["channels"]:
        return f"No activity in the last {days} days."
    peak = max(max(v) for v in g["by_hour"].values()) or 1.0
    lines = [f"Seconds on the air per UTC hour, last {days} days (darker = busier, peak {peak:.0f} s)",
             "channel MHz   " + "".join(f"{h:<3d}" for h in range(0, 24, 3)).ljust(24) + "  det  speech  total"]
    for ch in g["channels"]:
        row = "".join(SHADES[min(len(SHADES) - 1, int(round(v / peak * (len(SHADES) - 1))))] if v else "·"
                      for v in g["by_hour"][ch])
        t = g["totals"][ch]
        lines.append(f"{ch / 1e6:11.4f}   {row}  {t['detections']:4d}  {t['speech']:6d}  {t['seconds']:6.0f}s")
    return "\n".join(lines)


def render_html(store: Store, path: pathlib.Path, days: int = 14, now: dt.datetime | None = None) -> pathlib.Path:
    now = now or dt.datetime.now(UTC)
    g = grids(store.activity((now - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H")), days, now)
    recent = store.hits("speech", limit=25)

    def cells(values: list[float]) -> str:
        peak = max(values) if values and max(values) > 0 else 1.0
        out = []
        for v in values:
            a = 0 if not v else 0.15 + 0.85 * v / peak
            out.append(f'<td title="{v:.0f} s" style="--a:{a:.2f}"></td>')
        return "".join(out)

    hour_rows = "".join(
        f"<tr><th>{ch / 1e6:.4f}</th>{cells(g['by_hour'][ch])}<td class=n>{g['totals'][ch]['detections']}</td>"
        f"<td class=n>{g['totals'][ch]['speech']}</td></tr>" for ch in g["channels"])
    day_rows = "".join(
        f"<tr><th>{ch / 1e6:.4f}</th>{cells([g['by_day'][ch].get(d, 0.0) for d in g['days']])}</tr>"
        for ch in g["channels"])
    hits = "".join(
        f"<tr><td>{html.escape(h['start_utc'] or h['detected_at'])}</td><td>{h['freq_hz'] / 1e6:.4f}</td>"
        f"<td class=n>{(h['duration_s'] or 0):.0f} s</td><td class=n>{h['peak_db']}</td>"
        f"<td>{html.escape(h['language'] or '')}</td><td>{html.escape(h['transcript'] or '')}"
        + (f"<br><i>{html.escape(h['translation'])}</i>" if h['translation'] and h['translation'] != h['transcript'] else "")
        + "</td></tr>" for h in recent)
    empty = "<p>No activity recorded yet.</p>" if not g["channels"] else ""
    doc = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Pirate Watch Activity</title>
<style>
:root{{--bg:#fafaf8;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e2dc;--heat:204 92 28}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ececea;--muted:#9a9a94;--line:#2c2c2a;--heat:255 140 70}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px}} .m{{color:var(--muted)}}
.w{{overflow-x:auto}} table{{border-collapse:collapse}} th{{font-weight:500;text-align:right;padding:2px 8px;white-space:nowrap;font-variant-numeric:tabular-nums}}
.hm td{{width:18px;height:18px;border:1px solid var(--line);background:rgb(var(--heat)/var(--a))}}
.hm td.n{{width:auto;border:0;background:none;padding:0 8px;text-align:right}} .hm thead th{{font-size:11px;color:var(--muted);text-align:center;padding:0}}
.hits td{{border-top:1px solid var(--line);padding:4px 8px;vertical-align:top}} .hits td.n{{text-align:right;white-space:nowrap}}
</style></head><body>
<h1>Satcom pirate watch, 243–270 MHz</h1>
<p class=m>Updated {now:%Y-%m-%d %H:%M} UTC · last {days} days · cell = seconds on the air</p>{empty}
<h2>By UTC hour of day</h2><div class=w><table class=hm><thead><tr><th>MHz</th>{''.join(f'<th>{h}</th>' for h in range(24))}<th>det</th><th>speech</th></tr></thead><tbody>{hour_rows}</tbody></table></div>
<h2>By day</h2><div class=w><table class=hm><thead><tr><th>MHz</th>{''.join(f'<th>{d[8:]}</th>' for d in g['days'])}</tr></thead><tbody>{day_rows}</tbody></table></div>
<h2>Latest speech</h2><div class=w><table class=hits><tbody>{hits or '<tr><td>None yet.</td></tr>'}</tbody></table></div>
</body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(doc)
    tmp.replace(path)
    return path
