"""Configuration loading. TOML on disk, plain dicts in memory, a few typed accessors."""

from __future__ import annotations

import os
import pathlib
import tomllib
from dataclasses import dataclass
from typing import Any

DEFAULT_CONFIG = os.environ.get("AOBS_CONFIG", "/etc/autonomous-observatory/observatory.toml")
FALLBACK_CONFIGS = (
    pathlib.Path(__file__).resolve().parents[2] / "config" / "observatory.toml",
)


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
        return str(self.raw["site"].get("display_timezone", "America/New_York"))

    @property
    def host(self) -> str:
        return str(self.raw["site"]["host"])


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    candidates = [pathlib.Path(path)] if path else [pathlib.Path(DEFAULT_CONFIG), *FALLBACK_CONFIGS]
    for candidate in candidates:
        if candidate.is_file():
            with candidate.open("rb") as stream:
                raw = tomllib.load(stream)
            for required in ("site", "paths", "legacy", "forecast"):
                if required not in raw:
                    raise ConfigError(f"{candidate}: missing [{required}] section")
            return Config(raw=raw, path=candidate)
    raise ConfigError(f"No configuration file found (tried {', '.join(map(str, candidates))})")
