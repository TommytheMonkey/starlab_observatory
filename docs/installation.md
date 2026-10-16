# Installing Starlab Observatory

This guide covers installing, upgrading and removing Starlab Observatory on a Linux computer, with or without
SDR receivers. When the installer finishes, continue with [First-run setup](first-run-setup.md).

Starlab runs as a background service and you use it from a web browser. You don't need a receiver to try it:
**simulation mode** offers simulated receivers whose recordings are synthetic and labelled as such everywhere.

## Contents

- [What has been tested](#what-has-been-tested)
- [Requirements](#requirements)
- [Install](#install)
- [Installer options](#installer-options)
- [Signing in to the web page](#signing-in-to-the-web-page)
- [Receiver software (optional)](#receiver-software-optional)
- [USB permissions](#usb-permissions)
- [Running Starlab commands](#running-starlab-commands)
- [Where things are](#where-things-are)
- [Opening Starlab from another computer](#opening-starlab-from-another-computer)
- [Optional extras](#optional-extras)
- [Running the installer again](#running-the-installer-again)
- [Upgrading](#upgrading)
- [Uninstalling](#uninstalling)

## What has been tested

Please read this before relying on Starlab with your own hardware.

| | Status |
|---|---|
| Ubuntu 24.04, x86_64 | The installer and first-run wizard (simulation mode, a booking, rerun, upgrade, uninstall, reinstall, purge, per-user install) were tested in a clean container with no USB devices. |
| Debian 13, x86_64 | The same container test. |
| Raspberry Pi OS 13 (64-bit) and other ARM64 hardware | **Not tested by us.** The installer accepts it and the Python packages exist for ARM64, but nobody has run it there yet. |
| 32-bit ARM | Untested; the installer warns. numpy and scipy may need to build from source. |
| RTL-SDR, SDRplay RSP1B | Real-hardware recording has been exercised with these two only. |
| Airspy | A real Airspy was seen by discovery (USB listing). Identification and recording go through SoapySDR and have not been run on real hardware. |
| HackRF, LimeSDR, ADALM-Pluto, USRP, Airspy HF+ and other SoapySDR devices | Supported through SoapySDR in the code, but **not tested on real hardware**. |
| SDRplay RSP1A | Shares Starlab's RSP1B tables (same USB ID); not tested separately. |

## Requirements

| | |
|---|---|
| Operating system | Ubuntu 24.04 or newer, Debian 13, or Raspberry Pi OS 13 (64-bit). The installer warns on other distributions but continues. |
| Architecture | x86_64 or ARM64. |
| Python | 3.12 or newer, plus the `python3-venv` package. All three distributions above ship a new enough Python. |
| Disk | At least 2 GB free to install (the installer refuses less), and 10 GB or more for data (it warns below that). Recordings need far more: one satellite pass of raw IQ is 1 to 8 GB. |
| Network | Needed during installation to download Python packages. Afterwards it's only needed for optional things: current orbital elements, Google Calendar, Slack and remote receiver hosts. |
| systemd | Used to run Starlab as a service. Without it (in containers, for example) the installer prints the command to start Starlab yourself. |

Install the basics:

```bash
sudo apt update
sudo apt install python3 python3-venv git
```

## Install

1. Get the source:

   ```bash
   git clone https://github.com/<owner>/starlab.git    # or unpack a release archive
   cd starlab
   ```

2. Run the installer for the whole machine, as a service:

   ```bash
   sudo ./install.sh
   ```

   To install for your own account only, without root, run `./install.sh --user` instead.

   The installer asks one question: **how you'll open Starlab's web page.** The default, *from other computers on
   my home network, or over Tailscale*, is right for most people. If Tailscale is running on this computer, it
   also offers an HTTPS address for your Tailscale devices; say yes. See
   [Opening Starlab from another computer](remote-access.md).

3. Read the installer's output. It does the following, in order:

   1. **Checks the computer:** the OS and architecture, Python and its venv module, and free disk space. It lists
      which receiver tools it found, with the package to install for each missing one. A missing receiver tool is
      only a warning.
   2. **Creates a service account** (`starlab`, system installs only) and adds it to the `plugdev` and `dialout`
      groups for USB radio access.
   3. **Creates the folders:** `/opt/starlab` for the application, `/etc/starlab` for the configuration, and
      `/var/lib/starlab` for the database, recordings and a private `secrets/` folder (mode 700).
   4. **Installs Starlab** and its Python packages into a private environment, writes `starlab.toml`, and
      generates an API token in the secrets folder. Credentials never go into the source folder.
   5. **Creates the database.**
   6. **Installs and starts** the `starlab` systemd service, enabled at boot, and a short `starlab` command.
   7. **Tailscale:** recommends it if it's missing; if it's running, offers the HTTPS address.
   8. **Prints sign-in links**, one for each address other computers can use (Tailscale HTTPS, the computer's
      `.local` name, its IP address, its Tailscale name).

4. On your laptop (or on this computer), open the link that matches how you reach this computer. The
   [setup wizard](first-run-setup.md) opens. The links work for 24 hours, until setup is finished.

Nothing records after installation. Recordings start only when you book one, or when you turn on automatic
recording yourself. That takes an enabled rule, a verified receiver, and Scheduling set to automatic or hybrid
(see [Satellite scheduling](satellite-scheduling.md)).

### If the installer stops

| Message | What to do |
|---|---|
| `python3 is missing` / `Python's venv module is missing` | `sudo apt install python3 python3-venv` |
| `Python X is too old; Starlab needs 3.12 or newer` | Use Ubuntu 24.04+, Debian 13 or Raspberry Pi OS 13. |
| `only N GB free under …` | Free space, or choose another data folder with `--data-dir`. |
| `installing Starlab's Python packages failed` | Check the network connection. Or install `python3-numpy python3-scipy python3-skyfield` from apt and run the installer again (the environment can see system packages). |
| `a system install needs root` | Use `sudo ./install.sh`, or `./install.sh --user`. |
| `refusing to use /var itself; choose a subdirectory` | `--prefix`, `--config-dir` and `--data-dir` must be subfolders, never `/`, `/usr`, `/etc`, `/var`, `/opt`, `/home` or your home folder itself. |
| `paths must be absolute` | Give full paths starting with `/`. |

More in [Troubleshooting](troubleshooting.md).

## Installer options

Every path is configurable. Nothing assumes a particular user name or folder.

| Option | Meaning | Default (system / user) |
|---|---|---|
| `--system`, `--user` | Install for the whole machine, or for the current account only | `--system` as root, `--user` otherwise |
| `--prefix DIR` | Application and its Python environment | `/opt/starlab` / `~/.local/share/starlab/app` |
| `--config-dir DIR` | `starlab.toml` and `install.env` | `/etc/starlab` / `~/.config/starlab` |
| `--data-dir DIR` | Database, recordings, secrets | `/var/lib/starlab` / `~/.local/share/starlab/data` |
| `--service-user NAME` | Account the service runs as (system installs; created if missing) | `starlab` |
| `--access MODE` | `network`: other computers on private networks and Tailscale. `local`: this computer only. On a rerun, changes only the listen address (a backup of `starlab.toml` is kept) | asked; `network` with `--yes` |
| `--tailscale-https`, `--no-tailscale-https` | Publish Starlab at `https://<name>.<tailnet>.ts.net` with Tailscale Serve, or don't ask | asked when Tailscale is running |
| `--bind ADDR` | Listen address; overrides `--access` | `0.0.0.0` (network) or `127.0.0.1` (local) |
| `--port N` | Web and API port | `8751` |
| `--udev` | Add udev rules so the service account can open common USB SDRs | off |
| `--no-slack` | Skip the Slack Socket Mode package (`slack_sdk`), which is otherwise installed for the Slack setup | installed |
| `--no-service` | Don't install a systemd service | off |
| `--no-start` | Install the service but don't start it | off |
| `--upgrade` | Upgrade an existing installation (see [Upgrading](#upgrading)) | off |
| `--source DIR` | The source tree to install | the installer's own folder |
| `--yes` | Ask no questions; take the defaults | off |
| `--dry-run` | Only print what would be done | off |

Examples:

```bash
sudo ./install.sh --data-dir /srv/starlab --udev   # data on another disk, USB rules for SDRs
sudo ./install.sh --no-service                     # containers, or to run `starlab broker` yourself
./install.sh --user                                # no root needed
./install.sh --help                                # every option
```

For a user install, the service is a systemd user service. To keep it running while you're logged out, run
`sudo loginctl enable-linger $USER`.

## Signing in to the web page

Starlab has no passwords. You sign in with a link, made on the Starlab computer:

```bash
starlab web-link
```

It prints one link per address this computer can be reached at; open the one that matches how you reach it.

- Until setup is finished, a link is valid for **24 hours**; afterwards, for **15 minutes**.
- After you open it, that browser stays signed in for **30 days**.

Not at the Starlab computer? Log in to it with SSH, or use the Slack integration. Both are explained in
[Opening Starlab from another computer](remote-access.md#signing-in).

## Receiver software (optional)

Starlab uses your distribution's receiver tools. Install only what your hardware needs. With no receiver at all,
skip this section and use simulation mode.

| Receiver | Packages | Notes |
|---|---|---|
| RTL-SDR (RTL2832U) | `rtl-sdr` | Starlab needs `rtl_sdr` on the PATH. |
| Airspy R2 / Mini | `soapysdr-tools python3-soapysdr soapysdr-module-airspy` | Must be identified before it can be added (see [Receiver configuration](receiver-configuration.md)). |
| HackRF, LimeSDR, ADALM-Pluto, USRP B2xx, Airspy HF+ | `soapysdr-tools python3-soapysdr` plus the SoapySDR module for the radio (`soapysdr-module-all` installs every module) | Not tested on real hardware. |
| SDRplay RSP1A / RSP1B | SDRplay's API (from sdrplay.com, which installs its own service), then the SoapySDRPlay3 module, plus `soapysdr-tools python3-soapysdr` | Other RSP models are recognized but not supported for recording. |
| Decoding satellite images | SatDump (satdump.org) | Optional. Without it, recordings are still made and analysed; only decoding is unavailable. |
| Receivers on other computers | `openssh-client` | See [Remote receivers](remote-receivers.md). |
| SDR++ listening sessions | `sdrpp` and `bubblewrap` | Optional; see [Receiver configuration](receiver-configuration.md#sdr-listening-sessions). |

Starlab's Python environment is created with access to system packages, so `python3-soapysdr` from apt is
visible to it. After installing receiver software, use **Receivers → Discover devices** in the web page; you
don't need to restart Starlab.

## USB permissions

The service account must be able to open the USB device.

1. The installer adds the account to `plugdev`. Most distribution packages (for example `rtl-sdr`) ship udev rules
   that grant `plugdev` access.
2. If a receiver still can't be opened ("not allowed to access its USB device"), install Starlab's own rules:

   ```bash
   sudo ./install.sh --udev
   ```

   This writes `/etc/udev/rules.d/60-starlab-sdr.rules` for RTL-SDR, SDRplay, Airspy, Airspy HF+, HackRF,
   ADALM-Pluto, LimeSDR and USRP devices. **Unplug and replug the receivers once** afterwards.
3. RTL-SDR only: if the kernel's TV-tuner driver grabs the dongle, blacklist it, then replug the dongle (or reboot):

   ```bash
   echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/starlab-rtl.conf
   ```

## Running Starlab commands

The command-line program is called `starlab` (`aobs` is the same program under its older name). The installer
adds a short `starlab` command (`/usr/local/bin/starlab`; `~/.local/bin/starlab` for a user install) that runs it
as the service account, so that it can read the configuration, database and secrets. It asks for your password
(`sudo`) when needed.

```bash
starlab web-link
```

The long form, if you need it: `sudo -u starlab env STARLAB_CONFIG=/etc/starlab/starlab.toml /opt/starlab/venv/bin/starlab <command>`.

| Command | What it does |
|---|---|
| `starlab web-link` | Prints a sign-in link. |
| `starlab db status` | Schema version and backups. Read-only. |
| `starlab db backup` | Takes a database backup now. Safe while Starlab runs. |
| `starlab db list` | Lists the database backups. |
| `starlab db restore <file> --yes` | Replaces the database with a backup. Stop Starlab first. See [Backup and restore](backup-and-restore.md). |
| `starlab broker` | Runs Starlab in the foreground (what the service runs). |

## Where things are

| | System install | User install |
|---|---|---|
| Application | `/opt/starlab` | `~/.local/share/starlab/app` |
| Configuration | `/etc/starlab/starlab.toml` | `~/.config/starlab/starlab.toml` |
| Installer's memory | `/etc/starlab/install.env` | `~/.config/starlab/install.env` |
| Database | `/var/lib/starlab/db/starlab.sqlite3` | `~/.local/share/starlab/data/db/starlab.sqlite3` |
| Database backups | `/var/lib/starlab/db/backups/` | `…/data/db/backups/` |
| Recordings | `/var/lib/starlab/recordings/jobs` (manual), `…/recordings/auto` (automatic) | the same under `…/data/recordings` |
| Secrets (API token, SSH key, Slack tokens) | `/var/lib/starlab/secrets` (mode 700) | `…/data/secrets` |
| Logs | `journalctl -u starlab` | `journalctl --user -u starlab` |

`starlab.toml` holds only what Starlab needs before the database opens: the data folder, the listen address and
port, and the folders recordings may be written to (`allowed_output_roots`). Everything else is set in the web page
(**Settings**), stored in the database, and versioned.

The system service is hardened: it can write only under the data folder. To record onto another disk, either
install with `--data-dir` on that disk, or see
[Troubleshooting → Recordings can't be written](troubleshooting.md#recordings-cant-be-written-to-another-disk).

## Opening Starlab from another computer

By default Starlab answers this computer and other computers on private networks: your home network and Tailscale.
It refuses everyone else. Starlab has no HTTPS of its own; Tailscale provides it.

- **Tailscale (recommended):** a fixed HTTPS address you can open from anywhere, with no router changes.
- **Home network:** `http://<name>.local:8751` or `http://<IP address>:8751`.
- **This computer only:** `sudo ./install.sh --access local`, then use Tailscale HTTPS or an SSH tunnel.

The full guide, including how to set up SSH from macOS, Windows and Linux:
[Opening Starlab from another computer](remote-access.md). Never forward Starlab's port on your router.

## Optional extras

- **Slack notifications and commands:** see [Slack integration](slack-integration.md). Socket Mode needs the
  `slack_sdk` package, which the installer adds unless you pass `--no-slack`. If it failed (no network), rerun
  `sudo ./install.sh`: it adds the package even when Starlab itself is already up to date.
- **Google Calendar:** publishes bookings to a Google calendar. Set it up in the web page (setup wizard →
  *Integrations*, or **Settings → Integrations**): upload a Google service-account key, share the calendar with it,
  test, and turn it on. No files on the server and no restart. See [Google Calendar](google-calendar.md).

## Running the installer again

`install.sh` is safe to run again. It:

- reinstalls the application only when the source version changed;
- never overwrites `starlab.toml`, the API token or the database (an explicit `--access` or `--bind` changes only
  the listen address in `starlab.toml`, and keeps a backup copy next to it);
- repairs anything missing.

It remembers your earlier choices (folders, service account) in `install.env`; options given on the command line
win.

## Upgrading

Always upgrade with `--upgrade`, which takes a backup and stops Starlab before changing anything.

1. Pick a moment when nothing is recording or about to start (**Scheduler → Calendar**).
2. Get the new version:

   ```bash
   cd starlab
   git pull            # or unpack the new release next to the old one
   ```

3. Run the upgrade:

   ```bash
   sudo ./install.sh --upgrade        # ./install.sh --upgrade for a user install
   ```

   The upgrade:

   1. backs up the database (`db/backups/starlab-manual-<time>.sqlite3`). If the backup fails, it stops and
      changes nothing;
   2. stops the service and installs the new version;
   3. migrates the database if the new version needs a newer schema. Before the first migration step it takes
      another automatic backup, `starlab-pre-migration-v<old>-v<new>-<time>.sqlite3`;
   4. starts the service again.

4. Check the result:

   ```bash
   starlab db status                 # "state": "up to date"
   journalctl -u starlab -n 50       # "migrating database schema …" and no tracebacks
   ```

5. Open the web page and check that **Scheduler** shows your upcoming recordings.

Starlab refuses to start on a database that is **newer** than the code ("Database schema N is newer than this
code"). It never downgrades a database. Going back to an older version therefore also means restoring a backup
taken before the migration. See [Backup and restore → Rolling back an upgrade](backup-and-restore.md#rolling-back-an-upgrade).

## Uninstalling

```bash
sudo ./uninstall.sh            # removes the application, its service and udev rules; KEEPS config, database, recordings, secrets
sudo ./uninstall.sh --purge    # also deletes them; you type the data folder's path to confirm
./uninstall.sh --user          # the same for a user install
```

After a normal uninstall, running `install.sh` again picks up the kept configuration and data. A purge keeps the
`starlab` account; remove it with `sudo userdel starlab`. The uninstaller refuses to delete an application folder
that lacks the installer's marker file, and refuses to purge `/`, `/usr`, `/etc`, `/var`, `/opt`, `/home` or your
home folder.
