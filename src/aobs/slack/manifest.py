"""Slack app manifest for the features that are switched on, and nothing more.

Scopes:
  chat:write           any notification, and replies to commands in HTTP mode
  files:write          decoded image notifications (images are uploaded to the channel)
  commands             the /starlab slash command
Socket Mode (default) needs no public address: the app opens an outbound connection with its
App-Level Token (scope connections:write, granted on that token, not in the manifest). HTTP mode needs a
public HTTPS address for the slash command; requests are verified with the Signing Secret.
"""

from __future__ import annotations

import re
from typing import Any

NOTIFY_KEYS = ("notify_upcoming", "notify_recording_start", "notify_completed", "notify_failed",
               "notify_receiver_offline", "notify_storage", "notify_decoded_images")


def scopes(cfg: dict[str, Any]) -> list[str]:
    out = []
    if any(cfg.get(k) for k in NOTIFY_KEYS) or (cfg.get("commands_enabled") and cfg.get("mode") == "http"):
        out.append("chat:write")
    if cfg.get("notify_decoded_images"):
        out.append("files:write")
    if cfg.get("commands_enabled"):
        out.append("commands")
    return out


def manifest(cfg: dict[str, Any], station: str) -> dict[str, Any]:
    name = re.sub(r"[^A-Za-z0-9 ._-]", "", str(cfg.get("app_name") or "Starlab"))[:35].strip() or "Starlab"
    socket = cfg.get("mode", "socket") != "http"
    m: dict[str, Any] = {
        "display_information": {"name": name, "description": f"Satellite passes, recordings and receivers from Starlab ({station[:60]})"[:139],
                                "background_color": "#1d2b4a"},
        "features": {"bot_user": {"display_name": name.lower().replace(" ", "-")[:80], "always_online": False}},
        "oauth_config": {"scopes": {"bot": scopes(cfg)}},
        "settings": {"org_deploy_enabled": False, "socket_mode_enabled": socket, "token_rotation_enabled": False},
    }
    if cfg.get("commands_enabled"):
        cmd = {"command": "/" + str(cfg.get("command") or "starlab").lstrip("/"),
               "description": "Observatory status, passes, receivers and recordings",
               "usage_hint": "status | passes | receivers | recordings | help", "should_escape": False}
        if not socket:
            cmd["url"] = str(cfg.get("public_base_url") or "https://YOUR-PUBLIC-ADDRESS").rstrip("/") + "/slack/commands"
        m["features"]["slash_commands"] = [cmd]
    return m


def problems(cfg: dict[str, Any]) -> list[str]:
    out = []
    if cfg.get("mode") == "http" and cfg.get("commands_enabled"):
        url = str(cfg.get("public_base_url") or "")
        if not url.startswith("https://"):
            out.append("HTTP mode needs a public https:// address for the slash command; Socket Mode does not")
    if not scopes(cfg):
        out.append("no notification or command is switched on, so the app would have nothing to do")
    return out
