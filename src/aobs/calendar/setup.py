"""Google Calendar set up from the web page: key upload, connection test, turn on and off without a restart.

The service-account key is uploaded in the browser and stored as <secrets>/google-calendar.json (mode 600). It is
never sent back: the page only sees the account's e-mail address, so the owner can share the calendar with it.

A station whose starlab.toml sets [calendar] auth_mode = "oauth" (or a service_account_file of its own) keeps
using those credentials; the upload is then not offered. Turning the calendar on saves the "integrations" settings
document and rebuilds the broker's calendar client in place.
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from typing import Any, Callable

from ..config import Config
from .google_api import CalendarError

KEY_NAME = "google-calendar.json"
MAX_KEY_BYTES = 64 * 1024
TEST_SUMMARY = "Starlab connection test (deleted automatically)"

SHARE_HELP = ("In Google Calendar, open the calendar's Settings and sharing → Share with specific people, add {email} "
              "and choose \"Make changes to events\".")
READ_ONLY_HELP = ("Starlab can see the calendar but may not add events. Share it with {email} with \"Make changes to "
                  "events\", not \"See all event details\". If that permission is greyed out or the share stays "
                  "read-only, a Google Workspace policy limits sharing with accounts outside your organization (service "
                  "accounts count as outside); a Workspace administrator can allow it, or use a calendar in a personal "
                  "Google account.")


class CalendarSetupError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def key_path(config: Config) -> pathlib.Path:
    return config.secrets_dir / KEY_NAME


def managed_by_file(config: Config) -> str | None:
    """Why the uploaded key is not used, when starlab.toml sets the calendar's credentials itself."""
    cal = config.section("calendar")
    if cal.get("auth_mode") == "oauth":
        return "oauth"
    if cal.get("service_account_file") and pathlib.Path(cal["service_account_file"]) != key_path(config):
        return "service_account_file"
    return None


def effective(config: Config) -> dict[str, Any]:
    """The [calendar] section with the web-upload defaults filled in (the TOML wins where it says something)."""
    cal = dict(config.section("calendar"))
    cal.setdefault("auth_mode", "service_account")
    if cal["auth_mode"] == "service_account":
        cal.setdefault("service_account_file", str(key_path(config)))
    return cal


def _creds(cal: dict[str, Any]) -> Any:
    from .google_api import credentials_from_config
    return credentials_from_config(cal)


def _client(creds: Any) -> Any:
    from .google_api import CalendarClient
    return CalendarClient(creds, max_retries=1)


def _broker_client(cal: dict[str, Any]) -> Any:
    from .google_api import CalendarClient
    return CalendarClient(_creds(cal))


# Tests replace these; nothing in the test suite reaches Google.
load_creds: Callable[[dict[str, Any]], Any] = _creds
make_client: Callable[[Any], Any] = _client
broker_client: Callable[[dict[str, Any]], Any] = _broker_client


def client_from_config(config: Config) -> tuple[Any, str | None]:
    """The broker's calendar client, or (None, None) when publishing is off or not configured. Raises when the
    calendar is on but its credentials cannot be loaded."""
    cal = effective(config)
    if not (config.section("scheduler").get("calendar") and cal.get("enabled") and cal.get("calendar_id")):
        return None, None
    return broker_client(cal), str(cal["calendar_id"])


# ------------------------------------------------------------------ the key file
def parse_key(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise CalendarSetupError("choose the key file (JSON) you downloaded from Google Cloud")
    if len(text.encode()) > MAX_KEY_BYTES:
        raise CalendarSetupError("that file is too large to be a Google service-account key")
    try:
        data = json.loads(text)
    except ValueError:
        raise CalendarSetupError("that file is not JSON; choose the key file Google Cloud downloaded "
                                 "(Service accounts → Keys → Add key → JSON)") from None
    if not isinstance(data, dict):
        raise CalendarSetupError("that file is not a Google service-account key")
    if data.get("type") != "service_account":
        kind = data.get("type") or ("an OAuth client file" if "installed" in data or "web" in data else "unknown")
        raise CalendarSetupError(f"that is not a service-account key (type: {kind}). In Google Cloud: IAM & Admin → "
                                 "Service accounts → your account → Keys → Add key → Create new key → JSON")
    missing = [k for k in ("client_email", "private_key", "token_uri") if not data.get(k)]
    if missing:
        raise CalendarSetupError(f"the key file is incomplete (missing {', '.join(missing)}); download a new key")
    if "PRIVATE KEY" not in str(data["private_key"]):
        raise CalendarSetupError("the key file's private key is not readable; download a new key")
    return data


def store_key(config: Config, text: str) -> dict[str, Any]:
    why = managed_by_file(config)
    if why:
        raise CalendarSetupError("this Starlab's calendar credentials are set in its configuration file "
                                 f"({config.path}); remove them there to use an uploaded key instead", 409)
    data = parse_key(text)
    path = key_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{KEY_NAME}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return key_info(config)


def remove_key(config: Config) -> None:
    if config.section("calendar").get("enabled") and not managed_by_file(config):
        raise CalendarSetupError("turn the calendar off before removing its key", 409)
    key_path(config).unlink(missing_ok=True)


def key_info(config: Config) -> dict[str, Any]:
    """What the page may know about the key: never the private key."""
    path = key_path(config)
    if not path.exists():
        return {"present": False}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"present": True, "error": "the stored key file is unreadable; upload it again"}
    mode = path.stat().st_mode & 0o777
    return {"present": True, "client_email": data.get("client_email"), "project_id": data.get("project_id"),
            "private": not (mode & 0o077)}


