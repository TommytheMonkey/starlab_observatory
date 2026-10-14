"""Database schema (SQLAlchemy Core).

Portable by construction: only generic types, string primary keys or integer
autoincrement keys, JSON stored as Text, timestamps as timezone-aware UTC via
UTCDateTime. The same metadata creates the schema on SQLite now and on
PostgreSQL later (or seeds an Alembic baseline).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, MetaData, String, Table, Text,
    TypeDecorator, UniqueConstraint,
)

from ..timeutil import UTC

SCHEMA_VERSION = 1
metadata = MetaData()


class UTCDateTime(TypeDecorator[dt.datetime]):
    """Store naive UTC, always return aware UTC. Rejects naive input."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class JSONText(TypeDecorator[Any]):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        return None if value is None else json.dumps(value, sort_keys=True, default=str)

    def process_result_value(self, value: str | None, dialect: Any) -> Any:
        return None if value is None else json.loads(value)


def _ts() -> list[Column[Any]]:
    return [
        Column("created_at", UTCDateTime, nullable=False),
        Column("updated_at", UTCDateTime, nullable=False),
    ]


schema_info = Table(
    "schema_info", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", String(256), nullable=False),
)

# ---------------------------------------------------------------- inventory
receivers = Table(
    "receivers", metadata,
    Column("id", String(32), primary_key=True),          # OBS-R01 (== USB serial)
    Column("serial", String(64), nullable=False, unique=True),
    Column("hardware", String(128)),
    Column("host", String(64), nullable=False),
    Column("antenna_id", String(64)),
    Column("mission", String(256)),
    Column("min_hz", BigInteger),
    Column("max_hz", BigInteger),
    Column("present", Boolean),
    Column("last_seen_at", UTCDateTime),
    Column("notes", Text),
    *_ts(),
)

antennas = Table(
    "antennas", metadata,
    Column("id", String(64), primary_key=True),
    Column("description", String(256)),
    Column("min_hz", BigInteger),
    Column("max_hz", BigInteger),
    Column("switch_id", String(32)),
    Column("switch_port", Integer),
    Column("hardwired_receiver", String(32)),
    Column("notes", Text),
    *_ts(),
)

# --------------------------------------------------------- satellite ops
satellite_passes = Table(
    "satellite_passes", metadata,
    Column("id", String(64), primary_key=True),           # "57166-r12345" (NORAD + revolution)
    Column("norad_id", Integer, nullable=False),
    Column("satellite_name", String(64), nullable=False),
    Column("job_id", String(64), nullable=False),
    Column("revolution", Integer),
    Column("host", String(64), nullable=False),
    Column("receiver", String(32), nullable=False),
    Column("antenna", String(128)),
    Column("center_frequency_hz", BigInteger),
    Column("sample_rate", BigInteger),
    Column("gain_db", Float),
    # Geometry. aos/los are 0-degree horizon crossings; threshold_rise/set are the
    # min-elevation crossings the recorder actually schedules from.
    Column("aos", UTCDateTime),
    Column("los", UTCDateTime),
    Column("threshold_rise", UTCDateTime, nullable=False),
    Column("threshold_set", UTCDateTime, nullable=False),
    Column("min_elevation_deg", Float, nullable=False),
    Column("culmination", UTCDateTime, nullable=False),
    Column("max_elevation_deg", Float, nullable=False),
    Column("record_start", UTCDateTime, nullable=False),  # recorder window for this satellite
    Column("record_end", UTCDateTime, nullable=False),
    Column("capture_start", UTCDateTime, nullable=False), # merged window actually captured
    Column("capture_end", UTCDateTime, nullable=False),
    Column("capture_segments", JSONText),                 # planned rtl_sdr segments
    Column("predicted_quality", String(16)),
    Column("tle_epoch", UTCDateTime),
    Column("predicted_at", UTCDateTime, nullable=False),
    Column("in_forecast", Boolean, nullable=False, default=True),
    Column("removed_at", UTCDateTime),
    Column("status", String(16), nullable=False),
    Column("status_reason", Text),
    Column("readiness", JSONText),
    Column("readiness_checked_at", UTCDateTime),
    Column("actual_start", UTCDateTime),
    Column("actual_end", UTCDateTime),
    Column("recorded_bytes", BigInteger),
    Column("outcome", JSONText),
    *_ts(),
)
Index("ix_passes_record_start", satellite_passes.c.record_start)
Index("ix_passes_norad_culm", satellite_passes.c.norad_id, satellite_passes.c.culmination)

