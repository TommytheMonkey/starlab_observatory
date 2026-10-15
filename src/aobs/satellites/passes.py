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
        catalog = [{"norad": n, **c, "selected": n in selected} for n, c in predict.CATALOG.items()]
        for n in selected:
            if n not in predict.CATALOG:
                e = elements.get(n)
                catalog.append({"norad": n, "name": e.name if e else f"NORAD {n}", "downlink_hz": None, "mode": None,
                                "selected": True, "custom": True})
        return {"catalog": catalog, "selected": selected, "min_elevation_deg": sats.get("min_elevation_deg", 20),
                "refresh_online": bool(sats.get("refresh_online")),
                "elements": predict.element_status(elements, selected, self.s.clock())}

    def refresh(self) -> dict[str, Any]:
        sats = self._doc("satellites")
        if not sats.get("refresh_online"):
            raise SchedulerError("downloading orbital elements is off (Settings → Satellites → Download current "
                                 "orbital elements)", 409)
        return predict.refresh_tles(list(sats.get("selected") or []), self.s.config.data_root)

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
        for p in passes:
            p.update({k: v for k, v in predict.CATALOG.get(p["norad"], {}).items() if k in ("downlink_hz", "mode")})
            p["booked"] = self._booked(p)
        return {**out, "passes": passes, "location_set": True}

    def _booked(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        aos, los = dt.datetime.fromisoformat(p["aos"]), dt.datetime.fromisoformat(p["los"])
        slack = dt.timedelta(minutes=5)            # predicted times have sub-second precision; jobs are whole seconds
        out = []
        for j in self.s.jobs(start=aos - slack, end=los + slack, limit=100):
            starts = dt.datetime.fromisoformat(j["record_start"])
            if (j.get("target") or "").upper() == p["name"].upper() and abs((starts - aos).total_seconds()) < 120 \
                    and j["status"] not in ("cancelled",):
                out.append({"job_id": j["job_id"], "receiver_id": j["receiver_id"], "status": j["status"]})
        return out

    # ------------------------------------------------------------ booking
    def job_for_pass(self, rx: Any, p: dict[str, Any]) -> dict[str, Any]:
        sat = predict.CATALOG.get(p["norad"])
        if not sat or not sat.get("downlink_hz"):
            raise SchedulerError(f"Starlab does not know the downlink of NORAD {p['norad']}; book it manually", 400)
        freq = int(sat["downlink_hz"])
        radio: dict[str, Any] = {"target_frequency_hz": freq, "ppm_correction": 0, "bias_tee": rx.bias_tee_fixed is True,
                                 "direct_sampling": False, "offset_tuning": False, "iq_correction": False,
                                 "dc_offset_correction": False, "doppler_tracking": False, "agc": False,
                                 "gain_mode": "manual"}
        if rx.gain_model == "rtl":
            rate = 1_024_000
            radio.update(sample_rate_hz=rate, gain_db=33.8, bandwidth_hz=None)
        elif rx.gain_model == "sdrplay":
            rate = 2_000_000
            radio.update(sample_rate_hz=rate, gain_db=None, bandwidth_hz=600_000, gain_elements={"IFGR": 40, "RFGR": 4})
        else:
            caps = rx.caps or {}
            rates = sorted(caps.get("sample_rates_hz") or [lo for lo, *_ in caps.get("sample_rate_ranges_hz") or []])
            if not rates:
                raise SchedulerError(f"{rx.id} has not been identified, so its sample rates are unknown", 400)
            rate = int(next((r for r in rates if r >= 4 * (sat.get("bandwidth_hz") or 0)), rates[-1]))
            radio.update(sample_rate_hz=rate, gain_db=None, bandwidth_hz=None,
                         gain_elements={n: round((g["min"] + g["max"]) / 2) for n, g in (caps.get("gains") or {}).items()})
        radio["center_frequency_hz"] = freq - rate // 8          # keeps the receiver's DC spike off the signal
        aos, los = dt.datetime.fromisoformat(p["aos"]), dt.datetime.fromisoformat(p["los"])
        return {
            "job_name": f"{sat['name'].lower().replace(' ', '-')}-{aos:%Y%m%d-%H%M}",
            "target": {"type": "satellite", "name": sat["name"], "satellite": sat["name"]},
            "schedule": {"start": aos.isoformat(), "end": los.isoformat(), "timezone": "UTC",
                         "pre_roll_seconds": 30, "post_roll_seconds": 30},
            "receiver": {"receiver_id": rx.id, "driver": rx.driver, "serial": rx.serial, "exclusive_lock": True},
            "radio": radio,
            "recording": {"enabled": True, "engine": rx.engines()[0], "format": rx.formats()[0],
                          "minimum_free_space_gb": 1},
            "satdump": {"enabled": False, "operation": "none"},
            "postprocess": {"generate_waterfall": True, "generate_spectrum": True, "decode": False, "retain_raw_iq": True,
                            "checksum": True, "record_metadata": True},
            "conflict_policy": {"on_receiver_conflict": "reject", "on_storage_warning": "reject",
                                "on_missing_device": "reject"},
            "metadata": {"booked_from": "predicted pass", "max_elevation_deg": p["max_elevation_deg"],
                         "passes": [{"name": sat["name"], "rise": p["aos"], "set": p["los"],
                                     "max_elevation_deg": p["max_elevation_deg"]}]},
        }

    def schedule(self, norad: int, aos: str, receiver_id: str, actor: str, dry_run: bool = False) -> dict[str, Any]:
        rx = self.s.receivers.get(receiver_id)
        if rx is None:
            raise SchedulerError(f"no receiver {receiver_id}", 404)
        found = self.predicted(72)
        want = dt.datetime.fromisoformat(aos)
        p = next((x for x in found["passes"] if x["norad"] == int(norad)
                  and abs((dt.datetime.fromisoformat(x["aos"]) - want).total_seconds()) < 60), None)
        if p is None:
            raise SchedulerError("that pass is not in the current prediction (it may have started already); reload", 409)
        res = self.s.submit([self.job_for_pass(rx, p)], "all_or_nothing", f"web:{actor}", dry_run=dry_run)
        r0 = res["results"][0]
        if not r0.get("ok"):
            raise SchedulerError("Not booked: " + "; ".join(r0.get("errors") or ["refused"]), 409)
        return {"ok": True, "job_id": r0.get("job_id"), "warnings": r0.get("warnings", []), "dry_run": dry_run,
                "simulated": rx.simulated}
