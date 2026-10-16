# Troubleshooting

This guide lists common problems, the messages Starlab shows for them, and what to do. Starlab's messages are
meant to be acted on directly. A failed recording names the receiver, says what went wrong and what to try, and
keeps the raw error text next to it.

`starlab` below means the command run as the service account; see
[Installation → Running Starlab commands](installation.md#running-starlab-commands).

## Contents

- [Where to look first](#where-to-look-first)
- [Installation](#installation)
- [The service doesn't start](#the-service-doesnt-start)
- [Can't sign in](#cant-sign-in)
- [Setup can't finish](#setup-cant-finish)
- [A receiver isn't found or can't be added](#a-receiver-isnt-found-or-cant-be-added)
- [Identify is refused](#identify-is-refused)
- [Verify fails](#verify-fails)
- [A recording failed](#a-recording-failed)
- [Remote receiver hosts](#remote-receiver-hosts)
- [No passes are predicted](#no-passes-are-predicted)
- [Automatic recording doesn't book anything](#automatic-recording-doesnt-book-anything)
- [Storage](#storage)
- [Settings won't save](#settings-wont-save)
- [SDR++ sessions are refused](#sdr-sessions-are-refused)
- [Database and upgrades](#database-and-upgrades)

## Where to look first

| What | Where |
|---|---|
| Service log | `journalctl -u starlab -n 200` (user install: `journalctl --user -u starlab`) |
| Service state | `systemctl status starlab` |
| A job's start checks, errors and recorder log | Scheduler → open the job → **Execution** |
| A recording's files, outcome and logs | **Recordings** → the recording |
| A receiver's readiness and last test | **Receivers** → the receiver's card |
| Database state | `starlab db status` (read-only) |
| Setup checks | Settings → First-run setup → Open the setup wizard → Validation → **Run the checks** |

## Installation

See [Installation → If the installer stops](installation.md#if-the-installer-stops) for the installer's own
messages. Also:

- **`--upgrade: no installation found`**: `--upgrade` needs an existing installation (it reads `install.env` in the
  config folder). For a first install, leave out `--upgrade`. If you used `--config-dir`, give it again.
- **`the database backup failed; nothing was changed`** (during `--upgrade`): run `starlab db backup` by hand to see
  the error. It's usually disk space or permissions on the data folder.
- **`systemd is not running here, so no service was installed`**: expected in containers. Start Starlab with the
  printed command (`STARLAB_CONFIG=… /opt/starlab/venv/bin/starlab broker`).

## The service doesn't start

Check `journalctl -u starlab -n 100`:

| Log message | Cause and fix |
|---|---|
| `No configuration file found (tried …)` | The service can't find `starlab.toml`. The unit sets `STARLAB_CONFIG`; check the file exists and is readable by the `starlab` group (`/etc/starlab` is `root:starlab`, mode 750). |
| `…: not valid TOML: …` | A typo in `starlab.toml`. Fix the line shown. |
| `[paths] data_root is required` | `starlab.toml` lost its `[paths]` section. Restore it (see the installer's template in `install.sh`). |
| `Database schema N is newer than this code (M); install the newer Starlab version or restore an older backup` | You're running an older Starlab on a database that a newer version migrated. Reinstall the newer version, or roll back properly: [Backup and restore → Rolling back an upgrade](backup-and-restore.md#rolling-back-an-upgrade). |
| `address already in use` | Another program uses port 8751. Change `[broker] port` in `starlab.toml`, or stop the other program. |

A broker lock prevents two Starlab instances on one data folder.

## Can't sign in

| Symptom | Fix |
|---|---|
| "this sign-in link has expired" | Links last 15 minutes. Make a new one: `starlab web-link`. |
| The page asks for a sign-in link again | The browser's session ended (after 30 days) or cookies were cleared. Make a new link. |
| The link opens but you aren't signed in | The link's address must be the one your browser uses. Through an SSH tunnel or proxy, use `starlab web-link --base-url http://<address you use>:<port>`. |
| The page doesn't load from another computer | Starlab listens on `127.0.0.1` by default. Use an SSH tunnel or see [Installation → Reaching Starlab from another computer](installation.md#reaching-starlab-from-another-computer). |
| "Use this browser's location" does nothing | Browsers share location only over HTTPS or on `localhost`. Type the coordinates. |

## Setup can't finish

**Finish setup** is refused while a check **fails**:

| Check | Fix |
|---|---|
| Database: schema X (this version needs Y) | Restart Starlab so it migrates the database; check the log for migration errors. |
| Storage: … NOT writable by the Starlab user | The folder in Settings → Storage must be writable by the `starlab` account, and must be under the data folder for a system install (see [Storage](#storage)). |

Warnings (location, no receivers, secrets folder permissions, network exposure) don't block finishing.

## A receiver isn't found or can't be added

Press **Discover devices** on **Receivers** after plugging in or installing software. Then check the device's
status:

| Status or message | Fix |
|---|---|
| Not listed at all | Check `lsusb`. Try another cable or port, or a powered hub. Discovery lists USB devices only. |
| *driver not installed*: "needs rtl_sdr installed" | `sudo apt install rtl-sdr` |
| *driver not installed*: "SoapySDR is not installed or SoapySDRUtil failed" | `sudo apt install soapysdr-tools python3-soapysdr` |
| *driver not installed*: "the SoapySDR module for … is not installed" | Install the module (for example `soapysdr-module-airspy`; SDRplay: SoapySDRPlay3). Check with `SoapySDRUtil --info`. |
| *not supported yet* | Recognized, but Starlab can't record with it (for example SDRplay RSP2/RSPduo/RSPdx). |
| *not an IQ receiver* | Document it in the signal-path diagram instead. |
| *unknown* | A USB device Starlab doesn't know. |
| "more than one device on this host reports serial …" | Two receivers share a serial (common with RTL-SDRs set to `00000001`). Starlab won't identify or add either one. Give each a unique serial: plug in one dongle alone and run `rtl_eeprom -s <new serial>`, then replug. |
| "the SDRplay device has no serial yet (firmware not loaded)" | The RSP is still in its bootloader. Wait, then **Refresh**. If it stays that way, restart the SDRplay API service. |
| "identify the device first" | Airspy and other SoapySDR receivers need **Identify** before **Add**. |
| A serial-less receiver no longer matches | It's identified by USB port. Put it back in its original port. |

## Identify is refused

Identify opens the device, so Starlab refuses ("Not identifying it now, because identifying opens the device: … Nothing
was opened.") when:

- **"it is in use: …"**: another program has the device open or claimed. Close SDR++, dump1090, rtl_tcp, GQRX etc.
- **"Starlab has it booked: …"**: wait until the recording or test is over.
- **"an RSP recording is being written right now"** (SDRplay).
- **"another device is being identified"**: wait a moment.

Some tools are skipped (with the reason) rather than refused: `airspy_info` and `hackrf_info` open every device of
their kind, so they run only if all of those devices are free.

## Verify fails

The receiver stays *Configured · last test failed* and its card shows the reasons.

| Reason | Meaning and fix |
|---|---|
| Not started: the device is in use, leased, or a booking is near | Close the other program, or verify between recordings. |
| The capture failed to open the device | See the error codes in [A recording failed](#a-recording-failed): usually permissions, a missing driver, or another program. |
| File shorter than expected | USB drops or the device stalled. Try another port or cable, or a powered hub. |
| All zero / not finite | The driver delivered no real samples. Replug the receiver. |
| I and Q don't vary like receiver noise / too few distinct values / stuck repeating samples | A stuck tuner (a known RTL-SDR failure). Replug the USB cable and verify again. If it stays flat, try another port or cable, then replace the dongle. |
| I and Q are copies of each other | The driver or device isn't delivering complex samples. Check the driver version. |

An antenna or amplifier fault lowers the signal level but leaves normal noise, so a flat result points at the
receiver or its USB link, not the antenna.

## A recording failed

Open the job (Scheduler → Calendar or Recording history) and read **Execution**. The first lines are the start
checks; errors are translated into these messages:

| Message (with your receiver's ID) | What to do |
|---|---|
| … could not start because another process has claimed its USB interface. | Close the other program (SDR++, dump1090, rtl_tcp, another rtl_sdr), or wait for it to finish. |
| … could not start because another program is using it. | Close that program or wait; Starlab never opens a receiver a second time. |
| … could not start because another Starlab job holds its reservation. | Two jobs were booked on the same device; check the receiver's schedule. |
| … the SDRplay API reports the device is in use or not responding. | Make sure no other SDRplay program is open. If the API hangs, restart its service between recordings. |
| … could not be opened: this user is not allowed to access its USB device. | Install udev rules (`sudo ./install.sh --udev`) or add the user to `plugdev`, then replug. |
| … disappeared from USB during the recording. | Check the cable, hub and power supply; `journalctl -k` shows USB resets. |
| … was not found on USB. | Plug it into the right computer; **Receivers** shows what is connected. |
| … cannot record: there is not enough disk space for the recording. | Free space, or choose another storage folder (Settings → Storage). |
| …'s recording could not write its files (permission denied). | Check the owner and mode of the recording folder; see [Storage](#storage). |
| … cannot record on this host: the Python module numpy / SoapySDR is not installed there. | Install `python3-numpy` / `python3-soapysdr` on that host. |
| … cannot record on this host: a required program is not installed (rtl_sdr). | Install the receiver's tools on that host. |
| …'s host could not be reached over SSH. | Check the host is on and reachable; Starlab retries until the recording window ends. |
| …'s host presented a different SSH key, so Starlab did not connect. | See [Remote receiver hosts](#remote-receiver-hosts). |
| …'s host refused Starlab's SSH key. | Add Starlab's public key to `~/.ssh/authorized_keys` of that user on the host. |
| …'s host restarted during the recording, which stopped it. | Find out why the host restarted (power, updates). The part recorded before the restart is kept, if any. |
| …'s recorder stopped unexpectedly (crashed or was killed). | Read the recorder log on the job page; out-of-memory kills appear in `journalctl -k`. |
| …'s recorder did not stop at the end of the recording and was stopped by Starlab. | The recorder or driver may be hung. Check the recorder log; replug the receiver. |
| … recorded less than the schedule asked for. | Usually USB drops or a stalled device. Check IQ health and the recorder log. |
| …'s driver did not accept the requested settings. | Choose a sample rate, gain or bandwidth the receiver supports. |

Other checks that fail a recording after it ran: the file is missing, under 98% of the scheduled samples, cut
mid-sample, or (RTL-SDR) flat. The file is kept for inspection.

Missed recordings: if Starlab wasn't running when a recording should have started, the job becomes FAILED (missed).
Starlab never starts a recording late.

## Remote receiver hosts

Messages when adding, testing or using a host (Settings → Receiver hosts):

| Message | Fix |
|---|---|
| the host's SSH key is not the one you accepted … Nothing was run. | Find out why the key changed (reinstall?). Then **Review the new host key**, compare fingerprints on the host, and accept only a match. |
| the host's SSH key has not been accepted; nothing was run | Accept the host key (compare fingerprints first). |
| the host refused Starlab's SSH key | Add the public key shown on the page to `~/.ssh/authorized_keys` of the login user (folder mode 700, file mode 600). |
| the host closed the connection before logging in | OpenSSH briefly refuses an address right after a key scan or failed logins (PerSourcePenalties), or when too many connections are open (MaxStartups). Wait a few seconds and test again. Starlab retries once by itself when saving. |
| nothing accepts SSH connections on that port | Wrong port, or `sshd` isn't running. |
| no answer from the host (switched off, wrong address, or a firewall) | Check the address and the firewall. |
| the host name could not be resolved / no network route to the host | Check the name or IP address and the network. |
| could not read the SSH host key of … | `ssh-keyscan` got no answer. Same causes as above. |
| key file … must be an absolute path to a private key Starlab can read | System installs can't read `/home`; put the key in Starlab's secrets folder, owned by `starlab`, mode 600. |
| Python X is not available on … / rtl_sdr is not installed on … | Install it on the host, or set *Python on the host* to the right program. |
| cannot write …/recordings on … | Check the work folder's owner and permissions on the host. |
| "not discovered yet" after a restart | Expected: remote discovery results are kept in memory. Press **Discover devices**. |

## No passes are predicted

| Shown | Fix |
|---|---|
| "Set the observatory's latitude and longitude" / "The observatory location is not set" | Settings → Observatory. |
| "no orbital elements for this satellite" | Turn on downloads in Settings → Satellites and refresh, or check the custom source address. |
| "elements are N days old …" | Refresh them when online. The shipped sample elements age; with downloads off, pass times drift by minutes. |
| No passes above the minimum | Lower *Ignore passes peaking below* (Settings → Satellites), or wait: some days have only low passes. |
| "downloading orbital elements is off" | Turn on *Download current orbital elements* first. |
| A refresh reports failures | Check internet access from the server and the source address. Cached elements are kept. |

## Automatic recording doesn't book anything

Work through this list; the rule's warnings and **Preview passes** (Scheduler → Automatic rules) usually say which
item it is.

1. **Settings → Scheduling is manual.** Rules then only produce PREDICTED jobs. Switch to hybrid.
2. **The rule is disabled.** New rules start disabled.
3. **The receiver isn't verified**: "has not passed a hardware test … no automatic recording on it until it is
   verified". Verify it.
4. **The receiver isn't connected**, or is **busy** with another job or an SDR++ session.
5. **The receiver can't use the settings**: "cannot take these passes: …" (frequency, rate, format, gain profile).
6. **Another schedule owns the receiver**: "automatic passes are planned from observatory.toml" or "is recorded by
   … (legacy/shadow mode)". Rules never use such receivers.
7. **The pass is too low** (below the rule's minimum peak elevation) or **already recorded** by another job.
8. **No location or no elements**: see [No passes are predicted](#no-passes-are-predicted).
9. **Storage is short.**
10. It's further ahead than *Book automatic passes this far ahead* (Settings → Scheduling, default 48 h).

Press **Re-plan now** after changing anything.

## Storage

- **"must be under one of the allowed output roots"**: storage folders in Settings → Storage must lie under
  `allowed_output_roots` in `starlab.toml` (by default the data folder).
- **A job is refused for storage**: it would leave less than *Always keep free* (Settings → Storage), counting
  space already committed to booked jobs. Free space, lower the reserve, or move recordings.
- **Recordings disappear**: raw IQ is deleted by the retention policies in Settings → Storage (automatic recordings
  after 48 hours by default). Mark a recording **Keep**, or turn off *Delete raw IQ by the policies below*. See
  [ARCHIVE](ARCHIVE.md).

### Recordings can't be written to another disk

The system service may only write under its data folder (`ProtectSystem=strict`, `ReadWritePaths=<data folder>`).
To record onto another disk, for example `/mnt/bigdisk/starlab`:

1. Create the folder, owned by `starlab`: `sudo install -d -o starlab -g starlab /mnt/bigdisk/starlab`.
2. Allow it in `starlab.toml` under `[scheduler]`:
   `allowed_output_roots = ["/var/lib/starlab", "/mnt/bigdisk/starlab"]`.
3. Let the service write there: `sudo systemctl edit starlab`, then add:

   ```ini
   [Service]
   ReadWritePaths=/mnt/bigdisk/starlab
   ```

4. `sudo systemctl restart starlab`, then set the folders in Settings → Storage.

Alternatively, install with `--data-dir` on the big disk from the start.

## Settings won't save

| Message | Meaning |
|---|---|
| "… changed since you loaded it (now version N); reload and redo the change" | Someone else saved meanwhile. Reload and redo your change. |
| "… was not saved: …" with field errors | Values are checked as a whole (types, ranges, time zone, paths under the allowed roots, no unknown keys). Nothing was stored; fix the fields listed. |
| "applies after a broker restart" | Some integration settings are read at start: `sudo systemctl restart starlab`, when nothing is recording. |
| "observatory.toml says something else" | A setting in the database differs from `starlab.toml`. The database wins; the file's value is only shown for reference. |

Unsaved edits survive a page refresh as a draft in that browser.

## SDR++ sessions are refused

The session page lists the reasons. Common ones:

- bubblewrap or SDR++ isn't installed (`sudo apt install bubblewrap`, plus SDR++);
- SDRplay receivers are never offered (they can't be isolated);
- the receiver is behind an antenna switch, in use, leased, already in a session, or reserved now;
- a booking starts too soon, and *End before booked recordings* is off.

## Database and upgrades

- **`starlab db status` says "will be migrated at the next start (backup first)"**: normal after installing a newer
  version without restarting. Restart Starlab.
- **"NEWER than this code: refuse to start"**: see [The service doesn't start](#the-service-doesnt-start).
- **`starlab db restore` says the broker is running**: stop it with `sudo systemctl stop starlab` first. Always stop
  the service yourself before restoring, even if no warning appears. See [Backup and restore](backup-and-restore.md).
- **"… is not a Starlab database (no schema version)"**: the file isn't a Starlab backup.
- **"… is schema N, newer than this code"**: that backup came from a newer Starlab. Install that version to use it.
