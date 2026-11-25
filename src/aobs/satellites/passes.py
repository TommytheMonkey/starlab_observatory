"""Predicted passes for the web UI, and booking a pass as a manual recording.

Uses the observatory location from Settings and the built-in predictor (predict.py). Booking goes
through the normal scheduler, so every reservation rule applies: the receiver must be free, a
real receiver must be verified (or already in service), and the device is locked for the
recording. Nothing here books anything automatically.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..scheduling.store import SchedulerError
from . import predict


class PassService:
    def __init__(self, s: Any):
        self.s = s

    def _doc(self, name: str) -> dict[str, Any]:
        cur = self.s.config_store.current(name)
        return (cur or {}).get("data") or {}

    def location(self) -> tuple[float, float, float] | None:
        o = self._doc("observatory")
        if o.get("latitude") is None or o.get("longitude") is None:
            return None
        return float(o["latitude"]), float(o["longitude"]), float(o.get("elevation_m") or 0)

    def satellites(self) -> dict[str, Any]:
        sats = self._doc("satellites")
        selected = list(sats.get("selected") or [])
        elements = predict.load_elements(self.s.config.data_root)
        switches = sats.get("downlinks") or {}
        catalog = [{"norad": n, **c, "downlinks": predict.downlinks(n, switches), "selected": n in selected}
                   for n, c in predict.CATALOG.items()]
        for n in selected:
            if n not in predict.CATALOG:
                e = elements.get(n)
                catalog.append({"norad": n, "name": e.name if e else f"NORAD {n}", "downlink_hz": None, "mode": None,
                                "selected": True, "custom": True})
        rules = getattr(self.s, "rules", None)
        wanted = sorted(set(selected) | (rules.satellites() if rules is not None else set()))
        last = predict.last_refresh(self.s.config.data_root)
        return {"catalog": catalog, "selected": selected, "min_elevation_deg": sats.get("min_elevation_deg", 20),
                "refresh_online": bool(sats.get("refresh_online")),
                "elements_source": sats.get("elements_source") or "celestrak_omm",
                "refresh_hours": sats.get("refresh_hours", 12), "last_refresh": last.isoformat() if last else None,
                "elements": predict.element_status(elements, wanted, self.s.clock(),
                                                   float(sats.get("stale_days") or predict.STALE_DAYS))}

    def refresh(self, fetch: Any = None) -> dict[str, Any]:
        sats = self._doc("satellites")
        if not sats.get("refresh_online"):
            raise SchedulerError("downloading orbital elements is off (Settings → Satellites → Download current "
                                 "orbital elements)", 409)
        rules = getattr(self.s, "rules", None)
        norads = sorted(set(sats.get("selected") or []) | (rules.satellites() if rules is not None else set()))
        try:
            sources = predict.source_list(sats)
        except ValueError as exc:
            raise SchedulerError(str(exc), 400) from exc
        return predict.refresh_tles(norads, self.s.config.data_root, fetch, sources=sources, now=self.s.clock())

    def predicted(self, hours: float = 48) -> dict[str, Any]:
        loc = self.location()
        sats = self._doc("satellites")
        out: dict[str, Any] = {"hours": hours, "satellites": self.satellites()}
        if loc is None:
            return {**out, "passes": [], "location_set": False,
                    "warning": "Set the observatory's latitude and longitude (Settings → Observatory) to predict passes."}
        elements = predict.load_elements(self.s.config.data_root)
        passes = predict.predict_passes(elements, list(sats.get("selected") or []), *loc, self.s.clock(),
                                        min(max(hours, 1), 240), float(sats.get("min_elevation_deg", 20)))
        rows = []
        switches = sats.get("downlinks") or {}
        for p in passes:          # one row per capture: a single downlink, or active downlinks close enough to share one
            jobs = self._jobs_of_pass(p)
            dls = predict.downlinks(p["norad"], switches)
            if not dls:                     # outside the catalog: no known downlink, booked by hand
                rows.append({**p, "downlink": None, "downlink_hz": None, "mode": None,
                             "booked": [b for b, _ in jobs]})
                continue
            groups = predict.group_downlinks([d for d in dls if d["active"]])
            covered = {d["id"] for g in groups for d in g}
            # a downlink turned off after booking stays visible with its recording, as its own row
            groups += [[d] for d in dls if d["id"] not in covered and any(d["id"] in ids for _, ids in jobs)]
            for g in groups:
                ids = {d["id"] for d in g}
                rows.append({**p, "downlink": predict.group_key(g), "downlink_hz": g[0]["hz"],
                             "mode": g[0]["mode"] if len(g) == 1 else
                             "; ".join(f"{d['hz'] / 1e6:.3f} MHz {d['mode']}" for d in g),
                             "pipeline": g[0]["pipeline"] if len(g) == 1 else None,
                             "bandwidth_hz": g[0]["bandwidth_hz"],
                             "downlinks": [{k: d[k] for k in ("id", "hz", "mode", "pipeline")} for d in g],
                             "downlink_active": all(d["active"] for d in g),
                             "booked": [b for b, got in jobs if got & ids]})
        inactive = [f"{predict.CATALOG[n]['name']} {d['hz'] / 1e6:.3f} MHz {d['mode'].split(' (')[0]}"
                    for n in sats.get("selected") or [] for d in predict.downlinks(n, switches) if not d["active"]]
        return {**out, "passes": rows, "location_set": True, "inactive_downlinks": inactive}

    def _jobs_of_pass(self, p: dict[str, Any]) -> list[tuple[dict[str, Any], set[str]]]:
        """The recordings booked for this pass, each with the downlinks it records."""
        aos, los = dt.datetime.fromisoformat(p["aos"]), dt.datetime.fromisoformat(p["los"])
        slack = dt.timedelta(minutes=5)            # predicted times have sub-second precision; jobs are whole seconds
        out = []
        for j in self.s.jobs(start=aos - slack, end=los + slack, limit=100):
            starts = dt.datetime.fromisoformat(j["record_start"])
            if (j.get("target") or "").upper() == p["name"].upper() and abs((starts - aos).total_seconds()) < 120 \
                    and j["status"] not in ("cancelled",):
                out.append(({"job_id": j["job_id"], "receiver_id": j["receiver_id"], "status": j["status"]},
                            job_downlinks(j.get("spec") or {}, p["norad"])))
        return out

    # ------------------------------------------------------------ booking
    def job_for_pass(self, rx: Any, p: dict[str, Any]) -> dict[str, Any]:
        """The job for a pass row on this receiver: the recording form's defaults, booked unchanged."""
        from ..scheduling import schedview
        if not predict.CATALOG.get(p["norad"]):
            raise SchedulerError(f"Starlab does not know the downlink of NORAD {p['norad']}; book it manually", 400)
        form = schedview.form_for_pass(self.s, p["norad"], p["aos"], rx.id, p.get("downlink"))
        return schedview.job_from_form(self.s, {k: v for k, v in form.items() if k != "receiver_ids"})

    def schedule(self, norad: int, aos: str, receiver_id: str, actor: str, dry_run: bool = False,
                 downlink: str | None = None) -> dict[str, Any]:
        rx = self.s.receivers.get(receiver_id)
        if rx is None:
            raise SchedulerError(f"no receiver {receiver_id}", 404)
        found = self.predicted(72)
        want = dt.datetime.fromisoformat(aos)
        p = next((x for x in found["passes"] if x["norad"] == int(norad) and (not downlink or x["downlink"] == downlink)
                  and abs((dt.datetime.fromisoformat(x["aos"]) - want).total_seconds()) < 60), None)
        if p is None:
            raise SchedulerError("that pass (or that downlink) is not in the current prediction (it may have started "
                                 "already, or the downlink is off in Settings → Satellites); reload", 409)
        res = self.s.submit([self.job_for_pass(rx, p)], "all_or_nothing", f"web:{actor}", dry_run=dry_run)
        r0 = res["results"][0]
        if not r0.get("ok"):
            raise SchedulerError("Not booked: " + "; ".join(r0.get("errors") or ["refused"]), 409)
        return {"ok": True, "job_id": r0.get("job_id"), "warnings": r0.get("warnings", []), "dry_run": dry_run,
                "simulated": rx.simulated}


def job_downlinks(spec: dict[str, Any], norad: int) -> set[str]:
    """The catalog downlinks a recording records: those it was booked for (one, or a group sharing one capture),
    else the one its target frequency falls in, else (no frequency on record: an older booking) the primary one."""
    meta = spec.get("metadata") or {}
    if meta.get("downlinks"):
        return {str(d["id"]) for d in meta["downlinks"]}
    named = (meta.get("downlink") or {}).get("id")
    if named:
        return {str(named)}
    radio = spec.get("radio") or {}
    hz = radio.get("target_frequency_hz") or radio.get("center_frequency_hz")
    d = predict.downlink_for(norad, hz) if hz else predict.downlink(norad, None)
    return {d["id"]} if d else set()
