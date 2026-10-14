# Roadmap

| Phase | Status | Deliverables |
|---|---|---|
| 0 Inventory | ✅ done | docs/ENVIRONMENT.md |
| 1 Satellite ops | 🟡 built and tested; running as a dev process; calendar waits on a calendar share | forecast (72 h, recorder's predictor), readiness (10 checks), outcomes, DB, calendar sync (dry-run verified) |
| 1b Production install | ⏳ needs sudo + your OK | `/opt/autonomous-observatory`, `/etc/autonomous-observatory`, `aobs-satops.service` |
| 2 Discovery | ⏳ next | pass guard (with ISS on OBS-R02), broker, profiles, detector, IQ capture, quotas |
| 3 monkeybrain | ⏳ | SSH/Tailscale trust, queue API, worker, DSP classifier, decoder plugins (SatDump for METEOR first → DECODED status) |
| 4 Dashboard + notifications | ⏳ | FastAPI on :8750 (tailnet), webhooks/email, dedup and cooldown |
| 5 Advanced | ⏳ | noise-floor model, clustering, novelty, multi-receiver scheduling |

Phase 1 exit criteria: the calendar shows all 72 h passes, statuses move correctly through at least one real pass (READY → RECORDING → RECORDED), and the service survives a reboot.
