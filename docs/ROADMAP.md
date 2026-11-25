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

## Backlog

- **Per-booking receiver bandwidth (SDRplay).** Add an optional *Bandwidth* field to the recording form (and the
  form API, `schedview.FORM_KEYS` / `job_from_form`), offering the receiver's analog bandwidths
  (`hardware.SDRPLAY_BANDWIDTHS_HZ`, no wider than the sample rate). Left empty, the booking keeps today's default
  from `hardware.default_radio()`: at 2 MS/s and above the RSP1B (RX-01) gets **600 kHz**, although the hardware and
  driver accept more (the old automatic METEOR setup ran 1.536 MHz at 2 MS/s; the driver reports exactly what is
  asked). The 600 kHz cap came in with cb6925a (2026-10-15) without a recorded reason; keep it as the default until
  the reason is known. A wider bandwidth would also let the RSP1B hold the ISS 437 MHz downlinks in one capture
  (`schedview.group_fit` refuses it today). Requested 2026-11-13.
