"""Configuration loading. TOML on disk, plain dicts in memory, a few typed accessors.

The TOML is the bootstrap file: what is needed before the database can be opened (paths, listen
address, credential file locations) and, on stations that still run the original recorders, the
legacy integration. Everything a user edits in the web UI lives in the database (settings.store).

Search order: --config, $STARLAB_CONFIG, $AOBS_CONFIG, /etc/starlab/starlab.toml,
~/.config/starlab/starlab.toml, /etc/autonomous-observatory/observatory.toml, and last the
checkout's own config/observatory.toml (the original station's layout; a fresh install never uses it).
"""

from __future__ import annotations

import copy
import os
import pathlib
import tomllib
from dataclasses import dataclass
from typing import Any

DEFAULT_CONFIG = os.environ.get("STARLAB_CONFIG") or os.environ.get("AOBS_CONFIG") or ""
SYSTEM_CONFIGS = (
    pathlib.Path("/etc/starlab/starlab.toml"),
    pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config") / "starlab" / "starlab.toml",
    pathlib.Path("/etc/autonomous-observatory/observatory.toml"),
)
FALLBACK_CONFIGS = (
    pathlib.Path(__file__).resolve().parents[2] / "config" / "observatory.toml",
)

# Defaults for every section a fresh install may leave out. Paths are filled in from paths.data_root.
DEFAULTS: dict[str, Any] = {
    "site": {"name": "Starlab Observatory", "host": "localhost", "display_timezone": "UTC"},
    "legacy": {"enabled": False},
    "forecast": {"horizon_hours": 72, "max_tle_age_hours_ok": 72, "max_tle_age_hours_warn": 336},
    "readiness": {"recorder_agreement_tolerance_s": 90, "min_extra_free_gib": 10, "conflict_imminent_minutes": 30},
    "outcome": {"grace_minutes": 5, "min_coverage": 0.9},
    "calendar": {"enabled": False, "calendar_id": ""},
    "antenna_switch": {"base_url": ""},
    "broker": {"host": "127.0.0.1", "port": 8751, "receivers": []},
    "sessions": {"enabled": True, "port_range": [5259, 5299], "trusted_addresses": [], "allow_all_interfaces": False},
    "scheduler": {"enabled": True, "min_lead_seconds": 120, "max_duration_seconds": 14400, "max_horizon_days": 30,
                  "preempt_cutoff_seconds": 180, "legacy_guard_seconds": 60, "plan_interval_seconds": 300,
                  "auto_retention_hours": 48, "reserve_free_gib": 5, "calendar": False, "processing_gate_seconds": 120,
                  "notifications_disabled": [], "import_roots": [], "receivers": {}},
}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    path: pathlib.Path

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.raw.get(name, {}))

    @property
    def data_root(self) -> pathlib.Path:
        return pathlib.Path(self.raw["paths"]["data_root"])

    @property
    def database_url(self) -> str:
        return str(self.raw["paths"]["database_url"])

    @property
    def display_tz(self) -> str:
        return str(self.raw["site"].get("display_timezone", "UTC"))

    @property
    def host(self) -> str:
        return str(self.raw["site"].get("host", "localhost"))

    @property
    def secrets_dir(self) -> pathlib.Path:
        return pathlib.Path(self.raw["paths"]["secrets_dir"])

    @property
    def legacy_enabled(self) -> bool:
        """The original station's recorders (/opt/observatory-*) are integrated: only on that station."""
        return bool(self.raw.get("legacy", {}).get("enabled"))


def with_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(raw)
    paths = out.setdefault("paths", {})
    if "data_root" not in paths:
        raise ConfigError("[paths] data_root is required")
    root = pathlib.Path(paths["data_root"])
    paths.setdefault("database_url", f"sqlite:///{root / 'db' / 'starlab.sqlite3'}")
    paths.setdefault("secrets_dir", str(root / "secrets"))
    paths.setdefault("tle_cache", str(root / "tle"))
    leg = out.get("legacy")
    if leg is not None and "enabled" not in leg:
        leg["enabled"] = bool(leg.get("station_config"))   # stations configured before this flag existed
    for name, defaults in DEFAULTS.items():
        sec = out.setdefault(name, {})
        for k, v in defaults.items():
            sec.setdefault(k, copy.deepcopy(v))
    sched = out["scheduler"]
    sched.setdefault("allowed_output_roots", [str(root)])
    sched.setdefault("default_output_root", str(root / "recordings" / "jobs"))
    sched.setdefault("auto_output_root", str(root / "recordings" / "auto"))
    out["broker"].setdefault("token_file", str(pathlib.Path(paths["secrets_dir"]) / "api_token"))
    return out


def find_config(path: str | os.PathLike[str] | None = None) -> list[pathlib.Path]:
    if path:
        return [pathlib.Path(path)]
    if DEFAULT_CONFIG:
        return [pathlib.Path(DEFAULT_CONFIG)]
    return [*SYSTEM_CONFIGS, *FALLBACK_CONFIGS]


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    candidates = find_config(path)
    for candidate in candidates:
        if candidate.is_file():
            with candidate.open("rb") as stream:
                try:
                    raw = tomllib.load(stream)
                except tomllib.TOMLDecodeError as exc:
                    raise ConfigError(f"{candidate}: not valid TOML: {exc}") from exc
            return Config(raw=with_defaults(raw), path=candidate)
    raise ConfigError(f"No configuration file found (tried {', '.join(map(str, candidates))}). "
                      "Run the installer, or pass --config / set STARLAB_CONFIG.")
