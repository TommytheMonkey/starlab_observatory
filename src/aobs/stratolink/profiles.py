"""Tracker hardware and configuration profiles.

A profile is a starting point, not a recommendation for every flight: a set of Meshtastic settings by config path
("position.position_broadcast_secs", "lora.hop_limit", "channel.position_precision", ...) plus per-hardware overrides.
Profiles live as JSON in <data_dir>/profiles/ and are editable from the page; the three defaults below are written
there once, and never over an edited file. `resolve()` merges a profile with its hardware override, validates it against
the hardware, and estimates airtime and power, before the provisioning tool (deploy/stratolink/tracker/) applies it.

Hardware entries say what is known and what is not: power figures marked "nominal" are datasheet-level estimates that
were not measured on Stratolink hardware, and the estimate says so.
"""

from __future__ import annotations

import copy
import json
import math
import pathlib
import re
from typing import Any

# ------------------------------------------------------------------ hardware
HARDWARE: dict[str, dict[str, Any]] = {
    "heltec_v4": {
        "name": "Heltec WiFi LoRa 32 V4", "status": "supported",
        "hw_models": ["HELTEC_V4", "HELTEC_V4_R8"], "mcu": "ESP32-S3", "radio": "SX1262",
        "usb_ids": ["303a:1001"],          # ESP32-S3 native USB (CDC); identity is confirmed from the firmware's hw_model
        "gps": "L76K (on the V4 GNSS connector)", "max_tx_dbm": 28, "min_firmware": "2.7.0",
        "firmware_target": "heltec-v4", "flasher": "https://flasher.meshtastic.org/",
        "power": {"source": "nominal (not measured on Stratolink hardware)", "awake_ma": 45, "light_sleep_ma": 12,
                  "gps_ma": 25, "tx_ma": {"10": 60, "17": 110, "20": 140, "22": 170, "27": 400, "28": 450},
                  "rx_ma": 8},
        "notes": ["The L76K altitude limit above 18 km is not documented and Meshtastic exposes no airborne dynamic "
                  "model for it: verify GPS output above 18 km (or bench-test) before relying on it at float.",
                  "ESP32 power saving (light sleep) is applied by the firmware only on battery and only in some roles; "
                  "measure real consumption with a USB power meter and override the power figures."],
    },
    "rak4631": {
        "name": "RAKwireless WisBlock RAK4631 (+ RAK12500 GNSS)", "status": "planned",
        "hw_models": ["RAK4631"], "mcu": "nRF52840", "radio": "SX1262",
        "usb_ids": ["239a:8029", "239a:0029"], "gps": "RAK12500 (u-blox ZOE-M8Q) or RAK1910", "max_tx_dbm": 22,
        "min_firmware": "2.5.0", "firmware_target": "rak4631", "flasher": "https://flasher.meshtastic.org/",
        "power": {"source": "nominal (not measured on Stratolink hardware)", "awake_ma": 4, "light_sleep_ma": 2,
                  "gps_ma": 30, "tx_ma": {"10": 40, "17": 90, "20": 110, "22": 125}, "rx_ma": 6},
        "notes": ["Not yet provisioned or flown by Stratolink: the profile mapping is the same, but verify each setting "
                  "reads back and bench-test before use.",
                  "u-blox M8 modules need the airborne dynamic model above 12 km; Meshtastic does not set it."],
    },
    "generic": {
        "name": "Other Meshtastic board", "status": "experimental", "hw_models": [], "usb_ids": [], "mcu": "?",
        "radio": "?", "gps": "?", "max_tx_dbm": 22, "min_firmware": "2.5.0", "firmware_target": None,
        "flasher": "https://flasher.meshtastic.org/",
        "power": {"source": "unknown: set figures for this board", "awake_ma": None, "light_sleep_ma": None,
                  "gps_ma": None, "tx_ma": {}, "rx_ma": None},
        "notes": ["No Stratolink-specific knowledge of this board. Add an entry to profiles.HARDWARE to support it."],
    },
}


