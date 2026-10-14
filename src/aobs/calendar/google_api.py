"""Minimal Google Calendar v3 REST client.

Two auth modes:
  service_account (preferred for the unattended service): scope calendar.events.
    The account can only reach calendars explicitly shared with its email, so
    access is bounded by the share you grant ("Make changes to events").
  oauth: scope calendar.app.created - the app may only create calendars and
    manage events in calendars it created.
Credentials live in files with mode 0600 under the secrets directory; token
values are never logged or printed.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import random
import time
import urllib.parse
from typing import Any

LOG = logging.getLogger("aobs.calendar")
SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]
SA_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
API = "https://www.googleapis.com/calendar/v3"


class CalendarError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def _check_secret_file(path: pathlib.Path) -> None:
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(f"{path} must not be group/world accessible (mode {oct(mode)}); chmod 600 it")


def load_credentials(token_file: pathlib.Path) -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    _check_secret_file(token_file)
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _write_token(token_file, creds)
        else:
            raise RuntimeError("Stored Google credentials are invalid; run `aobs calendar auth` again")
    return creds


def load_service_account(key_file: pathlib.Path) -> Any:
    from google.oauth2 import service_account

    _check_secret_file(key_file)
    return service_account.Credentials.from_service_account_file(str(key_file), scopes=SA_SCOPES)


def credentials_from_config(cal: dict[str, Any]) -> Any:
    if cal.get("auth_mode", "service_account") == "service_account":
        return load_service_account(pathlib.Path(cal["service_account_file"]))
    return load_credentials(pathlib.Path(cal["token_file"]))


def _write_token(token_file: pathlib.Path, creds: Any) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = token_file.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(creds.to_json())
    os.replace(tmp, token_file)


def run_auth_flow(client_secret_file: pathlib.Path, token_file: pathlib.Path, port: int = 8085) -> None:
    """Interactive one-time OAuth consent. Prints a URL; open it in a browser that can reach
    localhost:<port> on this host (e.g. via `ssh -L 8085:localhost:8085 observatory`)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    _check_secret_file(client_secret_file)
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), SCOPES)
    creds = flow.run_local_server(host="localhost", port=port, open_browser=False,
                                  authorization_prompt_message="Open this URL in your browser:\n{url}\n",
                                  success_message="Authorization complete. You can close this tab.",
                                  prompt="consent", access_type="offline")
    _write_token(token_file, creds)


class CalendarClient:
    def __init__(self, credentials: Any, max_retries: int = 5):
        from google.auth.transport.requests import AuthorizedSession

        self.session = AuthorizedSession(credentials)
        self.max_retries = max_retries

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                 params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = API + path + (("?" + urllib.parse.urlencode(params, doseq=True)) if params else "")
        for attempt in range(self.max_retries + 1):
            response = self.session.request(method, url, data=None if body is None else json.dumps(body),
                                            headers={"Content-Type": "application/json"}, timeout=30)
            if response.status_code in (429, 500, 502, 503, 504) or (
                    response.status_code == 403 and "rateLimitExceeded" in response.text):
                if attempt < self.max_retries:
                    delay = min(60.0, (2 ** attempt) + random.random())
                    LOG.warning("Calendar API %s %s -> %s; retrying in %.1fs", method, path, response.status_code, delay)
                    time.sleep(delay)
                    continue
            if response.status_code >= 400:
                try:
                    message = response.json().get("error", {}).get("message", response.text[:300])
                except ValueError:
                    message = response.text[:300]
                raise CalendarError(response.status_code, message)
            return response.json() if response.content else {}
        raise CalendarError(599, "retries exhausted")

    # calendars
    def get_calendar(self, calendar_id: str) -> dict[str, Any]:
        """Verify access using events.list (works with the calendar.events scope); the
        response carries the calendar's title and our access role."""
        page = self._request("GET", f"/calendars/{urllib.parse.quote(calendar_id)}/events", params={"maxResults": 1})
        return {"id": calendar_id, "summary": page.get("summary"), "accessRole": page.get("accessRole"),
                "timeZone": page.get("timeZone")}

    def create_calendar(self, summary: str, time_zone: str) -> dict[str, Any]:
        return self._request("POST", "/calendars", {"summary": summary, "timeZone": time_zone,
                                                    "description": "Satellite recording sessions planned by the Autonomous Radio Observatory."})

    # events
    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"/calendars/{urllib.parse.quote(calendar_id)}/events/{event_id}")
        except CalendarError as exc:
            if exc.status in (404, 410):
                return None
            raise

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/calendars/{urllib.parse.quote(calendar_id)}/events", body)

    def update_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/calendars/{urllib.parse.quote(calendar_id)}/events/{event_id}", body)

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        try:
            self._request("DELETE", f"/calendars/{urllib.parse.quote(calendar_id)}/events/{event_id}")
        except CalendarError as exc:
            if exc.status not in (404, 410):
                raise

    def list_managed_events(self, calendar_id: str, time_min: str, time_max: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {"timeMin": time_min, "timeMax": time_max, "singleEvents": "true",
                                  "privateExtendedProperty": "aobs_schema=1", "showDeleted": "true", "maxResults": 250}
        while True:
            page = self._request("GET", f"/calendars/{urllib.parse.quote(calendar_id)}/events", params=params)
            items.extend(page.get("items", []))
            if not page.get("nextPageToken"):
                return items
            params["pageToken"] = page["nextPageToken"]
