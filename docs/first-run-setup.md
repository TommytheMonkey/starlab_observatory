# First-run setup

This guide walks through the setup wizard that opens the first time you sign in, including how to try Starlab
with no receiver at all (simulation mode). It assumes Starlab is installed ([Installation](installation.md)).

## Open the wizard

1. Open the sign-in link the installer printed. If it has expired (links last 15 minutes), get a new one on the
   server:

   ```bash
   sudo -u starlab env STARLAB_CONFIG=/etc/starlab/starlab.toml /opt/starlab/venv/bin/starlab web-link
   ```

2. Starlab sends you to `/setup` until setup is finished, and to the station page (`/rf`) afterwards.

Progress is saved after every step, in Starlab's database. You can close the browser and come back later, from any
browser, even after a restart. Everything the wizard sets stays editable later under **Settings**.

## The steps

| # | Step | Required? | What you do |
|---|---|---|---|
| 1 | Welcome | yes | Read what Starlab does and the ways to run it. |
| 2 | Observatory | yes | Name (required), description, display time zone, latitude, longitude, elevation. |
| 3 | Receiver hosts | optional | Explains local USB receivers. Remote hosts are added later (see below). |
| 4 | Hardware discovery | optional | Look for USB receivers; turn on simulation mode. |
| 5 | Receivers | optional | Add discovered receivers, then verify each one. |
| 6 | Satellites | optional | Choose satellites, the minimum peak elevation, and whether to download orbital elements. |
| 7 | Scheduling | optional | Manual, automatic or hybrid. |
| 8 | Integrations | optional | Pointers to Google Calendar and Slack. |
| 9 | Validation | yes | Run the setup checks. |
| 10 | Finish | yes | Finish setup. |

Optional steps have **Skip for now**. Skipped steps are marked in the step list, and you can click any step to go
back to it.

### 2. Observatory

- **Name** is required: the wizard won't continue without one.
- **Time zone** only changes how times are shown. Everything is stored in UTC.
- **Latitude and longitude** are needed to predict satellite passes. Without them, passes aren't predicted and
  automatic recording can't book anything. *Use this browser's location* works only over HTTPS or on `localhost`.
  Otherwise, type the coordinates.
- The location is stored only in this Starlab's database. It's included in a configuration export if you download
  one, so treat export files as private.

### 3. Receiver hosts

This step only explains the options. Receivers plugged into this computer need no setup here. To use receivers on
other Linux computers over SSH, finish the wizard first, then follow [Remote receivers](remote-receivers.md)
(**Settings → Receiver hosts**).

### 4. Hardware discovery

1. Press **Look for hardware**. This reads the USB device list only. It never opens a device, so it can't disturb
   anything that is running.
