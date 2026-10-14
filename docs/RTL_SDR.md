# RTL-SDR software and receiver health (investigated 2026-10-13)

## Two `rtl_sdr` executables, one library

| | `/usr/bin/rtl_sdr` | `/usr/local/bin/rtl_sdr` |
|---|---|---|
| Origin | Debian package `rtl-sdr` 2.0.2-2build1 (osmocom) | Source build, installed 2026-10-05 20:17 with `/usr/local/lib/librtlsdr.so.0.6git` (pkg-config version `0.6.0-131-gaed0`, the RTL-SDR Blog fork: contains "RTL-SDR Blog V4 Detected") |
| Library it actually loads | `/usr/local/lib/librtlsdr.so.0` | `/usr/local/lib/librtlsdr.so.0` |
| `-d` | "device_index or serial" | help text says "device_index", but the binary has the same `verbose_device_search` serial matching ("No matching devices found", "SN: %s") |
| Options | `-f -s -d -g -p -b -n -S -D` | same set |
| Used by | the broker's capture (`rtl_sdr_path`, default `/usr/bin/rtl_sdr`) and the legacy recorders | an interactive shell (`type -a rtl_sdr` lists `/usr/local/bin` first) |

**Confirmed:** `ldd` and `LD_DEBUG=libs` show that both executables resolve `librtlsdr.so.0` to `/usr/local/lib`, because `/etc/ld.so.conf.d` lists `/usr/local/lib` before the multiarch directory. The Debian library `/usr/lib/x86_64-linux-gnu/librtlsdr.so.2.0.1` is installed but neither tool uses it. Tuner initialization, gain tables, the PLL code and direct-sampling handling all live in the library, so both tools drive the hardware identically. Only the front-end differs (help text, the wording of direct-sampling messages).

**Not verified on hardware:** that `/usr/local/bin/rtl_sdr -d OBS-R0x` selects by serial. The strings show the same search code, and the manual R02 diagnostic worked with it, but I did not open a device to confirm this, because no new RF recordings were allowed.

**Decision:** nothing was removed, replaced or switched; there is no demonstrated defect. Risk to remember: if `/usr/local/lib/librtlsdr*` is ever removed or `ldconfig` order changes, `/usr/bin/rtl_sdr` silently switches to the Debian 2.0.2 library.

## Start-up messages (`PLL not locked`, direct sampling)

Every R820T open on this host prints, in this order:

```
Found Rafael Micro R820T tuner
Enabled direct sampling mode, input 2
Sampling at 2400000 S/s.
[R82XX] PLL not locked!
Disabled direct sampling mode
Tuned to 137500000 Hz.
Tuner gain set to 38.60 dB.
```

**Confirmed:**
- These exact lines appear in OBS-R02's flat (failed) captures at 16:27Z and 16:37Z and in its healthy captures at 17:18Z and 17:20Z. They also appear in the legacy recorder's logs.
- The lines come from the library: "input 2" without "/Q" is the library's format string.

So the warning did not tell the failure apart from health. It is a start-up artifact: the library enables direct sampling while nothing is tuned, tries the PLL in that state, then disables it at the first real tune.

The broker parses each capture log (`aobs.scheduling.rtllog`) and stores:
- the selected serial and tuner;
- requested versus tuned frequency, sample rate and gain;
- the final direct-sampling state;
- PLL warnings before and after the final tune.

It treats them as follows:

| Situation | Classification |
|---|---|
| One PLL warning *before* the tune | `ok_transient_pll_warning` (not a fault) |
| A warning *after* the final tune, a missing tune, or direct sampling left on | `tuning_failure_suspected` |
| `persistent_failure` | only after three such logs in a row on one receiver |

## IQ health (`aobs.scheduling.iqhealth`)

The R02 failure was invisible to every process-level check:
- rtl_sdr exited normally;
- the files were full size;
- the logs were identical to healthy ones.

The samples sat on 3–4 ADC codes around 127.5. Spread was 0.51 LSB, RMS −44.9 dBFS around 127.5, and 31% of consecutive samples were identical, at both 8.7 dB and 38.6 dB.

**Onset:** every R02 file still on disk from 07:31Z (legacy recorder, 10 dB) until the USB replug at 16:41:46Z (12:41:46 EDT, kernel log) is flat. Every file after the replug is healthy. Older files had already been deleted by retention, so the onset is unknown. The kernel logged no USB error, reset or over-current before the replug.

Each RTL-SDR capture is now assessed from 32 blocks spread through the file, about 8 MB read in about 0.1 s. The results go to:
- `<raw>.health.json` (a `diagnostics` artifact);
- `result.capture.iq_health` and `result.capture.tuner` for broker recordings;
- `result.iq_health` for legacy recordings, including OBS-R01's before cutover.

| Class | Rule |
|---|---|
| `capture_failure` | missing or empty file, under 90% of the expected bytes, or mostly all-zero blocks |
| `suspicious_flat` | ≥10% of blocks with under 1 LSB spread on I and Q **and** ≤8 distinct codes. A requested gain under 5 dB downgrades it to `weak_signal`. When another flat recording ≥6 dB away in gain has the same level, the evidence records "no gain response" |
| `possible_overload` | more than 0.1% of samples at code 0/255, or spread above 45 LSB |
| `weak_signal` | spread under 3 LSB but varying normally |
| `healthy` | none of the above |

`suspicious_flat` and `capture_failure` post a `health` notification at the top level of #star-lab. The job's completion notice then says "finished recording, but the IQ is NOT healthy". The latest result per receiver shows in `/radio-status` and in `/radio-health` (FAIL or WARNING). Nothing resets USB or power-cycles a receiver automatically.

Check any file by hand (read-only):
```
.venv/bin/aobs schedule iq-health --gain 38.6 --paths /data/observatory/jobs/*/OBS-R02*.cu8
```

Calibration on real files:

| Recording | Class | RMS | Spread | Codes (I/Q) |
|---|---|---|---|---|
| R02 16:27Z, 38.6 dB (stuck) | suspicious_flat | −44.9 dBFS | 0.51 LSB | 3/4 |
| R02 17:18Z, 8.7 dB (after replug) | healthy | −19.64 dBFS | 9.44 LSB | 121/126 |
| R02 17:20Z, 20.7 dB | healthy | −14.15 dBFS | 17.75 LSB | 203/199 |
| R01 legacy, 29.7 dB | healthy | about −21 dBFS | 7.7–9.0 LSB | about 113–126 |
| R01 legacy, 8.7 dB, after the switch to V-dipole + SAWbird (2026-10-13 15:22) | possible_overload | −4 to −6 dBFS, 0.8–2.2% clipped | 45–56 LSB | 256/256 (still decoded 128 images on 10-14 08:44Z) |
