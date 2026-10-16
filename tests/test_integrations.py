"""Integrations set up from the web page: the Google Calendar key upload, connection test and turning publishing on
and off without a restart, plus the shared integrations script. Every Google call goes to a fake."""

from __future__ import annotations

import json
from typing import Any

import pytest

from aobs.calendar import setup as cs
from aobs.calendar.google_api import CalendarError
from test_rflayout import http, rf  # noqa: F401
from test_scheduler import env  # noqa: F401

KEY = {"type": "service_account", "project_id": "starlab-test", "private_key_id": "abc",
       "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----\n",
       "client_email": "starlab@starlab-test.iam.gserviceaccount.com", "token_uri": "https://oauth2.googleapis.com/token"}
AUTH = {"Authorization": "Bearer t"}


class FakeCalendar:
    def __init__(self, role: str = "writer", get_error: int | None = None, insert_error: int | None = None):
        self.role, self.get_error, self.insert_error = role, get_error, insert_error
        self.inserted: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def get_calendar(self, calendar_id: str) -> dict[str, Any]:
        if self.get_error:
            raise CalendarError(self.get_error, "Not Found" if self.get_error == 404 else "Forbidden")
        return {"id": calendar_id, "summary": "Satellites", "accessRole": self.role}

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.insert_error:
            raise CalendarError(self.insert_error, "Forbidden")
        self.inserted.append(body)
        return {"id": f"ev{len(self.inserted)}", **body}

    def update_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return body

    def get_event(self, calendar_id: str, event_id: str) -> None:
        return None

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.deleted.append(event_id)


@pytest.fixture(autouse=True)
def fresh_install_calendar(env):
    """A fresh install's [calendar]: nothing in the file, so the uploaded key is what counts."""
    for k in ("auth_mode", "service_account_file", "token_file", "client_secret_file"):
        env.cfg.raw.setdefault("calendar", {}).pop(k, None)


@pytest.fixture
def google(monkeypatch):
    fake = {"cal": FakeCalendar(), "broker": FakeCalendar(), "creds": []}
    monkeypatch.setattr(cs, "load_creds", lambda cal: fake["creds"].append(cal) or object())
    monkeypatch.setattr(cs, "make_client", lambda creds: fake["cal"])
    monkeypatch.setattr(cs, "broker_client", lambda cal: fake["broker"])
    return fake


def upload(http, text: str) -> tuple[int, Any]:
    code, body, _ = http("/v1/integrations/calendar/key", "PUT", {"key_json": text}, AUTH)
    return code, body


# ------------------------------------------------------------------ the key
def test_key_upload_is_validated_stored_privately_and_never_returned(http, env):
    for text, msg in [("", "choose the key file"), ("not json", "not JSON"),
                      (json.dumps({"installed": {"client_id": "x"}}), "not a service-account key"),
                      (json.dumps({**KEY, "private_key": ""}), "missing private_key"),
                      (json.dumps({**KEY, "type": "authorized_user"}), "type: authorized_user")]:
        code, body = upload(http, text)
        assert code == 400 and msg in body["error"], (text, body)
    assert not cs.key_path(env.cfg).exists()

    code, body = upload(http, json.dumps(KEY))
    assert code == 200 and body["key"] == {"present": True, "client_email": KEY["client_email"],
                                           "project_id": "starlab-test", "private": True}
    path = cs.key_path(env.cfg)
    assert path.name == "google-calendar.json" and path.parent == env.cfg.secrets_dir
    assert path.stat().st_mode & 0o777 == 0o600 and json.loads(path.read_text())["private_key"] == KEY["private_key"]
    code, st, _ = http("/v1/integrations/calendar", headers=AUTH)
    assert code == 200 and "PRIVATE KEY" not in json.dumps(st) and "PRIVATE KEY" not in json.dumps(body)
    assert st["auth_mode"] == "service_account" and st["enabled"] is False
    assert not list(env.cfg.secrets_dir.glob("*.tmp"))

    assert http("/v1/integrations/calendar/key", "PUT", {"key_json": json.dumps(KEY)})[0] == 401   # signed-in only


def test_credentials_in_the_config_file_win_and_the_upload_is_refused(http, env):
    env.cfg.raw.setdefault("calendar", {})["auth_mode"] = "oauth"
    code, body = upload(http, json.dumps(KEY))
    assert code == 409 and "configuration file" in body["error"] and not cs.key_path(env.cfg).exists()
    st = http("/v1/integrations/calendar", headers=AUTH)[1]
    assert st["credentials_in_config_file"] == "oauth" and st["key"] is None
    assert cs.effective(env.cfg)["auth_mode"] == "oauth" and "service_account_file" not in cs.effective(env.cfg)


def test_the_effective_calendar_config_uses_the_uploaded_key_by_default(env):
    cal = cs.effective(env.cfg)
    assert cal["auth_mode"] == "service_account" and cal["service_account_file"] == str(cs.key_path(env.cfg))
    env.cfg.raw.setdefault("calendar", {})["service_account_file"] = "/etc/elsewhere.json"
    assert cs.managed_by_file(env.cfg) == "service_account_file"


