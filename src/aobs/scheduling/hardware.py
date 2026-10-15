"""Receiver profiles and per-hardware validation of radio settings.

Every receiver is identified by its physical serial. RTL-SDR and SDRplay take
different gain controls, sample rates and stream formats; settings are checked
against the selected driver and never translated from one backend to another.
Other SoapySDR receivers (Airspy, generic) are checked against the capabilities their
installed driver reported for the actual device (inventory.probe), never against a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..inventory.drivers import DRIVERS

R820T_GAINS_DB = (0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28.0,
                  29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6)
RTL_FREQ_HZ = (24_000_000, 1_766_000_000)          # R820T tuner, no direct sampling
RTL_RATE_BANDS = ((225_001, 300_000), (900_001, 2_560_000))  # above 2.56 MS/s the RTL2832U drops samples
SDRPLAY_FREQ_HZ = (1_000, 2_000_000_000)
SDRPLAY_RATE_HZ = (2_000_000, 10_660_000)
SDRPLAY_DECIMATED_RATES_HZ = (62_500, 96_000, 125_000, 192_000, 250_000, 384_000, 500_000, 768_000, 1_000_000)
# Driver settings (SoapySDRPlay3 getSettingInfo, read from the RSP1B 2026-10-13) a job may set explicitly.
SDRPLAY_DEVICE_SETTINGS = {"rfnotch_ctrl": "FM broadcast notch filter", "dabnotch_ctrl": "DAB notch filter"}
SDRPLAY_BANDWIDTHS_HZ = (200_000, 300_000, 600_000, 1_536_000, 5_000_000, 6_000_000, 7_000_000, 8_000_000)
SDRPLAY_IFGR_DB = (20, 59)
BYTES_PER_SAMPLE = {"cu8": 2, "cs8": 2, "cs16": 4, "cf32": 8}

ENGINES = {name: d.engines for name, d in DRIVERS.items()}
FORMATS = {name: d.formats for name, d in DRIVERS.items()}


def rsp1b_rfgr_max(freq_hz: int) -> int:
    """Highest LNA state (RFGR) the RSP1A/RSP1B accepts at this frequency."""
    if freq_hz < 60_000_000:
        return 6
    if freq_hz < 1_000_000_000:
        return 9
    return 8


@dataclass(frozen=True)
class Receiver:
    id: str
    driver: str                      # rtlsdr | sdrplay | airspy | soapy (inventory.drivers)
    serial: str
    hardware: str
    antenna: str
    signal_path: str
    mode: str                        # legacy | shadow | broker
    bias_tee_supported: bool = False
    bias_tee_fixed: bool | None = None       # True/False: hard-wired (e.g. EEPROM); jobs must state that value
    legacy_unit: str = ""
    legacy_kind: str = ""
    legacy_output: str = ""
    publish_legacy_to_calendar: bool = True
    shared_with: tuple[str, ...] = ()
    notes: str = ""
    auto: dict[str, Any] = field(default_factory=dict)
    amplifier: str = ""                      # e.g. "Nooelec LaNA"; "" = none
    amplifier_power: str = ""                # "external" | "bias_tee" | ""
    filters: tuple[str, ...] = ()
    # inventory (schema 2)
    name: str = ""
    origin: str = "toml"                     # toml (owned by observatory.toml) | ui
    readiness: str = "untested"              # untested | verified | failed: set only by hardware evidence
    in_service: bool = False                 # imported from the TOML while already scheduling; keeps doing so
    soapy_driver: str = ""                   # SoapySDR module for driver "soapy"
    caps: dict[str, Any] = field(default_factory=dict)   # driver-reported capabilities (probe), elements drivers
    identity: str = "serial"                 # serial | usb_path
    usb_path: str = ""

    @property
    def switched(self) -> bool:
        return self.antenna == "ANTSW-01"

    @property
    def gain_model(self) -> str:
        return DRIVERS[self.driver].gain_model

    @property
    def soapy(self) -> str:
        return self.soapy_driver or DRIVERS[self.driver].soapy or ""

    @property
    def simulated(self) -> bool:
        return self.driver == "sim"

    @property
    def schedulable(self) -> bool:
        """Recording jobs (not tests) need hardware evidence, or an import of a receiver already in service.
        Simulated receivers may always be booked: they only ever produce labelled synthetic recordings."""
        return self.readiness == "verified" or self.in_service or self.simulated

    def engines(self) -> tuple[str, ...]:
        return ENGINES[self.driver]

    def formats(self) -> tuple[str, ...]:
        if self.gain_model == "elements":
            return tuple(f for f in FORMATS[self.driver] if f in (self.caps.get("formats") or ()))
        return FORMATS[self.driver]

    def test_kinds(self) -> tuple[str, ...]:
        return DRIVERS[self.driver].test_kinds

    def capabilities(self, switch_ports: dict[int, dict[str, Any]]) -> dict[str, Any]:
        d: dict[str, Any] = {
            "receiver_id": self.id, "serial": self.serial, "driver": self.driver, "hardware": self.hardware,
            "mode": self.mode, "engines": list(self.engines()), "formats": list(self.formats()),
            "gain_model": self.gain_model, "test_kinds": list(self.test_kinds()), "readiness": self.readiness,
            "signal_path": self.signal_path, "shared_with": list(self.shared_with),
            "rf_chain": {"antenna": self.antenna, "amplifier": self.amplifier or None,
                         "amplifier_power": self.amplifier_power or None, "filters": list(self.filters)},
            "bias_tee": {"hardware": self.bias_tee_supported, "fixed": self.bias_tee_fixed,
                         "schedulable_values": [self.bias_tee_fixed] if self.bias_tee_fixed is not None else
                         ([False, True] if self.bias_tee_supported and self.driver == "sdrplay" else [False])},
            "preemptible_automatic_jobs": self.mode == "broker",
        }
        if self.switched:
            d["antennas"] = [{"antenna": p["antenna"], "port": n, "label": p.get("label", "")}
                             for n, p in sorted(switch_ports.items())]
        else:
            d["antennas"] = [{"antenna": self.antenna}]
        if self.driver == "rtlsdr":
            d.update({
                "frequency_hz": list(RTL_FREQ_HZ),
                "sample_rate_hz_ranges": [list(b) for b in RTL_RATE_BANDS],
                "gain_modes": ["manual", "auto"], "gain_db_values": list(R820T_GAINS_DB),
                "bandwidth_hz": "null only (rtl_sdr has no IF filter control; the passband equals the sample rate)",
                "ppm_correction": [-200, 200],
                "unsupported_true": ["agc", "direct_sampling", "offset_tuning", "iq_correction",
                                     "dc_offset_correction", "doppler_tracking"],
            })
        elif self.gain_model == "elements":
            c = self.caps
            d.update({
                "frequency_hz": c.get("frequency_hz"), "sample_rate_hz_values": c.get("sample_rates_hz", []),
                "sample_rate_hz_ranges": c.get("sample_rate_ranges_hz", []), "gain_modes": ["manual"],
                "gain_elements": c.get("gains", {}), "bandwidth_hz_values": c.get("bandwidths_hz", []),
                "capabilities_source": c.get("source", "not probed"),
                "unsupported_true": ["agc", "bias_tee", "direct_sampling", "offset_tuning", "iq_correction",
                                     "dc_offset_correction", "doppler_tracking"],
            })
        else:
            d.update({
                "frequency_hz": list(SDRPLAY_FREQ_HZ),
                "sample_rate_hz_range": list(SDRPLAY_RATE_HZ),
                "sample_rate_hz_values": list(SDRPLAY_DECIMATED_RATES_HZ),
                "device_settings": {k: f"{v} (true/false)" for k, v in SDRPLAY_DEVICE_SETTINGS.items()},
                "bandwidth_hz_values": list(SDRPLAY_BANDWIDTHS_HZ),
                "gain_modes": ["manual", "auto"],
                "gain": "radio.gain_elements = {\"IFGR\": 20-59 dB reduction, \"RFGR\": LNA state}; gain_db must be null. "
                        "gain_mode auto = IF AGC (agc true) and still needs RFGR.",
                "rfgr_max": {"below_60_MHz": 6, "60_MHz_to_1_GHz": 9, "1_to_2_GHz": 8},
                "iq_correction": "driver setting iqcorr_ctrl",
                "ppm_correction": [-200, 200],
                "supported_true": ["agc", "iq_correction", "dc_offset_correction", "bias_tee"],
                "unsupported_true": ["direct_sampling", "offset_tuning", "doppler_tracking"],
            })
        if self.notes:
            d["notes"] = self.notes
        return d


def receiver_from_config(rid: str, raw: dict[str, Any], **inventory: Any) -> Receiver:
    """One receiver from its settings (a TOML [scheduler.receivers.<id>] table, or an inventory row's config)."""
    if raw.get("driver") not in ENGINES:
        raise ValueError(f"{rid}: unknown driver {raw.get('driver')}")
    if raw.get("mode", "broker") not in ("legacy", "shadow", "broker"):
        raise ValueError(f"{rid}: mode must be legacy, shadow or broker")
    rx = Receiver(
        id=rid, driver=raw["driver"], serial=str(raw["serial"]), hardware=raw.get("hardware", ""),
        antenna=raw.get("antenna", ""), signal_path=raw.get("signal_path", ""), mode=raw.get("mode", "broker"),
        bias_tee_supported=bool(raw.get("bias_tee_supported", False)), legacy_unit=raw.get("legacy_unit", ""),
        bias_tee_fixed=raw.get("bias_tee_fixed"),
        legacy_kind=raw.get("legacy_kind", ""), legacy_output=raw.get("legacy_output", ""),
        publish_legacy_to_calendar=bool(raw.get("publish_legacy_to_calendar", True)),
        shared_with=tuple(raw.get("shared_with", ())), notes=raw.get("notes", ""), auto=dict(raw.get("auto", {})),
        amplifier=raw.get("amplifier", ""), amplifier_power=raw.get("amplifier_power", ""),
        filters=tuple(raw.get("filters", ())), **inventory)
    if rx.amplifier_power == "external" and rx.bias_tee_fixed is True:
        raise ValueError(f"{rid}: an externally powered amplifier must not also get bias-tee DC (bias_tee_fixed = true)")
    if rx.auto.get("enabled") and not DRIVERS[rx.driver].automatic:
        raise ValueError(f"{rid}: automatic recording is not implemented for {rx.driver} receivers")
    return rx


def load_receivers(config: Config) -> dict[str, Receiver]:
    """The receivers declared in observatory.toml (imported into the inventory; see inventory.store)."""
    out = {rid: receiver_from_config(rid, raw) for rid, raw in config.section("scheduler").get("receivers", {}).items()}
    serials = [r.serial for r in out.values()]
    if len(set(serials)) != len(serials):
        raise ValueError("two scheduler receivers share a serial")
    return out


def validate_radio(rx: Receiver, radio: dict[str, Any], engine: str, fmt: str, errors: list[str],
                   warnings: list[str]) -> None:
    """Check radio settings against the receiver's driver. Appends to errors/warnings; never rewrites values."""
    def need(key: str) -> Any:
        if radio.get(key) is None:
            errors.append(f"radio.{key} is required")
        return radio.get(key)

    if engine not in rx.engines():
        errors.append(f"recording.engine {engine!r} cannot drive {rx.id} ({rx.driver}); use one of {list(rx.engines())}")
    if fmt not in rx.formats():
        errors.append(f"recording.format {fmt!r} is not produced by {rx.driver} ({rx.id}); use one of {list(rx.formats())}")

    center, rate = need("center_frequency_hz"), need("sample_rate_hz")
    gain_mode = need("gain_mode")
    for flag in ("agc", "bias_tee"):
        if not isinstance(radio.get(flag), bool):
            errors.append(f"radio.{flag} must be explicitly true or false")
    for flag in ("direct_sampling", "offset_tuning", "doppler_tracking"):
        if radio.get(flag):
            errors.append(f"radio.{flag}=true is not supported by the scheduled recorder for {rx.driver}")
    ppm = radio.get("ppm_correction", 0)
    if not isinstance(ppm, int) or not -200 <= ppm <= 200:
        errors.append("radio.ppm_correction must be an integer between -200 and 200")
    if rx.bias_tee_fixed is not None and isinstance(radio.get("bias_tee"), bool):
        if radio["bias_tee"] != rx.bias_tee_fixed:
            errors.append(f"{rx.id}'s bias tee is permanently {'ON' if rx.bias_tee_fixed else 'OFF'} (hardware/EEPROM); "
                          f"set bias_tee {str(rx.bias_tee_fixed).lower()} so the request describes what will happen")
    elif radio.get("bias_tee") is True:
        if not rx.bias_tee_supported:
            errors.append(f"{rx.id} has no bias tee" + (f" control here; its {rx.amplifier} is externally powered, so "
                                                          "bias_tee must stay false" if rx.amplifier_power == "external" else ""))
        elif rx.amplifier_power == "external":
            warnings.append(f"{rx.id}'s {rx.amplifier} has its own power supply; bias-tee DC is not needed and puts "
                            "DC on the amplifier's output port")
        elif rx.driver == "rtlsdr":
            errors.append(f"{rx.id}'s bias tee cannot be controlled per serial on this host (rtl_biast is index-only)")
        elif rx.driver != "sdrplay":
            errors.append(f"bias tee control is not implemented for {rx.driver} receivers; set bias_tee false")
    if gain_mode not in (None, "manual", "auto"):
        errors.append("radio.gain_mode must be manual or auto")

    if rx.driver == "rtlsdr":
        if isinstance(center, int) and not RTL_FREQ_HZ[0] <= center <= RTL_FREQ_HZ[1]:
            errors.append(f"center_frequency_hz {center} is outside the R820T range {RTL_FREQ_HZ[0]}-{RTL_FREQ_HZ[1]} Hz")
        if isinstance(rate, int) and not any(lo <= rate <= hi for lo, hi in RTL_RATE_BANDS):
            errors.append(f"sample_rate_hz {rate} is not supported by RTL-SDR (225001-300000 or 900001-2560000)")
        if radio.get("bandwidth_hz") is not None:
            errors.append("radio.bandwidth_hz must be null for rtl_sdr (no IF filter control; the passband is the sample rate)")
        if radio.get("agc"):
            errors.append("radio.agc=true is not available through rtl_sdr; use gain_mode auto for tuner AGC")
        for flag in ("iq_correction", "dc_offset_correction"):
            if radio.get(flag):
                errors.append(f"radio.{flag}=true is not applied by rtl_sdr (raw capture); set it false "
                              "(SatDump can DC-block when decoding: satdump.additional_parameters.dc_block)")
        if radio.get("gain_elements"):
            errors.append("radio.gain_elements is SDRplay-only; RTL-SDR takes gain_db")
        if radio.get("device_settings"):
            errors.append("radio.device_settings is SDRplay-only")
        if gain_mode == "manual":
            g = radio.get("gain_db")
            if g is None:
                errors.append("gain_mode manual needs radio.gain_db")
            elif float(g) not in R820T_GAINS_DB:
                errors.append(f"gain_db {g} is not an R820T gain step; valid: {', '.join(map(str, R820T_GAINS_DB))}")
        elif gain_mode == "auto" and radio.get("gain_db") is not None:
            errors.append("gain_mode auto with a gain_db is contradictory; drop gain_db or use manual")
    elif rx.gain_model == "elements":
        _validate_elements(rx, radio, center, rate, gain_mode, errors)
    else:
        if isinstance(center, int) and not SDRPLAY_FREQ_HZ[0] <= center <= SDRPLAY_FREQ_HZ[1]:
            errors.append(f"center_frequency_hz {center} is outside the RSP1B range")
        if isinstance(rate, int) and not (SDRPLAY_RATE_HZ[0] <= rate <= SDRPLAY_RATE_HZ[1] or rate in SDRPLAY_DECIMATED_RATES_HZ):
            errors.append(f"sample_rate_hz {rate} is not an RSP1B rate: {SDRPLAY_RATE_HZ[0]}-{SDRPLAY_RATE_HZ[1]} "
                          f"or one of {list(SDRPLAY_DECIMATED_RATES_HZ)}")
        ds = radio.get("device_settings") or {}
        for k, v in ds.items():
            if k not in SDRPLAY_DEVICE_SETTINGS or not isinstance(v, bool):
                errors.append(f"radio.device_settings.{k} is not allowed; allowed (true/false): {sorted(SDRPLAY_DEVICE_SETTINGS)}")
        bw = radio.get("bandwidth_hz")
        if bw is None:
            errors.append(f"radio.bandwidth_hz is required for SDRplay; one of {list(SDRPLAY_BANDWIDTHS_HZ)}")
        elif bw not in SDRPLAY_BANDWIDTHS_HZ:
            errors.append(f"bandwidth_hz {bw} is not an RSP1B IF filter; use one of {list(SDRPLAY_BANDWIDTHS_HZ)}")
        elif isinstance(rate, int) and bw > rate:
            errors.append(f"bandwidth_hz {bw} is wider than sample_rate_hz {rate}")
        if radio.get("gain_db") is not None:
            errors.append("radio.gain_db does not apply to SDRplay; use radio.gain_elements {IFGR, RFGR} (gain reduction)")
        ge = radio.get("gain_elements") or {}
        if gain_mode == "manual" and radio.get("agc"):
            errors.append("gain_mode manual with agc=true is contradictory")
        if gain_mode == "auto" and radio.get("agc") is False:
            errors.append("gain_mode auto on SDRplay means IF AGC; set agc true")
        if gain_mode == "manual":
            ifgr = ge.get("IFGR")
            if ifgr is None:
                errors.append("gain_mode manual needs radio.gain_elements.IFGR")
            elif not (isinstance(ifgr, int) and SDRPLAY_IFGR_DB[0] <= ifgr <= SDRPLAY_IFGR_DB[1]):
                errors.append(f"IFGR {ifgr} must be an integer {SDRPLAY_IFGR_DB[0]}-{SDRPLAY_IFGR_DB[1]} dB")
        elif ge.get("IFGR") is not None:
            errors.append("IFGR is set by AGC in gain_mode auto; drop gain_elements.IFGR")
        rfgr = ge.get("RFGR")
        if rfgr is None:
            errors.append("radio.gain_elements.RFGR (LNA state) is required for SDRplay")
        elif isinstance(center, int) and not (isinstance(rfgr, int) and 0 <= rfgr <= rsp1b_rfgr_max(center)):
            errors.append(f"RFGR {rfgr} must be an integer 0-{rsp1b_rfgr_max(center)} at {center / 1e6:.3f} MHz")
        unknown = set(ge) - {"IFGR", "RFGR"}
        if unknown:
            errors.append(f"unknown SDRplay gain elements {sorted(unknown)}")

    target = radio.get("target_frequency_hz")
    if isinstance(target, int) and isinstance(center, int) and isinstance(rate, int):
        usable = rate / 2 * 0.9          # keep 10% guard from the band edge (anti-alias roll-off)
        if rx.driver == "sdrplay" and isinstance(radio.get("bandwidth_hz"), int):
            usable = min(usable, radio["bandwidth_hz"] / 2 * 0.9)
        if abs(target - center) >= usable and not radio.get("allow_target_outside_passband"):
            errors.append(f"target_frequency_hz {target} is {abs(target - center) / 1e3:.0f} kHz from center; "
                          f"usable half-band is {usable / 1e3:.0f} kHz")
        elif abs(target - center) < 5_000 and rx.driver == "rtlsdr":
            warnings.append("target is within 5 kHz of center; the RTL-SDR DC spike will sit on the signal")


def rate_supported(caps: dict[str, Any], rate: int) -> bool:
    return rate in (caps.get("sample_rates_hz") or []) or any(
        lo <= rate <= hi for lo, hi, *_ in (caps.get("sample_rate_ranges_hz") or []))


def _validate_elements(rx: Receiver, radio: dict[str, Any], center: Any, rate: Any, gain_mode: Any,
                       errors: list[str]) -> None:
    """SoapySDR receivers: everything is checked against what the installed driver reported for this device."""
    c = rx.caps
    if not c:
        errors.append(f"{rx.id} has not been identified: its driver has not reported what it supports yet "
                      "(Receivers → Identify)")
        return
    lo, hi = c.get("frequency_hz") or (None, None)
    if isinstance(center, int) and lo is not None and not lo <= center <= hi:
        errors.append(f"center_frequency_hz {center} is outside {rx.id}'s range {lo}-{hi} Hz (driver-reported)")
    if isinstance(rate, int) and not rate_supported(c, rate):
        errors.append(f"sample_rate_hz {rate} is not offered by {rx.id}'s driver; it offers "
                      f"{c.get('sample_rates_hz') or ''} {c.get('sample_rate_ranges_hz') or ''}".rstrip())
    if gain_mode != "manual" or radio.get("agc"):
        errors.append(f"{rx.id}: only manual gain (gain_elements, agc false) is implemented for {rx.driver} receivers")
    if radio.get("gain_db") is not None:
        errors.append(f"radio.gain_db does not apply to {rx.id}; set radio.gain_elements per element "
                      f"({', '.join(c.get('gains', {}))})")
    ge = radio.get("gain_elements") or {}
    gains = c.get("gains") or {}
    for name, g in gains.items():
        v = ge.get(name)
        if v is None:
            errors.append(f"radio.gain_elements.{name} is required ({g['min']:g}-{g['max']:g} dB)")
        elif not isinstance(v, (int, float)) or not g["min"] <= v <= g["max"]:
            errors.append(f"gain element {name} = {v} is outside {g['min']:g}-{g['max']:g} dB (driver-reported)")
    unknown = set(ge) - set(gains)
    if unknown:
        errors.append(f"{rx.id}'s driver has no gain elements {sorted(unknown)}; it has {sorted(gains)}")
    bw = radio.get("bandwidth_hz")
    if bw is not None:
        ranges = c.get("bandwidth_ranges_hz") or []
        if bw not in (c.get("bandwidths_hz") or []) and not any(a <= bw <= b for a, b, *_ in ranges):
            errors.append(f"bandwidth_hz {bw} is not offered by {rx.id}'s driver; use null for the driver default")
    for flag in ("iq_correction", "dc_offset_correction"):
        if radio.get(flag):
            errors.append(f"radio.{flag}=true is not implemented for {rx.driver} receivers")
    if radio.get("device_settings"):
        errors.append("radio.device_settings is SDRplay-only")