def hardware_for_model(hw_model: str | None) -> str | None:
    for key, h in HARDWARE.items():
        if hw_model and hw_model in h["hw_models"]:
            return key
    return None


# ------------------------------------------------------------------ profiles
FLAGS = {"ALTITUDE": 1, "ALTITUDE_MSL": 2, "GEOIDAL_SEPARATION": 4, "DOP": 8, "HVDOP": 16, "SATINVIEW": 32,
         "SEQ_NO": 64, "TIMESTAMP": 128, "HEADING": 256, "SPEED": 512}
TRACK_FLAGS = sum(FLAGS[f] for f in ("ALTITUDE", "ALTITUDE_MSL", "DOP", "SATINVIEW", "SEQ_NO", "TIMESTAMP", "HEADING",
                                     "SPEED"))

COMMON: dict[str, Any] = {
    "lora.region": "US", "lora.use_preset": True, "lora.modem_preset": "LONG_FAST", "lora.tx_enabled": True,
    "lora.ignore_mqtt": True, "lora.config_ok_to_mqtt": False,
    "device.role": "TRACKER", "device.rebroadcast_mode": "NONE",
    "position.gps_mode": "ENABLED", "position.fixed_position": False, "position.position_broadcast_smart_enabled": False,
    "position.position_flags": TRACK_FLAGS,
    "telemetry.device_telemetry_enabled": True, "network.wifi_enabled": False,
}

DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "ground-test": {
        "name": "Ground Test", "description": "Bench and field checks: frequent fixes, full precision, Bluetooth on "
                                              "for the phone app.",
        "settings": {**COMMON, "position.position_broadcast_secs": 60, "position.gps_update_interval": 30,
                     "telemetry.device_update_interval": 300, "channel.position_precision": 32,
                     "lora.hop_limit": 3, "lora.tx_power": 0, "device.node_info_broadcast_secs": 3600,
                     "bluetooth.enabled": True, "power.is_power_saving": False, "display.screen_on_secs": 60},
        "overrides": {},
    },
    "high-altitude": {
        "name": "High-Altitude Flight", "description": "Latex-balloon flight (hours). Faster position reports, no "
                                                       "rebroadcasting (a node at altitude hears a whole region), "
                                                       "Bluetooth and screen off.",
        "settings": {**COMMON, "position.position_broadcast_secs": 60, "position.gps_update_interval": 30,
                     "telemetry.device_update_interval": 300, "channel.position_precision": 32,
                     "lora.hop_limit": 2, "lora.tx_power": 22, "device.node_info_broadcast_secs": 10800,
                     "bluetooth.enabled": False, "power.is_power_saving": False, "display.screen_on_secs": 10},
        "overrides": {"rak4631": {"lora.tx_power": 22}},
    },
    "pico": {
        "name": "Long-Duration Pico Balloon", "description": "Days to weeks on a small battery/solar: long intervals, "
                                                            "power saving, every peripheral off.",
        "settings": {**COMMON, "position.position_broadcast_secs": 900, "position.gps_update_interval": 600,
                     "telemetry.device_update_interval": 1800, "channel.position_precision": 32,
                     "lora.hop_limit": 1, "lora.tx_power": 17, "device.node_info_broadcast_secs": 43200,
                     "bluetooth.enabled": False, "power.is_power_saving": True, "power.ls_secs": 300,
                     "power.min_wake_secs": 10, "display.screen_on_secs": 1},
        "overrides": {},
    },
}

