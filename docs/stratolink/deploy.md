# Deploying Stratolink and switching over from the manual receiver

Starlab's services are installed with sudo (see `docs/OPERATIONS.md`); these steps need it once.

## 1. Settings

`~/.config/starlab/stratolink.toml` (from `deploy/stratolink/stratolink.example.toml`). Set the **exact** position
of the observatory's gateway antenna (`lat`, `lon`, `alt_m`): every distance, bearing and elevation is measured from
it. The example's 35.48, -80.83 is only accurate to about 1 km.

## 2. Code

The daemon runs from `~/starlab-dev` (like the SSTV and pirate watches), so the branch must be merged there. The
`/stratolink` page is served by `starlab.service`, which runs the installed copy, so it also needs the usual upgrade:

```bash
cd ~/starlab-dev && git merge feature/stratolink       # or merge the PR and pull
sudo ~/starlab-dev/deploy/upgrade-from-head.sh          # installs committed HEAD into /opt/starlab, restarts starlab
```

## 3. Install the service (not started yet)

```bash
sudo install -m 644 ~/starlab-dev/deploy/systemd/starlab-stratolink.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable starlab-stratolink
```

## 4. Switch over from `receiver.py`

Only one program can own UDP 1700. The gateway keeps sending while nothing listens; it only misses
acknowledgements for a few seconds, and nothing is lost from `packets.jsonl`.

```bash
ss -ulpn | grep 1700                       # shows the manual python3 receiver.py
# stop it: Ctrl-C in its terminal (or kill <its pid>)
sudo systemctl start starlab-stratolink
ss -ulpn | grep 1700                       # now the Stratolink python process
journalctl -u starlab-stratolink -n 20      # "import ... packets.jsonl", "listening for Semtech UDP on 192.168.4.66:1700"
cd ~/starlab-dev && .venv/bin/python -m aobs.stratolink status
```

If the service started first, it logs `port_busy` and takes the port within 10 s of `receiver.py` stopping.

At start the daemon imports `~/stratolink/receiver/packets.jsonl` (once; repeated imports add nothing, receptions are
identified by gateway, concentrator counter and frame). Keep the file; `receiver.py` is no longer needed.

**Rolling back:** `sudo systemctl stop starlab-stratolink` and start `receiver.py` again. Nothing on the gateway
changes either way.

## 5. Check

Open Starlab → **Stratolink**. The header shows *RX listening*; Ground stations shows the gateway online with its
temperature and acknowledgement rate within 30 s (its status interval).

## Resource use

Measured in the sandbox (2026-11-27) with a packet every 5 s from two simulated stations plus page polling: under 1 s of CPU time in its first 6 minutes and 40 MB resident.
The unit caps it (`Nice=5`, `CPUWeight=50`, `MemoryMax=512M`). It opens no SDR and makes no broker leases.

## Upgrading to missions (branch `feature/stratolink-mission`)

Nothing is reconfigured on the gateway or the trackers. The database gains columns and two tables the first time the
new daemon opens it (`store.migrate`: additive and idempotent; existing receptions, packets, positions, telemetry,
trackers and gateways are untouched; flights become missions: planned → PREPARATION, active → ARMED,
ended → COMPLETE). Back it up first anyway.

```bash
sqlite3 /data/archive/stratolink/stratolink.sqlite3 ".backup /data/archive/stratolink/before-missions.sqlite3"
cd ~/starlab-dev && git fetch && git merge feature/stratolink-mission      # or merge the PR
sudo systemctl restart starlab-stratolink        # daemon: missions engine + new API (a few seconds without UDP ACKs)
sudo ~/starlab-dev/deploy/upgrade-from-head.sh   # starlab.service: serves the new /stratolink page
journalctl -u starlab-stratolink -n 20           # "listening for Semtech UDP", no tracebacks
```

The service restart is safe at any time: the gateway keeps sending and the receiver resumes within seconds; a
mission's state and timeline are in the database and the engine continues where it stopped. The page is read on each
request, so `upgrade-from-head.sh` is only needed once for the new HTML and proxy code (it refuses to run during a
recording, like every upgrade).

New settings (optional, in `stratolink.toml`): `prediction_url` (default SondeHub's Tawhiri; `""` turns prediction
off), `mission_step_s` (5), `private_channel` ("Stratolink"). Rollback: `git checkout` the previous commit,
restart the service, and restore the backup if you want the pre-mission database (the newer columns are ignored by
the old code otherwise, so restoring is optional).
