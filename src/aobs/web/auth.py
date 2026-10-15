"""Signed, expiring sign-in links for the web pages (no passwords stored).

A link is valid for 15 minutes; opening it sets a 30-day session cookie in that browser. The
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


def login_url(config: Config, base_url: str, ttl_s: int = LINK_S) -> str:
    exp = int(time.time()) + ttl_s
    return f"{base_url.rstrip('/')}/rf/login/{exp}/{sign(config, 'login', exp)}"