# ------------------------------------------------------------------ connection test
def test_connection_test_writes_and_removes_one_event(http, env, google):
    upload(http, json.dumps(KEY))
    code, res, _ = http("/v1/integrations/calendar/test", "POST", {"calendar_id": " c_1@group.calendar.google.com "}, AUTH)
    assert code == 200 and res["ok"] and res["calendar_id"] == "c_1@group.calendar.google.com"
    assert [c["check"] for c in res["checks"]] == ["Credentials", "Calendar", "Permission to add events"]
    assert len(google["cal"].inserted) == 1 and google["cal"].deleted == ["ev1"]
    assert google["cal"].inserted[0]["start"] == {"date": "2000-01-01"}


@pytest.mark.parametrize("fake,expect", [
    (FakeCalendar(role="reader"), ["Make changes to events", "Google Workspace policy"]),
    (FakeCalendar(get_error=404), ["no calendar with that ID", "Share with specific people"]),
    (FakeCalendar(insert_error=403), ["Make changes to events", "Google said"]),
    (FakeCalendar(get_error=401), ["refused the key"]),
])
def test_connection_test_explains_what_to_fix(http, env, google, fake, expect):
    upload(http, json.dumps(KEY))
    google["cal"] = fake
    res = http("/v1/integrations/calendar/test", "POST", {"calendar_id": "c_1"}, AUTH)[1]
    assert not res["ok"]
    detail = res["checks"][-1]["detail"]
    assert all(x in detail for x in expect), detail
    assert KEY["client_email"] in detail or fake.get_error == 401
    assert not fake.inserted or fake.insert_error is None


def test_connection_test_needs_an_id_and_a_key(http, env, google):
    res = http("/v1/integrations/calendar/test", "POST", {"calendar_id": ""}, AUTH)[1]
    assert not res["ok"] and res["checks"][0]["check"] == "Calendar ID"
    res = http("/v1/integrations/calendar/test", "POST", {"calendar_id": "c_1"}, AUTH)[1]
    assert not res["ok"] and "upload the service-account key" in res["checks"][0]["detail"] and not google["creds"]


# ------------------------------------------------------------------ on and off, without a restart
def test_turning_the_calendar_on_and_off_takes_effect_without_a_restart(http, env, google):
    upload(http, json.dumps(KEY))
    st = http("/v1/integrations/calendar", headers=AUTH)[1]
    assert not st["running"]
    code, res, _ = http("/v1/integrations/calendar/enable", "POST", {"calendar_id": "c_1"}, AUTH)
    assert code == 200 and res["ok"] and res["status"]["enabled"] and res["status"]["publish"] and res["status"]["running"]
    doc = env.s.config_store.current("integrations")
    assert doc["data"]["calendar_enabled"] and doc["data"]["calendar_id"] == "c_1" and doc["data"]["publish_jobs_to_calendar"]
    assert doc["author"] == "api" and env.cfg.section("calendar")["enabled"] and env.cfg.section("scheduler")["calendar"]
    ov = http("/v1/integrations", headers=AUTH)[1]
    assert ov["calendar"] == {"enabled": True, "running": True, "calendar_id": "c_1"} and ov["slack"]["enabled"] is False

    code, res, _ = http("/v1/integrations/calendar/disable", "POST", {}, AUTH)
    assert code == 200 and not res["status"]["enabled"] and not res["status"]["running"]
    assert http("/v1/integrations", headers=AUTH)[1]["calendar"]["enabled"] is False
    assert http("/v1/integrations/calendar/key", "DELETE", None, AUTH)[0] == 200 and not cs.key_path(env.cfg).exists()


def test_a_failed_test_turns_nothing_on(http, env, google):
    upload(http, json.dumps(KEY))
    google["cal"] = FakeCalendar(role="reader")
    before = env.s.config_store.current("integrations")["version"]
    res = http("/v1/integrations/calendar/enable", "POST", {"calendar_id": "c_1"}, AUTH)[1]
    assert not res["ok"] and not res["status"]["enabled"]
    assert env.s.config_store.current("integrations")["version"] == before


def test_the_key_cannot_be_removed_while_the_calendar_is_on(http, env, google):
    upload(http, json.dumps(KEY))
    http("/v1/integrations/calendar/enable", "POST", {"calendar_id": "c_1"}, AUTH)
    code, body, _ = http("/v1/integrations/calendar/key", "DELETE", None, AUTH)
    assert code == 409 and "turn the calendar off" in body["error"] and cs.key_path(env.cfg).exists()


def test_broken_credentials_leave_scheduling_running(http, env, google, monkeypatch):
    upload(http, json.dumps(KEY))
    def boom(cal: dict[str, Any]) -> Any:
        raise ValueError("key file unreadable")
    monkeypatch.setattr(cs, "broker_client", boom)
    res = http("/v1/integrations/calendar/enable", "POST", {"calendar_id": "c_1"}, AUTH)[1]
    assert res["ok"] and res["status"]["enabled"] and not res["status"]["running"]
    assert "key file unreadable" in res["status"]["client_error"]


# ------------------------------------------------------------------ pages
def test_the_wizard_and_settings_load_the_shared_integrations_script(http):
    code, js, headers = http("/ui/integrations.js")
    assert code == 200 and headers["content-type"].startswith("text/javascript")
    assert "window.StarlabIntegrations" in js and "mountCalendar" in js and "mountSlack" in js
    setup = http("/setup")[1]
    assert '<script src="/ui/integrations.js"></script>' in setup and 'id="int-calendar"' in setup and 'id="int-slack"' in setup
    rf_page = http("/rf")[1]
    assert '<script src="/ui/integrations.js"></script>' in rf_page and "data-cal-card" in rf_page
