# Hardware discovery on this computer and on remote hosts

How Starlab finds SDR receivers, identifies them and reaches other computers. User-facing summary:
`docs/receivers.md`.

## One snapshot, every host

`aobs.inventory.snapshot.SCRIPT` is a POSIX shell script that Starlab feeds to `sh -s`. It runs the same way on
this computer (a subprocess) and on a remote host (over SSH, on stdin, so nothing is installed there). It reads:

- `/sys/bus/usb/devices`: vendor, product, the serial and manufacturer strings the kernel read at enumeration,
  bus and device number, and the kernel driver of each interface. `usbfs` means a libusb program has claimed it.
- `/proc/<pid>/fd`: which processes hold a `/dev/bus/usb` node open, with `readlink` only, plus how many
  processes could not be inspected.
- `command -v` for the SDR tools, and `SoapySDRUtil --info`, which loads modules and opens no device.

It never opens a device node, so it is safe while recordings stream. `discovery.discover_snapshot()` classifies
the result exactly as local discovery does: model, driver support, in use or free, configured or not, and
warnings for missing or duplicate serials. A test shows that the script and the Python sysfs reader see the same
devices.

## Identify (opens the device)

`aobs.inventory.adapters` runs the vendor tools through the host (`LocalHost` or `SshHost`) and parses their
output. Mocked output for each tool is in `tests/test_hosts.py`. The service decides whether the tools may run:

1. the device must be visibly free (no holder, no claimed interface);
2. nothing may be booked on it (recordings and SDR++ sessions, this computer only);
3. SDRplay: no RSP recording may be in progress, and only the API's device list is read;
4. tools that open every device of a kind (`airspy_info`, `hackrf_info`) run only if all are free;
5. on a remote host the snapshot is taken again right before anything is opened;
6. one identification at a time.

Results are kept with the tool that produced each value. For a configured receiver they are stored in
`inv_receivers.identification`. They are informational and never change readiness. SoapySDR capabilities also
go to `capabilities` for SoapySDR receivers, as before.

## Device identity

- A hardware serial when the device has one: `identity_key = "sn:<serial>"`.
- A USB port when it has none: `identity_key = "port:<usb_path>"`. Port lookups never fall back to another
  port.
- A receiver is unique per (host, driver, identity_key) (schema 5, which rebuilt the table so two serial-less
  devices can both be receivers).
- Devices sharing a serial on one host are flagged and are never identified or added, because librtlsdr,
  SoapySDR and the capture tools open receivers by serial.
- The friendly name is free text. The receiver ID is what jobs and the diagram use.

## Remote hosts (SSH)

Stored in `inv_hosts` (schema 5): host name, port, user, an optional key file, the accepted `known_hosts` lines
with their fingerprints, and the last test. Changes are logged to `inv_events` as `host:<id>`.

- **Key authentication only.** Starlab creates its own key pair in `secrets/ssh/starlab_ed25519` (mode 600) and
  shows the public key to install in `~/.ssh/authorized_keys` on the host. An owner's key file can be named
  instead. There is no password field, `BatchMode=yes` stops ssh asking for one, and any request carrying a
  password is refused. Passwords and private keys never reach the database.
- **Host keys are always checked.** The connection runs with `-F /dev/null` (no user ssh config),
  `StrictHostKeyChecking=yes` and a `known_hosts` file written from the database for that host only. When a host
  is added, `ssh-keyscan` fetches its keys and the page shows their SHA256 fingerprints with the command to read
  them on the host. Only the keys the owner ticks are kept, and they are fetched again when saving, so a key that
  changed in between is not accepted.
- **Saved only after a test.** A test connection, which runs the snapshot, must succeed with checking on, or
  nothing is saved.
- **A changed key stops everything.** Every connection fails with an explanation until the owner compares the
  new key and accepts it (*Review the new host key*). The old key stays until the new one tests successfully.
- **sshd penalties.** OpenSSH 9.8+ briefly refuses an address right after a key scan or failed logins
  (PerSourcePenalties). Starlab explains that error and retries the first test once.

Remote receivers are in the scheduler's receiver list with their host; tests and recordings run over SSH (see
`docs/remote-receivers.md`). A remote serial can never match a local device.

## Development and tests never open hardware

This station has the real `rtl_test`, `airspy_info`, `hackrf_info` and `SoapySDRUtil` on its PATH. Every
device-opening command goes through `hosts.LocalHost` or `SshHost`, which refuse it in these cases:

- under pytest, whatever the environment says;
- with `STARLAB_DEV=1`;
- with `STARLAB_NO_HARDWARE=1`.

In those modes only a mock from the host's `path_prefix` runs, called by absolute path, so a missing mock never
falls through to the real program. This was added after a debug test outside `tests/` (no conftest) once ran
the real `airspy_info` against the station's idle Airspy on 2026-10-15.

## Limits

- No SDR++ sessions on remote hosts yet (recording and testing there work since brief 7).
- Remote discovery runs when asked (*Discover devices*). Results are kept in memory, so after a broker restart
  remote hosts show "not discovered yet" until discovered again.
- Configuration export does not include receiver hosts. They need their keys accepted again on a new station.
- Passive parts (antennas, filters, LNAs, cables) cannot be discovered. Bias-tee *hardware* cannot be detected,
  only whether the driver offers a switch.
