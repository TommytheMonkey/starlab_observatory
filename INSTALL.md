# Installing Starlab Observatory

Starlab runs on a Linux computer and is used from a web browser. It needs no receiver to try out: simulation
mode offers simulated receivers whose recordings are synthetic and labelled as such everywhere.

## Requirements

| | |
|---|---|
| Operating system | Debian 13, Ubuntu 24.04 or newer, or Raspberry Pi OS 13 (Trixie). Other distributions may work; the installer warns. |
| Architecture | x86_64 or ARM64 (Raspberry Pi 4/5 with a 64-bit OS). 32-bit ARM is untested. |
| Python | 3.12 or newer, with `python3-venv` |
| Disk | 2 GB to install; recordings need much more (one satellite pass of raw IQ is 1–8 GB) |
| Network | During installation, to download Python packages. Afterwards only for optional things (current orbital elements, calendar). |

Receivers (optional), from your distribution:

| Receiver | Packages |
|---|---|
| RTL-SDR | `rtl-sdr` |
| Airspy, HackRF, LimeSDR, PlutoSDR, USRP… | `soapysdr-tools python3-soapysdr` and the module for the radio, e.g. `soapysdr-module-airspy` |
| SDRplay RSP1A/RSP1B | SDRplay's API from sdrplay.com, then SoapySDRPlay3 |
| Decoding satellite images | SatDump (satdump.org) |

## Install

```bash
git clone https://github.com/<you>/starlab.git   # or unpack a release archive
cd starlab
sudo ./install.sh                 # for the whole machine, as a service
```

The installer:
1. **Checks the computer.** It checks the OS and architecture, Python and its venv module, and free disk space.
   It lists which receiver tools are present, with the package to install for each one that's missing.
2. **Creates a service account** (`starlab`) and adds it to `plugdev` and `dialout` for USB radio access.
3. **Creates the folders:**
   - `/opt/starlab`: the application and its private Python environment;
   - `/etc/starlab`: `starlab.toml`;
   - `/var/lib/starlab`: database, recordings, and a private `secrets/` folder.
4. **Installs Starlab** and its Python packages into that environment, writes `starlab.toml` and generates an
   API token. Credentials never go into the source folder.
5. **Creates the database.**
6. **Installs and starts** the `starlab` systemd service.
7. **Prints the address and a sign-in link.**

Open the link in a browser on the same computer. The **setup wizard** walks through the station's name and
location, receivers (or simulation), satellites, scheduling and a final check. You can leave and come back:
progress is saved. Nothing records until you book a recording or explicitly enable automatic recording on a
receiver that passed a hardware test.

### Options

Every path is configurable; nothing assumes a particular user name or folder.

```bash
sudo ./install.sh --data-dir /srv/starlab --bind 0.0.0.0 --port 8751 --udev
./install.sh --user                          # no root: ~/.local/share/starlab and ~/.config/starlab
sudo ./install.sh --no-service               # containers, or to run `starlab broker` yourself
./install.sh --help                          # all options
```

- `--udev` adds udev rules so the service account can open common USB SDRs; replug them once afterwards.
- `--bind 0.0.0.0` makes Starlab reachable from other machines. It has no HTTPS of its own, so prefer a VPN or
  a reverse proxy with HTTPS.

### Signing in

Starlab has no passwords. A sign-in link is valid for 15 minutes, and the browser then stays signed in for 30
days. Get a new link on the server any time:

```bash
sudo -u starlab STARLAB_CONFIG=/etc/starlab/starlab.toml /opt/starlab/venv/bin/starlab web-link
```

## Running the installer again

`install.sh` is safe to rerun. It:
- reinstalls the application only when the source version changed;
- never overwrites `starlab.toml`, the API token or the database;
- repairs anything missing.

## Upgrading

```bash
cd starlab && git pull            # or unpack the new release
sudo ./install.sh --upgrade
```

The upgrade:
1. backs up the database (`/var/lib/starlab/db/backups/`);
2. stops the service and installs the new version;
3. migrates the database (taking another automatic backup);
4. starts the service again.

To go back: reinstall the previous release with `--upgrade`, then restore its backup with the service stopped:

```bash
sudo systemctl stop starlab
sudo -u starlab STARLAB_CONFIG=/etc/starlab/starlab.toml /opt/starlab/venv/bin/starlab db list
sudo -u starlab STARLAB_CONFIG=/etc/starlab/starlab.toml /opt/starlab/venv/bin/starlab db restore <file> --yes
sudo systemctl start starlab
```

The original observatory station (the git checkout with `config/observatory.toml` and the legacy recorders)
upgrades differently; see `docs/release-checklist.md`.

## Uninstalling

```bash
sudo ./uninstall.sh               # removes the application and its service; KEEPS config, database, recordings, secrets
sudo ./uninstall.sh --purge       # also deletes them (asks you to type the data folder's path)
```

After a normal uninstall, `install.sh` picks the kept data up again.

## Simulation mode

Turn it on in the setup wizard (Hardware discovery) or in Settings → Simulation. Receivers → Discovered
hardware then lists two simulated receivers. Add one and you can:

- **verify it:** the test runs through the simulator and the receiver becomes *Simulated*, never *Verified*;
- **run signal tests**;
- **look at predicted passes and book one on it:** the recording is synthetic noise with a weak carrier, labelled
  *simulated* in the results, on the calendar and in recordings.

Simulated receivers never stand in for hardware: a real receiver is *Verified* only after a test captured real
IQ samples from it.

## Where things are

| | System install | User install |
|---|---|---|
| Application | `/opt/starlab` | `~/.local/share/starlab/app` |
| Configuration | `/etc/starlab/starlab.toml` | `~/.config/starlab/starlab.toml` |
| Data | `/var/lib/starlab` | `~/.local/share/starlab/data` |
| Logs | `journalctl -u starlab` | `journalctl --user -u starlab` |

## Testing the installer

`tests/install/container-test.sh` runs the whole thing in a throwaway Ubuntu container with no USB devices:
1. a fresh install with a custom data folder;
2. the first-run wizard over HTTP, in simulation mode, including a booking;
3. a rerun, an upgrade, an uninstall that keeps data, a reinstall, and a purge;
4. a per-user install as a different account.

It needs Docker and network access, and never touches the machine's own Starlab.
