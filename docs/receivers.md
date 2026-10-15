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
signal-path diagram. Devices without a serial are identified by USB port; duplicate serials are flagged.

## Identify

For SoapySDR receivers (Airspy and the other non-RTL, non-SDRplay devices) Starlab uses only what the installed
driver reports for the actual device: `SoapySDRUtil --probe` lists its frequency range, sample rates, gain
elements and stream formats. Identifying opens the device, so it is refused unless the device is visibly free
and Starlab has nothing booked on it. RTL-SDR and SDRplay RSP1A/RSP1B use capability tables read from this
station's hardware and need no probe.

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
| rtlsdr | all | yes (via `observatory.toml`) |
| sdrplay (RSP1A/RSP1B) | all | yes (via `observatory.toml`) |
| airspy, other SoapySDR | quick signal test, verify | not yet |

## Receivers from `observatory.toml`

The receivers declared under `[scheduler.receivers.*]` are imported into the inventory (database schema 2) and
re-synced from the file on every start, because the cutover scripts and `aobs schedule set-mode` still edit the
file. They keep recording exactly as before (*in service*) and show as *configured* until a Starlab hardware
test verifies them. Upgrading migrates the database automatically, after a backup to `backups/` next to the
database file.