# ------------------------------------------------------------------ test, on, off
def _email(config: Config) -> str:
    return str(key_info(config).get("client_email") or "the service account's e-mail address")


def explain(exc: CalendarError, config: Config) -> str:
    email = _email(config)
    if exc.status == 404:
        return ("Google found no calendar with that ID that this account can see. Check the Calendar ID (Settings and "
                "sharing → Integrate calendar → Calendar ID), then share the calendar with it. " + SHARE_HELP.format(email=email))
    if exc.status == 403:
        return READ_ONLY_HELP.format(email=email) + f" (Google said: {exc})"
    if exc.status in (400, 401):
        return f"Google refused the key ({exc}). The key may have been deleted or disabled in Google Cloud; upload a new one."
    return f"Google Calendar did not answer as expected: {exc}"


def test_connection(config: Config, calendar_id: str) -> dict[str, Any]:
    """Can Starlab write to this calendar? Reads it, then creates and deletes one test event."""
    checks: list[dict[str, Any]] = []
    calendar_id = (calendar_id or "").strip()

    def out(ok: bool, **extra: Any) -> dict[str, Any]:
        return {"ok": ok, "calendar_id": calendar_id, "checks": checks, **extra}
    cal = effective(config)
    if not calendar_id:
        checks.append({"check": "Calendar ID", "ok": False, "detail": "enter the calendar's ID first"})
        return out(False)
    if cal["auth_mode"] == "service_account" and not pathlib.Path(cal["service_account_file"]).exists():
        checks.append({"check": "Key file", "ok": False, "detail": "upload the service-account key first"})
        return out(False)
    try:
        client = make_client(load_creds(cal))
    except Exception as exc:  # noqa: BLE001 - a broken key file must become a message, not a 500
        checks.append({"check": "Credentials", "ok": False, "detail": f"the credentials could not be loaded: {exc}"})
        return out(False)
    checks.append({"check": "Credentials", "ok": True, "detail": "loaded" + (
        f" ({_email(config)})" if cal["auth_mode"] == "service_account" else " (OAuth, from the configuration file)")})
    try:
        info = client.get_calendar(calendar_id)
    except CalendarError as exc:
        checks.append({"check": "Calendar", "ok": False, "detail": explain(exc, config)})
        return out(False)
    except Exception as exc:  # noqa: BLE001 - network trouble
        checks.append({"check": "Calendar", "ok": False, "detail": f"could not reach Google Calendar: {exc}"})
        return out(False)
    role = info.get("accessRole")
    checks.append({"check": "Calendar", "ok": True, "detail": f"found \"{info.get('summary') or calendar_id}\" "
                                                              f"(access: {role or 'unknown'})"})
    if role in ("reader", "freeBusyReader"):
        checks.append({"check": "Permission to add events", "ok": False, "detail": READ_ONLY_HELP.format(email=_email(config))})
        return out(False, summary=info.get("summary"), access_role=role)
    body = {"summary": TEST_SUMMARY, "start": {"date": "2000-01-01"}, "end": {"date": "2000-01-02"},
            "transparency": "transparent", "description": "Starlab checked that it can add events. Safe to delete."}
    try:
        made = client.insert_event(calendar_id, body)
    except CalendarError as exc:
        checks.append({"check": "Permission to add events", "ok": False, "detail": explain(exc, config)})
        return out(False, summary=info.get("summary"), access_role=role)
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "Permission to add events", "ok": False, "detail": f"could not reach Google Calendar: {exc}"})
        return out(False, summary=info.get("summary"), access_role=role)
    try:
        client.delete_event(calendar_id, made["id"])
        checks.append({"check": "Permission to add events", "ok": True, "detail": "added and removed a test event"})
    except Exception as exc:  # noqa: BLE001 - writing works; a leftover test event is harmless
        checks.append({"check": "Permission to add events", "ok": True,
                       "detail": f"added a test event, but could not remove it ({exc}); delete \"{TEST_SUMMARY}\" on 1 Jan 2000"})
    return out(True, summary=info.get("summary"), access_role=role)


def _save_integrations(s: Any, author: str, note: str, **values: Any) -> dict[str, Any]:
    st = s.config_store
    cur = st.current("integrations")
    data = {**(cur["data"] if cur else {}), **values}
    return st.save("integrations", data, cur["version"] if cur else None, author, note, config=s.config)


def turn_on(s: Any, calendar_id: str, author: str) -> dict[str, Any]:
    res = test_connection(s.config, calendar_id)
    if not res["ok"]:
        return {"ok": False, "test": res}
    _save_integrations(s, author, "Google Calendar turned on", calendar_enabled=True,
                       calendar_id=res["calendar_id"], publish_jobs_to_calendar=True)
    return {"ok": True, "test": res}


def turn_off(s: Any, author: str) -> dict[str, Any]:
    _save_integrations(s, author, "Google Calendar turned off", calendar_enabled=False, publish_jobs_to_calendar=False)
    return {"ok": True}


def status(config: Config, service: Any = None) -> dict[str, Any]:
    cal = effective(config)
    why = managed_by_file(config)
    last = dict(getattr(service, "last_calendar", {}) or {}) if service is not None else {}
    return {"enabled": bool(cal.get("enabled")), "publish": bool(config.section("scheduler").get("calendar")),
            "calendar_id": cal.get("calendar_id") or "", "auth_mode": cal["auth_mode"],
            "credentials_in_config_file": why, "config_file": str(config.path) if why else None,
            "key": key_info(config) if not why else None,
            "running": bool(service is not None and getattr(service, "calendar_client", None)
                            and getattr(service, "calendar_id", None)),
            "client_error": getattr(service, "calendar_error", None) if service is not None else None,
            "last_sync": last or None}
