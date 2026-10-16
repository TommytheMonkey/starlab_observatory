# Receiver configuration

This guide covers adding SDR receivers plugged into the Starlab computer: which hardware is supported, how
Starlab discovers, identifies and verifies a receiver, and what it refuses to do and why. For receivers on other
computers, see [Remote receivers](remote-receivers.md). To run without any receiver, see
[First-run setup → Trying Starlab without a receiver](first-run-setup.md#trying-starlab-without-a-receiver).

Everything here happens on the **Receivers** page (header button **Receivers**, or **Settings → Hardware →
Receivers**).

## Supported hardware

| Hardware | Starlab driver | Software needed | Tests available | Real-hardware status |
|---|---|---|---|---|
| RTL-SDR (RTL2832U) | rtlsdr | `rtl-sdr` | all (quick, scan, interference, gain sweep, satellite) | recording exercised |
| SDRplay RSP1A / RSP1B | sdrplay | SDRplay API + SoapySDRPlay3 | all | recording exercised (RSP1B) |
| Airspy R2 / Mini | airspy (SoapySDR) | `soapysdr-module-airspy` | quick test, verify | discovery and identify exercised; recording not exercised |
| Airspy HF+, HackRF One, ADALM-Pluto, LimeSDR (USB, Mini), USRP B200/B210/B200mini/B205mini | soapy (SoapySDR) | the SoapySDR module for the radio | quick test, verify | not tested |
| SDRplay RSP1, RSP2, RSPduo, RSPdx | none | — | — | recognized, not supported: Starlab's SDRplay tables cover the RSP1A/RSP1B only |
| YARD Stick One | none | — | — | recognized as *not an IQ receiver*; it can only be documented in the signal-path diagram |
| Simulated receiver | sim | none | quick test | synthetic samples only |

RTL-SDR and SDRplay RSP1A/RSP1B use built-in capability tables (frequency range, sample rates, gain steps). Every
other receiver's capabilities come only from what its SoapySDR driver reports for the actual device, so it must be
**identified** before it can be added.

Wideband scans, gain sweeps and satellite tests are only available for RTL-SDR and SDRplay receivers on this
computer.

## Receiver states

| State | Meaning | What Starlab will do with it |
|---|---|---|
| **Discovered** | Seen on USB, not added | Nothing, except *Identify* when the device is visibly free |
| **Configured** | Added, not yet proven by a test | Hardware tests only; no recordings |
| **Configured · last test failed** | Its last hardware test failed | Tests only; the reasons are shown |
| **Verified** | A hardware test captured real IQ samples from it, and they passed the checks | Tests and recordings |
| **Simulated** | A simulated receiver that passed its test | Tests and (synthetic) recordings; never counts as hardware |

Appearing in the list never makes a receiver ready. Recording jobs, manual or automatic, are refused on a real
receiver until it's verified ("has not passed a hardware test yet … only tests can run on it until a test captures
real IQ samples").

## Add a receiver

1. Install the software for your receiver ([Installation → Receiver software](installation.md#receiver-software-optional))
   and plug it in.
2. On **Receivers**, press **Discover devices**. The device appears under *Discovered hardware, not configured*,
   with its serial, USB port, whether it's in use, and the driver status.
   - Discovery only reads the USB device list (`/sys`) and which processes hold the device (`/proc`). It never opens
     a device, so it's safe during recordings.
3. **Airspy and other SoapySDR devices:** press **Identify** first (see below). The identification is valid for 6
   hours; after that, identify again.
4. Enter a **Receiver ID** and a **Name**, and press **Add as receiver**.
   - The ID is what jobs, rules and the diagram use: 2 to 32 letters, digits, `-` or `_`, starting with a letter or
     digit (for example `RX1`). It can't be changed later. The name is free text.
   - If the signal-path diagram already documents this device (same serial), tick *Use the diagram entry …* to keep
     its parts.
5. Press **Verify** (see below).

### What Starlab refuses when adding

| Refusal | Why |
|---|---|
| "this device is already receiver …" | Each physical device can be one receiver only. |
| "… is not an IQ receiver" | The device doesn't stream IQ samples; document it in the diagram instead. |
| "… cannot be added: needs rtl_sdr installed" / "the SoapySDR module for … is not installed" | Install the missing software, then **Refresh**. |
| "more than one device on this host reports serial …" | The capture tools open receivers by serial, so Starlab could record from the wrong one. Give each device a unique serial first (RTL-SDR: `rtl_eeprom -s <serial>`, with only that dongle plugged in). |
| "the SDRplay device has no serial yet (firmware not loaded)" | An RSP starts in its bootloader. Wait a moment, then **Refresh**. |
| "identify the device first" / "the identification is too old" | SoapySDR receivers need a current identification. |
| "… is already used in the signal-path layout; choose another ID" | Choose another ID, or tick *Use the diagram entry*. |

A device without a serial number (some cheap RTL-SDRs) is identified by its **USB port** instead. Keep it in the
same port: moved to another port, it no longer matches the receiver.

## Identify

**Identify** asks the device's own tools what it is, on the computer it's plugged into, and shows each value with
the tool that reported it. Nothing is filled in from tables.

| Device | Tools run |
|---|---|
| RTL-SDR | `rtl_test`, `rtl_eeprom`, and `SoapySDRUtil --probe` if the SoapySDR RTL-SDR module is installed |
| Airspy | `airspy_info`, `SoapySDRUtil --probe` |
| HackRF | `hackrf_info`, `SoapySDRUtil --probe` |
| SDRplay | `SoapySDRUtil --find=driver=sdrplay` (reads the API's device list; no device is opened) |
| ADALM-Pluto, LimeSDR | `iio_info -s`, `LimeUtil --find`, `SoapySDRUtil --probe` |
| USRP and others | `SoapySDRUtil --probe` (`uhd_find_devices` is never run: it can load firmware) |

Identifying opens the device for a few seconds, so Starlab refuses ("Not identifying it now …") when:

- another program has it open or has claimed its USB interface;
- Starlab has it booked;
- for SDRplay, an RSP recording is being written;
- another device is being identified (one at a time).

`airspy_info` and `hackrf_info` open every Airspy or HackRF on the computer, so they're skipped (and the reason
shown) unless all of those devices are free. RTL-SDR tools are told the device index, checked against the serial,
because they would read a serial made of digits (such as `00000001`) as an index.

## Verify

**Verify** proves a receiver works by recording from it.

1. Press **Verify** on the receiver. Starlab books a 10-second quick test through the normal scheduler. It takes the
   same exclusive hold on the device as any recording.
2. When the capture finishes, Starlab reads the samples (early, middle and late in the file) and checks that:
   - the file has the expected length;
   - every value is finite and not all zero;
   - I and Q vary like real receiver noise, with enough distinct values and spread;
   - the stream isn't stuck repeating samples;
   - I and Q aren't copies of each other.
3. If all pass, the receiver becomes **Verified**. Otherwise it stays *Configured · last test failed*, with the
   reasons (see [Troubleshooting → Verify fails](troubleshooting.md#verify-fails)).

Verify is refused if another program has the device open or claimed, if it's leased, or if a booked recording would
be disrupted.

A receiver goes back to *Configured* when its hardware identity changes (host, driver, serial or USB port). Verify
it again.

## Rename, disable or remove

On the receiver's card:

- **Save** changes the name and notes.
- **Disable** keeps the receiver but stops Starlab from using it. Refused while a recording is booked or running
  ("disable it afterwards").
- **Remove** takes it out of Starlab (click twice to confirm). Refused while it has scheduled or running jobs
  ("cancel them first"). Its lane stays in the signal-path diagram as documentation.

## Antennas, filters and amplifiers

Antennas, filters, amplifiers and cables are passive or aren't USB devices, so they can't be discovered. Document
them in the signal-path diagram on the station page. Starlab uses the diagram to warn when a recording's frequency
is outside its antenna's range or blocked by a filter. These are warnings, never refusals.

## SDR++ listening sessions

You can lend a receiver to [SDR++](https://www.sdrpp.org/) for live listening: select the receiver, then **Test ▾ →
SDR++ Server → Start**, and connect from SDR++ on another computer (source *SDR++ Server*).

Requirements and limits:

- `sdrpp` and `bubblewrap` must be installed. Without bubblewrap, no session is started. Starlab runs SDR++ in a
  sandbox where only the selected receiver is visible and can be opened.
- RTL-SDR, Airspy and simulated receivers on this computer only. SDRplay receivers aren't offered: they're reached
  through the SDRplay API service, so the sandbox can't isolate one.
- The session reserves the receiver. By default it ends 2 minutes before the next booked recording (with a warning
  5 minutes ahead), and after 30 minutes with no client connected.
- The server listens on `127.0.0.1` by default; connect through an SSH tunnel (`ssh -L <port>:127.0.0.1:<port>`).
  Other addresses must be listed in `starlab.toml` under `[sessions] trusted_addresses`. All interfaces
  (`0.0.0.0`) are allowed only with `allow_all_interfaces = true`.

SDR++ sessions haven't been confirmed on real hardware outside our station. More detail: [sdrpp-sessions.md](sdrpp-sessions.md).

## Programs outside Starlab

Starlab never opens a receiver that another program (SDR++, dump1090, rtl_tcp, GQRX, …) is using. It sees such
programs from the USB state and refuses tests, identification and recordings on that receiver ("another process has
claimed its USB interface"). Close the other program, or give it its own receiver.
