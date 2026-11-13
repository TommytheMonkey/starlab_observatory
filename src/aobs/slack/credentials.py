"""Slack credentials: a 0600 JSON file in the secrets folder, written atomically.

They are write-only through the API: the page learns only whether each one is set. They are never
logged, never part of the settings documents, and never in a configuration export (the export holds
settings documents only, and drops anything that looks like a secret anyway).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any

FIELDS = {
    "bot_token": (re.compile(r"^xoxb-[A-Za-z0-9-]{20,250}$"), "Bot User OAuth Token (starts with xoxb-)"),
    "signing_secret": (re.compile(r"^[0-9a-f]{32}$"), "Signing Secret (32 hexadecimal characters)"),
    "app_token": (re.compile(r"^xapp-[A-Za-z0-9-]{20,250}$"), "App-Level Token (starts with xapp-)"),
}


class CredentialError(ValueError):
    pass


class Credentials:
    def __init__(self, secrets_dir: pathlib.Path):
        self.path = pathlib.Path(secrets_dir) / "slack.json"

    def load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return {k: str(v) for k, v in data.items() if k in FIELDS and v}

    def status(self) -> dict[str, Any]:
        have = self.load()
        return {k: {"set": k in have, "label": label} for k, (_, label) in FIELDS.items()}

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Set (a value), keep (missing or "") or clear (null) each credential. Values are checked first."""
        unknown = set(changes) - set(FIELDS)
        if unknown:
            raise CredentialError(f"unknown credential(s): {', '.join(sorted(unknown))}")
        cur = self.load()
        for k, v in changes.items():
            if v is None:
                cur.pop(k, None)
                continue
            v = str(v).strip()
            if not v:
                continue
            rx, label = FIELDS[k]
            if not rx.match(v):
                raise CredentialError(f"that does not look like a {label}")
            cur[k] = v
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_name(".slack.json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(cur, fh)
        os.replace(tmp, self.path)
        return self.status()