2. Each device shows its model, serial and status:

   | Status | Meaning |
   |---|---|
   | supported | Ready to add. |
   | driver not installed | Install the package named in the detail (see [Installation → Receiver software](installation.md#receiver-software-optional)), then look again. |
   | not supported yet | Recognized, but Starlab can't record with it (for example SDRplay models other than RSP1A/RSP1B). |
   | not an IQ receiver | For example a YARD Stick One. It can only be documented in the signal-path diagram. |
   | unknown | A USB device Starlab doesn't know. |

3. No receiver? Tick **Simulation mode**. See [Trying Starlab without a receiver](#trying-starlab-without-a-receiver).

### 5. Receivers

1. For each supported device, enter a **Receiver ID** (2 to 32 letters, digits, `-` or `_`, for example `RX1`) and
   a **Name**, then press **Add**.
   - Airspy and other SoapySDR devices first need **Identify**. This opens the device briefly to ask its driver what
     it supports, and is refused if anything is using the device.
2. Press **Verify** on each added receiver. Starlab books a 10-second test, starting in about 25 seconds, captures
   IQ samples and checks them. A real receiver becomes **Verified** only if the samples pass.
3. If verification fails, the receiver stays *Configured · last test failed*, with the reason. You can continue
   setup and fix it later (see [Troubleshooting](troubleshooting.md#verify-fails)).

Recordings can be booked only on verified receivers (or simulated ones). Details: [Receiver configuration](receiver-configuration.md).

### 6. Satellites

- Tick the satellites to predict passes for. METEOR-M2 3, METEOR-M2 4 and the ISS are built in, with their
  downlink frequencies.
- **Ignore passes peaking below** (default 20°) hides low passes.
- **Download current ones from CelesTrak** is off by default. While it's off, Starlab uses the sample orbital
  elements shipped with it. They're fine for exploring, but pass times drift by minutes after a few weeks. Turn it
  on, save, and press **Refresh orbital elements now** to download.
- With the location set, the next 24 hours of passes appear as a check.

### 7. Scheduling

| Mode | What happens |
|---|---|
| Manual (the default) | You book each recording. Enabled automatic rules only show what they would record (as PREDICTED). |
| Automatic / Hybrid | Enabled rules book their passes, and you can still book, change or skip anything by hand. |

Whatever you choose here, **nothing records automatically yet**. Automatic recording also needs an enabled rule
that names a verified receiver, and there are no rules after setup. See [Satellite scheduling](satellite-scheduling.md).

### 8. Integrations

Google Calendar and Slack are optional and set up outside the wizard. See
[Installation → Optional extras](installation.md#optional-extras) and [Slack integration](slack-integration.md).

### 9. Validation

Press **Run the checks**. A failing check blocks finishing; warnings don't.

| Check | Fails or warns when |
|---|---|
| Database | **Fails** if the schema version doesn't match this Starlab version. |
| Storage: default output root / auto output root | **Fails** if the folder isn't writable by the Starlab user. Warns if free space is close to the reserve (Settings → Storage → *Always keep free*). |
| Location | Warns if latitude/longitude aren't set. |
| Pass prediction | Warns if no passes are predicted in the next 48 hours, or the orbital elements are stale. |
| Receivers | Warns if none are configured, or a receiver isn't verified or failed its last test. Simulated receivers are reported as simulated. |
| Automatic recording | Information: on which receivers it is on (normally none). |
| Secrets folder | Warns if it's readable by other users (fix with `chmod 700`). |
| Network | Warns if Starlab listens on more than `127.0.0.1` (reachable without HTTPS). |
| Google Calendar | Information, if enabled. |

### 10. Finish

**Finish setup** runs the checks again and refuses while one fails. Finishing never turns on automatic recording.
You land on the station page, whose header has these buttons:

| Button | What it opens |
|---|---|
| Scheduler | Calendar, upcoming passes, automatic rules, new recording, recording history |
| Recordings | The recording library: outcomes, decoding, storage and retention ([ARCHIVE](ARCHIVE.md)) |
| Receivers | Discovered hardware and the receivers Starlab controls |
| Settings | Every setting, receiver hosts, export/import, backups, the setup wizard |
| Testing | Signal-path tests on receivers |
| History | Versions of the signal-path diagram |

To walk through the wizard again later: **Settings → First-run setup → Open the setup wizard**. Nothing is reset;
each step shows the current values.

## Trying Starlab without a receiver

Simulation mode lets you try every workflow with no hardware.

1. Turn it on: tick **Simulation mode** in the wizard's *Hardware discovery* step, or later in **Settings →
   Simulation → Offer simulated receivers**, and save.
2. In *Receivers* (or **Receivers** in the header), two simulated devices appear under *Discovered hardware*:
   *Simulated receiver (no hardware)*, serials `SIM-0001` and `SIM-0002`.
3. Add one (for example as `SIM1`) and press **Verify**. It becomes **Simulated**, never *Verified*.
4. Set the location, then book a pass on it: **Scheduler → Upcoming passes → Schedule recording**.

What simulated receivers do and don't do:

- Their recordings are synthetic noise with a weak carrier. They're labelled *simulated* in results, on the
  calendar and in **Recordings**.
- They can be booked like a real receiver, but their tests never count as hardware verification.
- Only the quick signal test is available for them.
- Real receivers are unaffected by simulation mode. Turning it off stops offering simulated devices.