pass_status_history = Table(
    "pass_status_history", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pass_id", String(64), ForeignKey("satellite_passes.id"), nullable=False),
    Column("at", UTCDateTime, nullable=False),
    Column("old_status", String(16)),
    Column("new_status", String(16), nullable=False),
    Column("reason", Text),
)

recordings = Table(
    "recordings", metadata,
    Column("id", String(128), primary_key=True),          # recorder segment base name, or discovery capture id
    Column("kind", String(16), nullable=False),           # satellite | discovery
    Column("pass_id", String(64), ForeignKey("satellite_passes.id")),
    Column("receiver", String(32), nullable=False),
    Column("path", Text, nullable=False),
    Column("metadata_path", Text),
    Column("format", String(16)),
    Column("center_frequency_hz", BigInteger),
    Column("sample_rate", BigInteger),
    Column("started_at", UTCDateTime),
    Column("finished_at", UTCDateTime),
    Column("bytes", BigInteger),
    Column("expected_bytes", BigInteger),
    Column("status", String(16)),
    Column("owner", String(32), nullable=False),          # legacy-recorder | aobs
    Column("deleted_at", UTCDateTime),                    # observed missing (e.g. recorder retention)
    *_ts(),
)

calendar_events = Table(
    "calendar_events", metadata,
    Column("pass_id", String(64), ForeignKey("satellite_passes.id"), primary_key=True),
    Column("calendar_id", String(256), nullable=False),
    Column("event_id", String(128), nullable=False),
    Column("body_hash", String(64)),
    Column("remote_etag", String(128)),
    Column("last_synced_at", UTCDateTime),
    Column("last_error", Text),
    Column("state", String(16), nullable=False),          # active | cancelled
    *_ts(),
)

readiness_checks = Table(
    "readiness_checks", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pass_id", String(64), ForeignKey("satellite_passes.id")),
    Column("at", UTCDateTime, nullable=False),
    Column("verdict", String(16), nullable=False),
    Column("checks", JSONText, nullable=False),
)

