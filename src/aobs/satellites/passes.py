"""Predicted passes for the web UI, and booking a pass as a manual recording.

Uses the observatory location from Settings and the built-in predictor (predict.py). Booking goes
through the normal scheduler, so every reservation rule applies: the receiver must be free, a
real receiver must be verified (or already in service), and the device is locked for the
recording. Nothing here books anything automatically.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..scheduling.hardware import default_radio
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
        for p in passes:                    # one row per active downlink: each is booked on its own receiver
            jobs = self._jobs_of_pass(p)
            dls = predict.downlinks(p["norad"], switches)
            if not dls:                     # outside the catalog: no known downlink, booked by hand
                rows.append({**p, "downlink": None, "downlink_hz": None, "mode": None,
                             "booked": [b for b, _ in jobs]})
                continue
            for d in dls:
                booked = [b for b, dl in jobs if dl == d["id"]]
                if d["active"] or booked:       # a downlink turned off after booking stays visible with its recording
                    rows.append({**p, "downlink": d["id"], "downlink_hz": d["hz"], "mode": d["mode"],
                                 "pipeline": d["pipeline"], "bandwidth_hz": d["bandwidth_hz"],
                                 "downlink_active": d["active"], "booked": booked})
        inactive = [f"{predict.CATALOG[n]['name']} {d['hz'] / 1e6:.3f} MHz {d['mode'].split(' (')[0]}"
                    for n in sats.get("selected") or [] for d in predict.downlinks(n, switches) if not d["active"]]
        return {**out, "passes": rows, "location_set": True, "inactive_downlinks": inactive}

    def _jobs_of_pass(self, p: dict[str, Any]) -> list[tuple[dict[str, Any], str | None]]:
        """The recordings booked for this pass, each with the downlink it tunes to."""
        aos, los = dt.datetime.fromisoformat(p["aos"]), dt.datetime.fromisoformat(p["los"])
        slack = dt.timedelta(minutes=5)            # predicted times have sub-second precision; jobs are whole seconds
        out = []
        for j in self.s.jobs(start=aos - slack, end=los + slack, limit=100):
            starts = dt.datetime.fromisoformat(j["record_start"])
            if (j.get("target") or "").upper() == p["name"].upper() and abs((starts - aos).total_seconds()) < 120 \
                    and j["status"] not in ("cancelled",):
                out.append(({"job_id": j["job_id"], "receiver_id": j["receiver_id"], "status": j["status"]},
                            job_downlink(j.get("spec") or {}, p["norad"])))
        return out

    # ------------------------------------------------------------ booking
    def job_for_pass(self, rx: Any, p: dict[str, Any]) -> dict[str, Any]:
        sat = predict.CATALOG.get(p["norad"])
        dl = predict.downlink(p["norad"], p.get("downlink"))
        if not sat or not dl:
            raise SchedulerError(f"Starlab does not know the downlink of NORAD {p['norad']}; book it manually", 400)
        freq = int(dl["hz"])
        try:
            radio = default_radio(rx, freq, bandwidth_hint_hz=dl["bandwidth_hz"])
        except ValueError as exc:
            raise SchedulerError(str(exc), 400) from exc
        aos, los = dt.datetime.fromisoformat(p["aos"]), dt.datetime.fromisoformat(p["los"])
        several = len(sat.get("downlinks") or []) > 1
        aprs = dl["pipeline"] == "aprs"            # APRS is always decoded: the packets are the point of recording it
        return {
            "job_name": f"{sat['name'].lower().replace(' ', '-')}{'-' + dl['id'] if several else ''}-{aos:%Y%m%d-%H%M}",
            "target": {"type": "satellite", "name": sat["name"], "satellite": sat["name"], "norad_id": p["norad"]},
            "schedule": {"start": aos.isoformat(), "end": los.isoformat(), "timezone": "UTC",
                         "pre_roll_seconds": 30, "post_roll_seconds": 30},
            "receiver": {"receiver_id": rx.id, "driver": rx.driver, "serial": rx.serial, "exclusive_lock": True},
            "radio": radio,
            "recording": {"enabled": True, "engine": rx.engines()[0], "format": rx.formats()[0],
                          "minimum_free_space_gb": 1},
            "satdump": ({"enabled": True, "operation": "decode_after_recording", "pipeline": "aprs",
                         "input_type": "baseband", "baseband_format": rx.formats()[0],
                         "sample_rate_hz": radio["sample_rate_hz"], "frequency_hz": freq,
                         "output_directory": f"{self.s.settings.get('default_output_root', '/data/observatory/jobs')}"
                                             "/decoded/{job_name}"} if aprs
                        else {"enabled": False, "operation": "none"}),
            "postprocess": {"generate_waterfall": True, "generate_spectrum": True, "decode": aprs, "retain_raw_iq": True,
                            "checksum": True, "record_metadata": True},
            "conflict_policy": {"on_receiver_conflict": "reject", "on_storage_warning": "reject",
                                "on_missing_device": "reject"},
            "metadata": {"booked_from": "predicted pass", "max_elevation_deg": p["max_elevation_deg"], "norad": p["norad"],
                         "downlink": {k: dl[k] for k in ("id", "hz", "mode")},
                         "passes": [{"name": sat["name"], "rise": p["aos"], "set": p["los"],
                                     "max_elevation_deg": p["max_elevation_deg"]}]},
        }

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


def job_downlink(spec: dict[str, Any], norad: int) -> str | None:
    """Which catalog downlink a recording tunes to: the one it was booked for, else the one its target frequency
    falls in, else (no frequency on record: an older booking) the primary one."""
    named = ((spec.get("metadata") or {}).get("downlink") or {}).get("id")
    if named:
        return str(named)
    radio = spec.get("radio") or {}
    hz = radio.get("target_frequency_hz") or radio.get("center_frequency_hz")
    if hz:
        d = predict.downlink_for(norad, hz)
        return d["id"] if d else None
    d = predict.downlink(norad, None)
    return d["id"] if d else None
