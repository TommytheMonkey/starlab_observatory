# Installing Starlab Observatory

The installation guide is in **[docs/installation.md](docs/installation.md)**.

Quick start on Ubuntu 24.04+, Debian 13 or Raspberry Pi OS 13 (64-bit):

```bash
sudo apt install python3 python3-venv git
git clone https://github.com/<owner>/starlab.git && cd starlab
sudo ./install.sh
```

Then open the printed sign-in link and follow the setup wizard. No receiver is needed: simulation mode lets you try
everything first.

More guides:

| Guide | Covers |
|---|---|
| [Installation](docs/installation.md) | Requirements, what's been tested, options, upgrading, uninstalling |
| [First-run setup](docs/first-run-setup.md) | The setup wizard and simulation mode |
| [Receiver configuration](docs/receiver-configuration.md) | Supported SDRs; discover, identify, add and verify receivers |
| [Remote receivers](docs/remote-receivers.md) | Receivers on other Linux computers over SSH |
| [Satellite scheduling](docs/satellite-scheduling.md) | Pass prediction, manual bookings, automatic rules |
| [Backup and restore](docs/backup-and-restore.md) | Database backups, configuration export/import, rolling back an upgrade |
| [Troubleshooting](docs/troubleshooting.md) | Messages and what to do about them |
| [Slack integration](docs/slack-integration.md) | Optional notifications and commands |
| [Recording archive](docs/ARCHIVE.md) | Recordings, decoding, storage and retention |

Developers: `tests/install/container-test.sh [image]` runs a full install, the setup wizard in simulation mode, a
rerun, an upgrade, an uninstall and a purge in a throwaway container with no USB devices (needs Docker).
