"""Signed, expiring sign-in links for the web pages (no passwords stored).

A link is valid for 15 minutes; opening it sets a 30-day session cookie in that browser. Until the setup
wizard is finished, links last 24 hours instead (purpose "setup-login"), so the link the installer prints still
works after a coffee; those stop working as soon as setup is finished. The
signing key lives in <data>/secrets/rf_ui_key (created on first use, mode 0600). Anyone who can
run `starlab web-link` on the server, or ask the Slack bot, can get a link.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from ..config import Config

LINK_S = 15 * 60
SETUP_LINK_S = 24 * 3600
SESSION_S = 30 * 86400


def signing_key(config: Config) -> bytes:
    path = config.data_root / "secrets" / "rf_ui_key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip().encode()


def sign(config: Config, purpose: str, exp: int) -> str:
    return hmac.new(signing_key(config), f"{purpose}.{exp}".encode(), hashlib.sha256).hexdigest()[:40]


def login_url(config: Config, base_url: str, ttl_s: int = LINK_S, first_run: bool = False) -> str:
    """A sign-in link. first_run: valid 24 hours, but only while the setup wizard is unfinished."""
    if first_run:
        ttl_s = SETUP_LINK_S
    exp = int(time.time()) + ttl_s
    return f"{base_url.rstrip('/')}/rf/login/{exp}/{sign(config, 'setup-login' if first_run else 'login', exp)}"


def setup_completed(engine: object) -> bool:
    """Whether the setup wizard has been finished (an unreadable state counts as finished: the safe side)."""
    try:
        from ..settings.store import SettingsStore
        return bool(((SettingsStore(engine).current("setup") or {}).get("data") or {}).get("completed"))  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return True