# ------------------------------------------------------------- discovery
scan_profiles = Table(
    "scan_profiles", metadata,
    Column("id", String(64), primary_key=True),
    Column("definition", JSONText, nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    *_ts(),
)

scan_sessions = Table(
    "scan_sessions", metadata,
    Column("id", String(64), primary_key=True),
    Column("profile_id", String(64), ForeignKey("scan_profiles.id")),
    Column("receiver", String(32), nullable=False),
    Column("antenna", String(64)),
    Column("started_at", UTCDateTime, nullable=False),
    Column("ended_at", UTCDateTime),
    Column("end_reason", String(64)),
    Column("stats", JSONText),
)

signal_clusters = Table(
    "signal_clusters", metadata,
    Column("id", String(64), primary_key=True),
    Column("center_hz", BigInteger, nullable=False),
    Column("bandwidth_hz", BigInteger),
    Column("label", String(128)),
    Column("classification", String(32)),
    Column("confidence", Float),
    Column("evidence_level", String(16)),                 # DETECTED|CLASSIFIED|PROBABLE|VERIFIED
    Column("discovery_status", String(16)),               # new|known|investigate|excluded
    Column("first_seen", UTCDateTime, nullable=False),
    Column("last_seen", UTCDateTime, nullable=False),
    Column("observation_count", Integer, nullable=False, default=0),
    Column("notes", Text),
    *_ts(),
)
Index("ix_clusters_center", signal_clusters.c.center_hz)

signal_observations = Table(
    "signal_observations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(64), ForeignKey("scan_sessions.id")),
    Column("cluster_id", String(64), ForeignKey("signal_clusters.id")),
    Column("observed_at", UTCDateTime, nullable=False),
    Column("receiver", String(32), nullable=False),
    Column("antenna", String(64)),
    Column("center_hz", BigInteger, nullable=False),
    Column("bandwidth_hz", BigInteger),
    Column("power_db", Float),
    Column("noise_floor_db", Float),
    Column("snr_db", Float),
    Column("duration_s", Float),
    Column("recording_id", String(128), ForeignKey("recordings.id")),
)
Index("ix_obs_time", signal_observations.c.observed_at)
Index("ix_obs_center", signal_observations.c.center_hz)

analysis_jobs = Table(
    "analysis_jobs", metadata,
    Column("id", String(64), primary_key=True),
    Column("recording_id", String(128), ForeignKey("recordings.id"), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("state", String(16), nullable=False),          # queued|leased|done|failed|dead
    Column("attempts", Integer, nullable=False, default=0),
    Column("lease_owner", String(64)),
    Column("lease_expires_at", UTCDateTime),
    Column("not_before", UTCDateTime),
    Column("result", JSONText),
    Column("last_error", Text),
    *_ts(),
)
Index("ix_jobs_state", analysis_jobs.c.state, analysis_jobs.c.not_before)

decoder_attempts = Table(
    "decoder_attempts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("recording_id", String(128), ForeignKey("recordings.id"), nullable=False),
    Column("decoder", String(64), nullable=False),
    Column("decoder_version", String(64)),
    Column("started_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    Column("outcome", String(16), nullable=False),        # success|no_decode|incompatible|error
    Column("detail", JSONText),
    UniqueConstraint("recording_id", "decoder", "decoder_version", name="uq_attempt"),
)

decoded_artifacts = Table(
    "decoded_artifacts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("recording_id", String(128), ForeignKey("recordings.id")),
    Column("pass_id", String(64), ForeignKey("satellite_passes.id")),
    Column("decoder", String(64), nullable=False),
    Column("kind", String(32), nullable=False),           # image|text|audio|packets
    Column("path", Text, nullable=False),
    Column("verified", Boolean, nullable=False, default=False),
    Column("summary", Text),                              # untrusted decoded content, display-escaped
    Column("created_at", UTCDateTime, nullable=False),
)

# ------------------------------------------------------- ops / alerting
notifications = Table(
    "notifications", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("dedup_key", String(256), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("title", String(256), nullable=False),
    Column("body", Text),
    Column("link", Text),
    Column("created_at", UTCDateTime, nullable=False),
    Column("sent_at", UTCDateTime),
    Column("channel", String(32)),
    Column("suppressed", Boolean, nullable=False, default=False),
)
Index("ix_notif_dedup", notifications.c.dedup_key, notifications.c.created_at)

system_health = Table(
    "system_health", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", UTCDateTime, nullable=False),
    Column("component", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("detail", JSONText),
)
Index("ix_health_comp_at", system_health.c.component, system_health.c.at)

# ------------------------------------------------------- unified scheduler
# One row per reservation of a physical SDR: manual (Slack) jobs, broker-run
# automatic pass jobs, and windows imported from legacy recorders.
sched_jobs = Table(
    "sched_jobs", metadata,
    Column("job_id", String(64), primary_key=True),
    Column("job_name", String(128), nullable=False),
    Column("source", String(16), nullable=False),          # manual | auto | legacy
    Column("receiver_id", String(32), nullable=False),
    Column("device_serial", String(64), nullable=False),   # the lock key
    Column("status", String(16), nullable=False),
    Column("status_reason", Text),
    Column("executes", Boolean, nullable=False),           # false for legacy imports and shadow plans
    Column("reservation_start", UTCDateTime, nullable=False),
    Column("reservation_end", UTCDateTime, nullable=False),
    Column("record_start", UTCDateTime, nullable=False),
    Column("record_end", UTCDateTime, nullable=False),
    Column("pass_key", String(160)),                       # auto/legacy: stable pass identity
    Column("target", String(128)),
    Column("estimated_bytes", BigInteger),
    Column("spec", JSONText, nullable=False),               # normalized job as stored
    Column("paths", JSONText),
    Column("result", JSONText),
    Column("preempted_by", String(64)),
    Column("requested_by", String(128)),
    Column("pid", Integer),
    *_ts(),
    UniqueConstraint("source", "receiver_id", "pass_key", name="uq_sched_pass"),
)
Index("ix_sched_jobs_device_time", sched_jobs.c.device_serial, sched_jobs.c.reservation_start)
Index("ix_sched_jobs_status", sched_jobs.c.status)

sched_job_events = Table(
    "sched_job_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", String(64), ForeignKey("sched_jobs.job_id"), nullable=False),
    Column("at", UTCDateTime, nullable=False),
    Column("old_status", String(16)),
    Column("new_status", String(16), nullable=False),
    Column("reason", Text),
    Column("actor", String(64)),
)

# Outbox read by the Slack agent (cursor = id).
sched_notifications = Table(
    "sched_notifications", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", UTCDateTime, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("job_id", String(64)),
    Column("text", Text, nullable=False),
    Column("data", JSONText),
)

sched_calendar = Table(
    "sched_calendar", metadata,
    Column("job_id", String(64), ForeignKey("sched_jobs.job_id"), primary_key=True),
    Column("calendar_id", String(256), nullable=False),
    Column("event_id", String(128), nullable=False),
    Column("body_hash", String(64)),
    Column("last_error", Text),
    Column("synced_at", UTCDateTime),
)

# ------------------------------------------------------- processing attempts + artifact index
# Every decode/analysis run over a recording is an immutable attempt with its own output
# directory; re-running with other parameters creates a new attempt, never overwrites one.
proc_attempts = Table(
    "proc_attempts", metadata,
    Column("attempt_id", String(64), primary_key=True),
    Column("recording_id", String(64), ForeignKey("sched_jobs.job_id"), nullable=False),
    Column("kind", String(16), nullable=False),            # decode | analyze
    Column("params", JSONText, nullable=False),             # as requested
    Column("resolved", JSONText),                           # exact command / settings used
    Column("status", String(16), nullable=False),           # queued | running | paused | completed | failed | cancelled
    Column("status_reason", Text),
    Column("origin", String(32), nullable=False),           # post-recording | slack | api
    Column("requested_by", String(128)),
    Column("output_dir", Text, nullable=False),
    Column("pid", Integer),
    Column("result", JSONText),
    Column("queued_at", UTCDateTime, nullable=False),
    Column("started_at", UTCDateTime),
    Column("finished_at", UTCDateTime),
    Column("paused_seconds", Float),
)
Index("ix_proc_attempts_recording", proc_attempts.c.recording_id)
Index("ix_proc_attempts_status", proc_attempts.c.status)

artifacts = Table(
    "artifacts", metadata,
    Column("artifact_id", String(64), primary_key=True),
    Column("recording_id", String(64), ForeignKey("sched_jobs.job_id"), nullable=False),
    Column("attempt_id", String(64), ForeignKey("proc_attempts.attempt_id")),   # null = produced by the capture
    Column("kind", String(32), nullable=False),   # raw_iq | metadata | capture_log | waterfall | spectrum | image | product | diagnostics | log
    Column("path", Text, nullable=False),
    Column("bytes", BigInteger),
    Column("mime", String(64)),
    Column("label", String(256)),
    Column("created_at", UTCDateTime, nullable=False),
    Column("deleted_at", UTCDateTime),
    UniqueConstraint("path", name="uq_artifact_path"),
)
Index("ix_artifacts_recording", artifacts.c.recording_id, artifacts.c.kind)

# ------------------------------------------------------- RF signal paths (the drag-and-drop editor)
# Append-only: every save is a new version, so any recording's path can be traced to the layout
# that was in effect when it was made.
rf_layouts = Table(
    "rf_layouts", metadata,
    Column("version", Integer, primary_key=True),
    Column("layout", JSONText, nullable=False),     # {"components": {...}, "receivers": {...}}
    Column("author", String(128), nullable=False),
    Column("note", Text),
    Column("created_at", UTCDateTime, nullable=False),
)

# ------------------------------------------------------- signal-path tests (the /rf Test Lab)
# One row per test. The recording itself is an ordinary sched_jobs row (job_id), so it gets the broker's
# locking, conflict checks, IQ health and signal-path snapshot; this row adds what the test was for.
rf_tests = Table(
    "rf_tests", metadata,
    Column("test_id", String(64), primary_key=True),
    # quick | scan | gain_sweep | interference | satellite | filter_test | sat_compare
    Column("test_type", String(24), nullable=False, server_default="quick"),
    Column("job_id", String(64), ForeignKey("sched_jobs.job_id")),     # null: analysis of existing data only
    Column("receiver_id", String(32), nullable=False),
    Column("component_id", String(64)),                 # the part a filter test (etc.) is about
    Column("refs", JSONText),                           # other tests/recordings this one is built from
    Column("name", String(120)),
    Column("description", Text),
    Column("signal_type", String(32)),
    Column("tags", JSONText),
    Column("baseline_test_id", String(64)),
    Column("group_id", String(64)),
    Column("request", JSONText, nullable=False),        # parameters as the owner entered them, resolved
    Column("rf_path", JSONText),                        # path when the test was requested (capture start is in the job)
    Column("notes", Text),
    Column("result", JSONText),                         # summary cached once the analysis finished
    Column("created_by", String(128)),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)
Index("ix_rf_tests_receiver", rf_tests.c.receiver_id, rf_tests.c.created_at)

rf_test_presets = Table(
    "rf_test_presets", metadata,
    Column("preset_id", String(64), primary_key=True),
    Column("name", String(80), nullable=False),
    Column("receiver_id", String(32)),                  # null = any receiver
    Column("params", JSONText, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
)