# What each setting may be, for validation (type, low, high) or choices
RULES: dict[str, Any] = {
    "lora.region": ("US",), "lora.modem_preset": ("LONG_FAST",), "lora.use_preset": bool, "lora.tx_enabled": bool,
    "lora.hop_limit": (int, 0, 7), "lora.tx_power": (int, 0, 30), "lora.ignore_mqtt": bool,
    "lora.config_ok_to_mqtt": bool,
    "device.role": ("TRACKER", "TAK_TRACKER", "SENSOR", "CLIENT", "CLIENT_MUTE"),
    "device.rebroadcast_mode": ("NONE", "LOCAL_ONLY", "KNOWN_ONLY", "CORE_PORTNUMS_ONLY", "ALL"),
    "device.node_info_broadcast_secs": (int, 3600, 604800),
    "position.gps_mode": ("ENABLED",), "position.fixed_position": bool,
    "position.position_broadcast_smart_enabled": bool, "position.position_flags": (int, 0, 1023),
    "position.position_broadcast_secs": (int, 30, 86400), "position.gps_update_interval": (int, 5, 86400),
    "position.gps_attempt_time": (int, 0, 3600),
    "telemetry.device_update_interval": (int, 60, 86400), "telemetry.device_telemetry_enabled": bool,
    "telemetry.environment_update_interval": (int, 0, 86400), "telemetry.environment_measurement_enabled": bool,
    "channel.position_precision": (int, 0, 32), "network.wifi_enabled": bool, "bluetooth.enabled": bool,
    "power.is_power_saving": bool, "power.ls_secs": (int, 0, 86400), "power.min_wake_secs": (int, 0, 3600),
    "display.screen_on_secs": (int, 1, 86400),
    "lora.channel_num": (int, 0, 104),
    "position.broadcast_smart_minimum_interval_secs": (int, 0, 86400),
    "position.broadcast_smart_minimum_distance": (int, 0, 100000),
}

# ---- what the firmware does with these values (Meshtastic 2.7.26.54e0d8d, src/mesh/NodeDB.cpp lines 333-365 and
# src/mesh/Default.h lines 16-22; see docs/stratolink/reporting-intervals.md). At every boot, if the channel that
# carries positions (the first one with position_precision != 0) is a "default" channel - PSK AQ== and the preset's
# own name, e.g. LongFast - position_broadcast_secs is raised to 3600 and broadcast_smart_minimum_interval_secs to
# 300; and if ANY channel is a default channel (with a preset, on the preset's default frequency slot, no frequency
# override) every telemetry interval is raised to 1800. Roles ROUTER/ROUTER_LATE get 43200 instead. Values below
# the minimum are accepted when written and coerced at the next boot, which is why they appear to "revert".
FIRMWARE_RULES_VERSION = "2.7.26.54e0d8d"
DEFAULT_CHANNEL_MIN = {"position.position_broadcast_secs": 3600,
                       "position.broadcast_smart_minimum_interval_secs": 300}
ANY_DEFAULT_CHANNEL_MIN = {"telemetry.device_update_interval": 1800,
                           "telemetry.environment_update_interval": 1800}
US_LONG_FAST_SLOT = 20            # 906.875 MHz: LONG_FAST's default slot in the US (what the ground stations hear)
CHANNEL_MODES = ("public", "private")


def firmware_effect(settings: dict[str, Any], channel_mode: str = "public",
                    keep_public_secondary: bool = False) -> dict[str, dict[str, Any]]:
    """Predicted persisted values: {path: {requested, persisted, rule}} for every setting the firmware changes."""
    router = settings.get("device.role") in ("ROUTER", "ROUTER_LATE")
    out: dict[str, dict[str, Any]] = {}
    position_on_default = channel_mode == "public"
    any_default = channel_mode == "public" or keep_public_secondary
    rules = []
    if position_on_default:
        rules.append((DEFAULT_CHANNEL_MIN, "positions go out on the public default channel (LongFast, key AQ==)"))
    if any_default:
        rules.append((ANY_DEFAULT_CHANNEL_MIN, "a public default channel is configured"))
    for mins, why in rules:
        for k, lo in mins.items():
            if router and k != "position.broadcast_smart_minimum_interval_secs":
                lo = 43200
            v = settings.get(k)
            if v is not None and 0 < v < lo:
                out[k] = {"requested": v, "persisted": lo,
                          "rule": f"firmware {FIRMWARE_RULES_VERSION} raises it to {lo} s at boot: {why}"}
    return out


