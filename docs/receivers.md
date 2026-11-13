# Receivers: discovered, configured, verified

Every receiver Starlab knows about is in one of three states.

| State | Meaning | What Starlab may do with it |
|---|---|---|
| **Discovered** | Seen on USB by passive discovery; not in the inventory | Nothing, except *Identify* when the device is visibly free |
| **Configured** | In the inventory (added in the web UI, or imported from `observatory.toml`) | Hardware tests only, unless it was imported while already recording (*in service*) |
| **Verified** | A hardware test captured real IQ samples from it, and they were checked | Tests and recording jobs |

A failed test, or a change to the receiver's hardware identity (host, driver, serial, USB port identity),
leaves the receiver *configured*; the Receivers page shows why. Appearing in the UI never makes a receiver ready.

## Discovery never opens a device

Discovery reads `/sys/bus/usb/devices` (vendor, product, serial, which kernel driver holds each interface) and
`/proc` (processes holding the USB device node). A program that is streaming from a device has claimed its USB
interface, and the kernel shows that as the `usbfs` interface driver even when the program belongs to another
user, so discovery can tell "in use" apart from "free" without touching the device. It is safe to run during
recordings.

Recognized hardware: RTL-SDR (RTL2832U), SDRplay RSP1A/RSP1B, Airspy R2/Mini, Airspy HF+, HackRF, ADALM-Pluto,
LimeSDR, USRP B2xx. The YARD Stick One is recognized as *not an IQ receiver*: it can only be documented in the
signal-path diagram. Devices without a serial are identified by USB port (two of them on one host can both be
receivers since schema 5). Duplicate serials are flagged, and such devices cannot be identified or added until
each has its own serial (`rtl_eeprom -s` for RTL-SDRs), because the capture tools open receivers by serial.

## Identify

*Identify* asks the device's own tools what it is, on the host it is plugged into, and shows each value with the
tool that reported it. Nothing is filled in from tables: if no installed tool reports a frequency range, none is
shown.

| Device | Tools | Typically reports |
|---|---|---|
| RTL-SDR | `rtl_test`, `rtl_eeprom`, `SoapySDRUtil --probe` (if SoapyRTLSDR is installed) | tuner, gain steps, EEPROM strings; frequency range, rates, bias-tee setting from SoapySDR |
| Airspy | `airspy_info`, `SoapySDRUtil --probe` | firmware, part ID, sample rates; ranges, gains, bias tee |
| HackRF | `hackrf_info`, `SoapySDRUtil --probe` | firmware, board, hardware revision; ranges, gains |
| SDRplay | `SoapySDRUtil --find=driver=sdrplay` | model (RSP1A or RSP1B) and serial from the SDRplay API; no device is selected |
| ADALM-Pluto, LimeSDR | `iio_info -s`, `LimeUtil --find`, `SoapySDRUtil --probe` | URI or model, then ranges and gains |
| USRP, others | `SoapySDRUtil --probe` | ranges, rates, gains (`uhd_find_devices` is never run: it can load firmware) |

Identifying opens the device, so it is refused unless the device is visibly free and Starlab has nothing booked
on it. `airspy_info` and `hackrf_info` open every Airspy or HackRF on the host in turn, so they are skipped
(and the reason shown) unless all of those devices are free. `rtl_test` and `rtl_eeprom` are told the device's
index, never its serial: they read a serial made of digits, such as the common `00000001`, as an index, which
could open a different receiver. The index comes from the tools' own device list and is checked against the
serial in their output. Devices that share a serial on one host are never identified or added.

For SoapySDR receivers (Airspy and the other non-RTL, non-SDRplay devices), Identify must succeed before the
receiver can be added: their recording capabilities come only from the driver's probe. RTL-SDR and SDRplay
RSP1A/RSP1B record with capability tables read from this station's hardware.

Antennas, filters, amplifiers and cables are passive or are not USB devices, so they cannot be discovered or
identified. Document them in the signal-path diagram.

## Receiver hosts

Receivers can also be discovered on other Linux computers over SSH: **Settings → Receiver hosts**. Discovery
there is the same read-only snapshot as on this computer, and Identify runs the same tools on that host.
Receivers on a remote host are added the same way. Starlab tests and records on them over SSH (brief 7), and can
keep their recordings on the host (brief 8). See `docs/remote-receivers.md` and `docs/hardware-discovery.md`.

## Verify

*Verify hardware* books a 10-second quick test through the normal scheduler. It takes the same exclusive
ownership as any recording, and is refused if another program has the device open or claimed, if the
receiver is leased, or if a booked pass would be disrupted. When the capture finishes, Starlab reads the
samples themselves (early, middle and late windows) and checks:

- the file has the expected length;
- every value is finite and not all zero;
- I and Q vary like real receiver noise, with enough distinct codes and spread;
- the stream is not stuck repeating samples;
- I and Q are not copies of each other.

Only then does the receiver become *verified*.

## What a new receiver can do

New receivers never record automatically. Driver support today:

| Driver | Tests | Automatic recording |
|---|---|---|
| rtlsdr | all | rules (Scheduler → Automatic rules); also the older `observatory.toml` automatic blocks |
| sdrplay (RSP1A/RSP1B) | all | rules; also the older `observatory.toml` automatic blocks |
| airspy, other SoapySDR | quick signal test, verify | rules (any verified receiver); not the older `observatory.toml` blocks |

## Receivers from `observatory.toml`

The receivers declared under `[scheduler.receivers.*]` are imported into the inventory (database schema 2) and
re-synced from the file on every start, because the cutover scripts and `aobs schedule set-mode` still edit the
file. They keep recording exactly as before (*in service*) and show as *configured* until a Starlab hardware
test verifies them. Upgrading migrates the database automatically, after a backup to `backups/` next to the
database file.
