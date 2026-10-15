"""Capture drivers Starlab can run, and the USB devices it recognizes.

A driver says how Starlab opens and records from a receiver: the engine, the sample formats
it writes, and how gain is expressed. RTL-SDR and SDRplay have fixed capability tables that
were read from this station's hardware (scheduling/hardware.py). Every other driver goes
through SoapySDR, and its capabilities are taken only from what the installed SoapySDR module
reports for the actual device (a probe), never from a table: such a receiver cannot be
configured until it has been identified.

USB_MODELS maps a USB vendor:product ID to what it is. Several products share an ID (Airspy R2
and Mini; RSP1A and RSP1B), so the model name says so instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass

ALL_TESTS = ("quick", "scan", "interference", "gain_sweep", "satellite")


@dataclass(frozen=True)
class Driver:
    name: str
    label: str
    engines: tuple[str, ...]
    formats: tuple[str, ...]                 # files this driver writes, in order of preference
    gain_model: str                          # rtl (one dB value) | sdrplay (IFGR/RFGR) | elements (named Soapy gains)
    soapy: str | None                        # SoapySDR module key (driver=...), None for librtlsdr
    test_kinds: tuple[str, ...]              # Test Lab test types implemented for this driver
    automatic: bool                          # automatic pass recording implemented
    needs_probe: bool                        # capabilities come only from a probe of the device
    tools: tuple[str, ...] = ()              # executables needed on the host


DRIVERS: dict[str, Driver] = {
    "rtlsdr": Driver("rtlsdr", "RTL-SDR (librtlsdr, rtl_sdr)", ("rtl_sdr",), ("cu8",), "rtl", None, ALL_TESTS, True,
                     False, ("rtl_sdr",)),
    "sdrplay": Driver("sdrplay", "SDRplay RSP1A/RSP1B (SoapySDRPlay3)", ("soapysdr", "sdrplay", "soapysdrplay"),
                      ("cs16", "cf32"), "sdrplay", "sdrplay", ALL_TESTS, True, False),
    "airspy": Driver("airspy", "Airspy R2 / Mini (SoapyAirspy)", ("soapysdr",), ("cs16", "cf32"), "elements", "airspy",
                     ("quick",), False, True),
    "soapy": Driver("soapy", "SoapySDR device", ("soapysdr",), ("cs16", "cf32"), "elements", None, ("quick",), False,
                    True),
}

# Formats a SoapySDR stream can hand us, mapped to the file formats Starlab writes and analyses.
SOAPY_FORMATS = {"CS16": "cs16", "CF32": "cf32"}


@dataclass(frozen=True)
class UsbModel:
    model: str
    manufacturer: str
    kind: str                                # sdr | transceiver
    driver: str | None                       # Starlab driver, None = no capture driver
    soapy: str | None = None                 # SoapySDR module for generic devices
    serial_prefix: str = ""                  # stripped from the USB serial string to get the hardware serial
    note: str = ""


USB_MODELS: dict[tuple[str, str], UsbModel] = {
    ("0bda", "2838"): UsbModel("RTL2832U receiver (RTL-SDR)", "Realtek", "sdr", "rtlsdr"),
    ("0bda", "2832"): UsbModel("RTL2832U receiver (RTL-SDR)", "Realtek", "sdr", "rtlsdr"),
    ("1df7", "3050"): UsbModel("SDRplay RSP1A or RSP1B (same USB ID)", "SDRplay", "sdr", "sdrplay"),
    ("1df7", "3000"): UsbModel("SDRplay RSP1", "SDRplay", "sdr", None, "sdrplay",
                           note="Starlab's SDRplay gain and filter tables cover the RSP1A/RSP1B only"),
    ("1df7", "3010"): UsbModel("SDRplay RSP2", "SDRplay", "sdr", None, "sdrplay",
                           note="Starlab's SDRplay gain and filter tables cover the RSP1A/RSP1B only"),
    ("1df7", "3020"): UsbModel("SDRplay RSPduo", "SDRplay", "sdr", None, "sdrplay",
                           note="Starlab's SDRplay gain and filter tables cover the RSP1A/RSP1B only"),
    ("1df7", "3030"): UsbModel("SDRplay RSPdx", "SDRplay", "sdr", None, "sdrplay",
                           note="Starlab's SDRplay gain and filter tables cover the RSP1A/RSP1B only"),
    ("1d50", "60a1"): UsbModel("Airspy R2 or Airspy Mini (same USB ID)", "Airspy", "sdr", "airspy", "airspy",
                           serial_prefix="AIRSPY SN:"),
    ("03eb", "800c"): UsbModel("Airspy HF+", "Airspy", "sdr", "soapy", "airspyhf"),
    ("1d50", "6089"): UsbModel("HackRF One", "Great Scott Gadgets", "sdr", "soapy", "hackrf"),
    ("1d50", "cc15"): UsbModel("rad1o (HackRF)", "Munich hackerspace", "sdr", "soapy", "hackrf"),
    ("0456", "b673"): UsbModel("ADALM-Pluto", "Analog Devices", "sdr", "soapy", "plutosdr"),
    ("1d50", "6108"): UsbModel("LimeSDR-USB", "Lime Microsystems", "sdr", "soapy", "lime"),
    ("0403", "601f"): UsbModel("LimeSDR Mini", "Lime Microsystems", "sdr", "soapy", "lime"),
    ("2500", "0020"): UsbModel("USRP B200/B210", "Ettus Research", "sdr", "soapy", "uhd"),
    ("2500", "0021"): UsbModel("USRP B200mini", "Ettus Research", "sdr", "soapy", "uhd"),
    ("2500", "0022"): UsbModel("USRP B205mini", "Ettus Research", "sdr", "soapy", "uhd"),
    ("1d50", "605b"): UsbModel("YARD Stick One", "Great Scott Gadgets", "transceiver", None,
                           note="a sub-GHz packet transceiver (CC1111, rfcat); it does not stream IQ samples, so "
                                "Starlab cannot record or test with it. Document it in the signal-path layout instead."),
}


def canonical_serial(model: UsbModel | None, usb_serial: str) -> str:
    s = usb_serial.strip()
    if model and model.serial_prefix and s.upper().startswith(model.serial_prefix.upper()):
        s = s[len(model.serial_prefix):].strip()
    if model and model.driver == "airspy":
        s = s.upper().removeprefix("0X")
    return s