def channel_settings(settings: dict[str, Any], channel_mode: str) -> dict[str, Any]:
    """A private primary channel moves the frequency unless the slot is pinned: pin it to 906.875 MHz."""
    out = dict(settings)
    if channel_mode == "private":
        out["lora.channel_num"] = US_LONG_FAST_SLOT
    return out

# Settings Stratolink depends on: the ground station listens on exactly this
REQUIRED = {"lora.region": "US", "lora.modem_preset": "LONG_FAST", "lora.use_preset": True, "lora.tx_enabled": True,
            "position.gps_mode": "ENABLED"}
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def validate(settings: dict[str, Any], hardware: str, channel_mode: str = "public",
             keep_public_secondary: bool = False) -> tuple[list[str], list[str]]:
    """(errors, warnings). Errors block deployment; warnings are shown and must be acknowledged by reading them."""
    errors, warnings = [], []
    if channel_mode not in CHANNEL_MODES:
        return [f"channel mode must be one of {CHANNEL_MODES}"], []
    cn = settings.get("lora.channel_num")
    if cn not in (None, 0, US_LONG_FAST_SLOT):
        errors.append(f"lora.channel_num {cn} moves the radio off 906.875 MHz (slot {US_LONG_FAST_SLOT}), where the "
                      "ground stations listen")
    if channel_mode == "private" and cn != US_LONG_FAST_SLOT:
        errors.append(f"a private channel needs lora.channel_num {US_LONG_FAST_SLOT}: otherwise its name moves the "
                      "frequency off 906.875 MHz")
    for k, e in firmware_effect(settings, channel_mode, keep_public_secondary).items():
        warnings.append(f"{k}: requested {e['requested']} s will persist as {e['persisted']} s ({e['rule']})")
    if settings.get("position.position_broadcast_smart_enabled"):
        warnings.append("smart position broadcasting measures horizontal distance only: a balloon climbing straight "
                        "up does not trigger it; the regular interval still applies")
    hw = HARDWARE.get(hardware)
    if hw is None:
        return [f"unknown hardware {hardware!r} (known: {', '.join(HARDWARE)})"], []
    for k, v in settings.items():
        rule = RULES.get(k)
        if rule is None:
            errors.append(f"{k}: not a setting Stratolink knows how to apply")
        elif rule is bool:
            if not isinstance(v, bool):
                errors.append(f"{k} must be true or false")
        elif isinstance(rule, tuple) and rule and rule[0] is int:
            if isinstance(v, bool) or not isinstance(v, int) or not rule[1] <= v <= rule[2]:
                errors.append(f"{k} must be a whole number {rule[1]}-{rule[2]}, not {v!r}")
        elif v not in rule:
            errors.append(f"{k} must be one of {', '.join(map(str, rule))}, not {v!r}")
    for k, v in REQUIRED.items():
        if settings.get(k) != v:
            errors.append(f"{k} must be {v!r}: the ground stations listen on US915 LONG_FAST only")
    if errors:
        return errors, warnings
    pos, gps = settings.get("position.position_broadcast_secs"), settings.get("position.gps_update_interval")
    if pos and gps and gps > pos:
        warnings.append(f"GPS updates every {gps} s but positions go out every {pos} s: some reports will repeat an "
                        "old fix")
    tx = settings.get("lora.tx_power") or 0
    if tx > hw["max_tx_dbm"]:
        errors.append(f"lora.tx_power {tx} dBm is above the {hw['name']}'s {hw['max_tx_dbm']} dBm")
    if tx == 0:
        warnings.append("lora.tx_power 0 means the firmware default (the region's legal maximum the board supports)")
    if (settings.get("lora.hop_limit") or 0) > 3:
        warnings.append("hop_limit above 3 makes every packet travel far across the public mesh")
    rb = settings.get("device.rebroadcast_mode")
    if rb == "NONE" and settings.get("device.role") not in ("TRACKER", "TAK_TRACKER", "SENSOR"):
        errors.append("rebroadcast_mode NONE is only accepted by the TRACKER, TAK_TRACKER and SENSOR roles")
    if rb in ("ALL", "LOCAL_ONLY") and settings.get("device.role") in ("TRACKER", "TAK_TRACKER"):
        warnings.append("an airborne tracker that rebroadcasts relays other people's traffic over a very wide area")
    if pos and pos < 60:
        warnings.append(f"positions every {pos} s on the shared LongFast channel use a lot of airtime")
    prec = settings.get("channel.position_precision")
    if prec is not None and prec < 32:
        warnings.append(f"position precision {prec} bits rounds positions (32 = full precision)")
    if prec == 0:
        errors.append("position precision 0 sends no position at all")
    if hw["status"] != "supported":
        warnings.append(f"{hw['name']} is {hw['status']} in Stratolink: verify on the bench before flight")
    return errors, warnings


