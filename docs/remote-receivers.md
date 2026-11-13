# Remote receivers

This guide covers receivers plugged into other Linux computers ("receiver hosts") that Starlab reaches over SSH:
how to prepare the host, register it, add its receivers, and choose whether recordings are copied back or kept on
the host. Receivers on the Starlab computer itself are covered in [Receiver configuration](receiver-configuration.md).

Example used below: a Raspberry Pi called `roof-pi` with an RTL-SDR, reached as user `starlab-rx`.

## How it works

- Starlab logs in with **an SSH key only**, never a password, and **checks the host's SSH key** on every connection.
- Discovery and Identify run the same read-only checks and tools on the host as on the Starlab computer. Nothing is
  installed on the host for discovery.
- To record, Starlab copies its recorder (a single Python file) to the host, starts it there detached (it survives
  SSH drops and a Starlab restart), watches it, then collects the result.
- After each recording the file is either **copied back** and verified by size and SHA-256 (the default), or
  **kept on the host** until you transfer it.

What remote receivers can and can't do:

| | Remote receiver |
|---|---|
| Discover, Identify, Add | yes |
| Verify / quick test | yes, over SSH |
| Manual and automatic recordings | yes |
| Wideband scans, gain sweeps, satellite tests | no: these run on the Starlab computer only |
| SDR++ listening sessions | no |
| Signal-path diagram (antenna switch, parts) | no: remote receivers aren't part of the Starlab computer's diagram |

## 1. Prepare the host

On `roof-pi` (any Linux with OpenSSH server, `/sys` and `/proc`):

1. Create the account Starlab will log in as, and let it open USB radios:

   ```bash
   sudo adduser --disabled-password starlab-rx
   sudo usermod -aG plugdev starlab-rx
   ```

2. Install what the recorder needs:

   | Receiver on the host | Packages |
   |---|---|
   | any | `python3 python3-numpy` (Python 3 with numpy) |
   | RTL-SDR | `rtl-sdr` (`rtl_sdr` must be on the PATH) |
   | SDRplay, Airspy, other SoapySDR | `python3-soapysdr` plus the radio's SoapySDR module (SDRplay: the SDRplay API and SoapySDRPlay3) |
   | keeping recordings on the host | `sha256sum` (coreutils, normally present) |

