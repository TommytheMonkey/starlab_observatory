# Operations

## Everyday commands
```
aobs inventory                 # receivers, USB holders, ANTSW-01, leases (read-only)
aobs forecast                  # 72 h METEOR schedule with readiness (read-only)
aobs readiness --limit 3       # every check, with evidence
aobs sync-once [--calendar dry-run|apply]
aobs calendar plan --show 2    # preview rendered events (no writes)
aobs calendar setup --calendar-id <id>   # verifies access + writer role
aobs calendar sync
```
The dev workspace runs as `/home/tommy/auto-observe/.venv/bin/aobs` with `--config config/observatory.toml`.

## Enable Google Calendar (service account)
1. In Google Calendar, create a calendar named **Satellite Observatory** (time zone America/New_York).
2. Settings → Share with specific people → add `rf-observe@rf-observatory.iam.gserviceaccount.com` → **Make changes to events**.
3. Settings → Integrate calendar → copy the **Calendar ID**.
4. `aobs calendar setup --calendar-id <id>` should report `role=writer`.
5. Put the id in `calendar.calendar_id`, run `aobs calendar plan --show 1` and review it.
6. Set `calendar.enabled = true`. The next cycle creates the events, which the gate allows only if the recorder agrees with the forecast.

## Production install (requires sudo — not done yet)
```
sudo install -d -o tommy -g tommy /opt/autonomous-observatory /etc/autonomous-observatory
rsync -a --exclude .venv --exclude __pycache__ ~/auto-observe/ /opt/autonomous-observatory/
python3 -m venv --system-site-packages /opt/autonomous-observatory/.venv
/opt/autonomous-observatory/.venv/bin/pip install /opt/autonomous-observatory
cp /opt/autonomous-observatory/config/observatory.toml /etc/autonomous-observatory/
sudo cp /opt/autonomous-observatory/deploy/systemd/aobs-satops.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aobs-satops.service
```
This touches no existing unit or config. Rollback: `sudo systemctl disable --now aobs-satops && sudo rm /etc/systemd/system/aobs-satops.service`. The legacy recorder is unaffected either way, because satops only reads its files.

## Recovery
* **DB corrupt or lost**: stop the service, move the file aside, restart. The forecast rebuilds immediately. Calendar events are re-adopted through deterministic ids (409 → update), so no duplicates. Past outcomes whose recorder metadata was already pruned (> 6 h) are lost, so back up `db/` daily.
* **Calendar shows WARNING "no longer appears in the forecast"**: a TLE update moved or removed the pass. Check `aobs forecast`.
* **Readiness `recorder_agreement` FAIL**: the recorder and satops disagree. Compare `journalctl -u observatory-record@OBS-R01 -n 20` with `aobs forecast`. Usually the TLE caches differ; it resolves at the next refresh.
* **`no_scheduling_conflict` names sdrpp**: someone is streaming that receiver from SDR++. Stop playback before the pass.
* **ADS-B index drift FAIL**: do not restart observatory-adsb until fixed. It would grab the wrong receiver.

## Why each missed recording can be explained
`pass_status_history` (every transition with its reason), `readiness_checks` (all checks for every cycle), `satellite_passes.outcome` (per-segment status, bytes, rtl_sdr exit code, error), and the recorder's journal.