# ------------------------------------------------------------------ airtime and power
def airtime_s(payload_bytes: int, sf: int = 11, bw_hz: int = 250_000, cr: int = 1, preamble: int = 16,
              explicit_header: bool = True, crc: bool = True) -> float:
    """LoRa time on air (Semtech AN1200.13). LONG_FAST: SF11, 250 kHz, 4/5, 16-symbol preamble."""
    tsym = (2 ** sf) / bw_hz
    de = 1 if tsym > 0.016 else 0
    h = 0 if explicit_header else 1
    num = 8 * payload_bytes - 4 * sf + 28 + 16 * int(crc) - 20 * h
    n = 8 + max(math.ceil(num / (4 * (sf - 2 * de))) * (cr + 4), 0)
    return (preamble + 4.25) * tsym + n * tsym


FRAME_BYTES = {"position": 16 + 39, "telemetry": 16 + 27, "nodeinfo": 16 + 60}   # header + typical Data payload


def estimate(settings: dict[str, Any], hardware: str) -> dict[str, Any]:
    hw = HARDWARE[hardware]
    p = hw["power"]
    day = 86400
    per_day = {"position": day / max(1, settings.get("position.position_broadcast_secs") or day),
               "telemetry": day / max(1, settings.get("telemetry.device_update_interval") or day)
               if settings.get("telemetry.device_telemetry_enabled", True) else 0,
               "nodeinfo": day / max(1, settings.get("device.node_info_broadcast_secs") or day)}
    air = {k: n * airtime_s(FRAME_BYTES[k]) for k, n in per_day.items()}
    tx_s = sum(air.values())
    out: dict[str, Any] = {"packets_per_day": {k: round(v, 1) for k, v in per_day.items()},
                           "airtime_s_per_packet": {k: round(airtime_s(b), 3) for k, b in FRAME_BYTES.items()},
                           "airtime_s_per_day": round(tx_s, 1), "duty_cycle_pct": round(100 * tx_s / day, 3),
                           "power_source": p["source"], "mah_per_day": None, "assumptions": []}
    tx_dbm = settings.get("lora.tx_power") or hw["max_tx_dbm"]
    table = {int(k): v for k, v in (p.get("tx_ma") or {}).items()}
    if p.get("awake_ma") is None or not table:
        out["assumptions"].append("no power figures for this hardware: no consumption estimate")
        return out
    tx_ma = table[min(table, key=lambda k: abs(k - tx_dbm))]
    base = p["light_sleep_ma"] if settings.get("power.is_power_saving") else p["awake_ma"]
    gps_ma = p["gps_ma"] if settings.get("position.gps_mode") == "ENABLED" else 0
    # GPS on continuously when updates are frequent; duty-cycled (~60 s to reacquire per update) when sparse
    gps_int = settings.get("position.gps_update_interval") or 30
    gps_duty = 1.0 if gps_int <= 120 else min(1.0, 60 / gps_int)
    mah = (base * 24) + (gps_ma * gps_duty * 24) + (tx_ma * tx_s / 3600)
    out.update({"mah_per_day": round(mah), "tx_ma": tx_ma, "base_ma": base, "gps_ma": gps_ma,
                "gps_duty": round(gps_duty, 2)})
    out["assumptions"] += [f"base {base} mA ({'power saving' if settings.get('power.is_power_saving') else 'awake'}),"
                           f" GPS {gps_ma} mA x {gps_duty:.0%}, TX {tx_ma} mA at ~{tx_dbm} dBm",
                           "Bluetooth, screen and sensors not included", p["source"]]
    return out