3. Make sure the receiver can be opened by that account: udev rules for the device, and for RTL-SDR the
   `dvb_usb_rtl28xxu` blacklist. See [Installation → USB permissions](installation.md#usb-permissions); the same
   rules apply on the host.
4. Make sure there's room: recordings go to `~/.starlab/recordings/<job>/` by default, and each needs the
   recording's estimated size plus 256 MB free at the start.

On the Starlab computer, `ssh`, `ssh-keygen` and `ssh-keyscan` (package `openssh-client`) must be installed.

## 2. Register the host

1. Open **Settings → Receiver hosts** (or **Receivers → Receiver hosts**) and press **Add a remote host…**.
2. Starlab shows **its public key** (created on first use, in `secrets/ssh/starlab_ed25519`). On `roof-pi`, add it
   for the login account:

   ```bash
   sudo -u starlab-rx mkdir -p -m 700 ~starlab-rx/.ssh
   echo 'ssh-ed25519 AAAA… starlab@yourserver' | sudo -u starlab-rx tee -a ~starlab-rx/.ssh/authorized_keys
   sudo chmod 600 ~starlab-rx/.ssh/authorized_keys
   ```

   You can name your own private key file instead (*Private key file on this computer*). It must be an absolute
   path the Starlab service account can read. A system install can't read files under `/home`, so keep the key in
   Starlab's secrets folder.
3. Fill in **Host ID** (2 to 32 letters, digits, `-` or `_`, not `local`; for example `roof-pi`), **Name**, **Host
   name or IP address**, **SSH port** and **User name** (`starlab-rx`).
4. Press **Fetch the host key**. Starlab lists the keys the host offers, with their SHA256 fingerprints.
5. **Compare them with the host.** On `roof-pi`, run:

   ```bash
   for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$f"; done
   ```

   Tick a key only if its fingerprint matches exactly.
6. Press **Test the connection and save**. Starlab fetches the keys again (so a key that changed meanwhile isn't
   accepted), logs in with checking on, and reads the host's USB list. **Nothing is saved unless this test
   succeeds.**

The host card then shows the accepted key, the last test (kernel, number of USB devices, SDR tools found) and the
number of configured receivers.

### What Starlab refuses

| Refusal | Why |
|---|---|
| Any password field | "Starlab does not take or store SSH passwords. Use key authentication." There is no password field, and ssh runs in batch mode so it never asks for one. |
| No fingerprint ticked | You must compare and accept at least one host key. |
| "the host is not offering any key you accepted now" | The key changed between fetching and saving. Check the fingerprints again. |
| "test connection failed: … Nothing was saved." | See [Troubleshooting → Remote hosts](troubleshooting.md#remote-receiver-hosts). |
| "… is already registered" | The same user, address and port is already a host. |
| Changing the address, port or user | Remove the host and add it again, so its key is checked again. Only the name, enabled state and recording settings can be changed. |
| Removing a host that has receivers | Remove its receivers first. |

Starlab uses its own `known_hosts` built from the accepted keys, and ignores `~/.ssh/config` and the system
`known_hosts`.

## 3. Add the host's receivers

1. On the host card, press **Discover devices**, or press **Discover devices** on **Receivers**, which looks on
   this computer and every enabled host.
2. The host's devices appear under *Discovered hardware*, grouped by host. Identify, add and verify them exactly as
   in [Receiver configuration](receiver-configuration.md#add-a-receiver). Identify takes a fresh look at the host's
   USB list right before opening anything.
3. **Verify** runs the quick test on the host over SSH. The receiver can be booked once it's verified.

Discovery results for remote hosts are kept in memory. After a Starlab restart, a host shows "not discovered yet"
until you discover again; the configured receivers are unaffected.

## 4. Recording settings per host

On the host card, under **Recordings**:

| Setting | Default | Meaning |
|---|---|---|
| **Recordings** (transfer) | *Copy here after each recording* | Each file is copied to the Starlab computer, verified by size and SHA-256, then deleted on the host. |
| | *Keep on the host* | Nothing large crosses the network. The size and SHA-256 are recorded and the job completes as "kept on <host>". Nothing is analysed or decoded automatically, and the IQ health check is skipped until you transfer the file (**Transfer** on the recording's page in **Recordings**). Retention can delete it on the host. |
| **Python on the host** | `python3` | Program name or path used to run the recorder. |
| **Work folder on the host** | `.starlab` | Where the recorder and recordings live. A relative path is relative to the login account's home. No `..`. |

Press **Save recording settings**. With *Copy here*, the Starlab computer also needs room for the copy. More on
transfers and retention: [ARCHIVE](ARCHIVE.md#receiver-hosts-keep-or-transfer).

## Before each recording

Starlab checks, in order, and refuses with a plain message if any check fails (nothing is held then):

1. the host answers over SSH;
2. the work folder is writable and has room for the recording plus 256 MB;
3. the Starlab computer has room for the copy;
4. Python 3 is there, with numpy (plus SoapySDR bindings for SoapySDR receivers), and `rtl_sdr` for RTL-SDR;
5. the receiver is on the host's USB and no other program is using it.

During the recording Starlab renews its reservation, rides out short SSH drops, and notices if the host rebooted
("the host restarted during the recording"). A recorder still running 90 seconds after its scheduled end is stopped.

## When the host's SSH key changes

If the host presents a different key (reinstalled, or something intercepting the connection), **every connection
stops** with "the host's SSH key is not the one you accepted … Nothing was run". To continue:

1. Find out why the key changed.
2. On the host card, press **Review the new host key**, compare the fingerprints with the host as in step 5 above,
   tick the matching key, and accept it.
3. Starlab tests the connection with the new key. The old key stays until the new one tests successfully.

## Not included in exports

Receiver hosts and their keys aren't part of the configuration export. On a new Starlab, add the hosts again and
accept their keys. See [Backup and restore](backup-and-restore.md).