# ------------------------------------------------------------------ storage
class Profiles:
    def __init__(self, root: pathlib.Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        for slug, prof in DEFAULT_PROFILES.items():
            path = root / f"{slug}.json"
            if not path.exists():
                path.write_text(json.dumps(prof, indent=2) + "\n")

    def list(self) -> dict[str, dict[str, Any]]:
        out = {}
        for p in sorted(self.root.glob("*.json")):
            try:
                out[p.stem] = json.loads(p.read_text())
            except ValueError:
                continue
        return out

    def get(self, slug: str) -> dict[str, Any]:
        if not SLUG.fullmatch(slug):
            raise ValueError("a profile id is lower-case letters, digits and dashes")
        path = self.root / f"{slug}.json"
        if not path.exists():
            raise KeyError(slug)
        return json.loads(path.read_text())

    def save(self, slug: str, prof: dict[str, Any]) -> dict[str, Any]:
        if not SLUG.fullmatch(slug):
            raise ValueError("a profile id is lower-case letters, digits and dashes")
        if not isinstance(prof.get("settings"), dict) or not str(prof.get("name", "")).strip():
            raise ValueError("a profile needs a name and settings")
        overrides = prof.get("overrides") or {}
        for hw, o in overrides.items():
            if hw not in HARDWARE or not isinstance(o, dict):
                raise ValueError(f"override for unknown hardware {hw!r}")
        for hw in ("heltec_v4", *overrides):
            errs, _ = validate({**prof["settings"], **overrides.get(hw, {})}, hw)
            if errs:
                raise ValueError(f"{hw}: " + "; ".join(errs))
        clean = {"name": str(prof["name"]).strip()[:60], "description": str(prof.get("description", ""))[:400],
                 "settings": prof["settings"], "overrides": overrides}
        tmp = self.root / f".{slug}.tmp"
        tmp.write_text(json.dumps(clean, indent=2) + "\n")
        tmp.replace(self.root / f"{slug}.json")
        return clean

    def reset(self, slug: str) -> dict[str, Any]:
        if slug not in DEFAULT_PROFILES:
            raise ValueError(f"{slug} is not a built-in profile")
        (self.root / f"{slug}.json").write_text(json.dumps(DEFAULT_PROFILES[slug], indent=2) + "\n")
        return DEFAULT_PROFILES[slug]


def resolve(prof: dict[str, Any], hardware: str, extra: dict[str, Any] | None = None, channel_mode: str = "public",
            keep_public_secondary: bool = False) -> dict[str, Any]:
    settings = channel_settings({**copy.deepcopy(prof["settings"]), **(prof.get("overrides") or {}).get(hardware, {}),
                                 **(extra or {})}, channel_mode)
    errors, warnings = (validate(settings, hardware, channel_mode, keep_public_secondary) if hardware in HARDWARE
                        else ([f"unknown hardware {hardware}"], []))
    effect = firmware_effect(settings, channel_mode, keep_public_secondary)
    persisted = {**settings, **{k: v["persisted"] for k, v in effect.items()}}
    return {"settings": settings, "errors": errors, "warnings": warnings, "channel_mode": channel_mode,
            "firmware_effect": effect, "persisted": persisted, "firmware_rules_version": FIRMWARE_RULES_VERSION,
            "estimate": estimate(persisted, hardware) if not errors else None, "hardware": HARDWARE.get(hardware)}
