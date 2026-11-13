"""Opening Starlab from other computers: client rules, advertised addresses, first-run sign-in links."""

from __future__ import annotations

import time
import urllib.error
import urllib.request

import pytest

from aobs.web.access import AccessRules, addresses, lan_ipv4, serve_proxies_to
from test_scheduler import env  # noqa: F401


# ------------------------------------------------------------------ who gets an answer
@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "192.168.1.40", "10.1.2.3", "172.20.0.5", "100.101.102.103",
                                "100.100.1.2", "fd7a:115c:a1e0::1234", "fe80::1%eth0", "::ffff:192.168.1.9"])
def test_private_networks_and_tailscale_are_answered(ip):
    assert AccessRules().allows(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "2603:6082:a000:1a::1a74", "100.128.0.1", "", "testclient", None])
def test_public_and_unknown_clients_are_refused(ip):
    assert not AccessRules(["private"]).allows(ip)


def test_custom_networks_any_and_bad_entries():
    r = AccessRules(["192.168.4.0/24"])
    assert r.allows("192.168.4.20") and not r.allows("192.168.5.20") and r.allows("127.0.0.1")   # loopback always
    assert r.private_only and AccessRules().private_only
    assert AccessRules("any").allows("8.8.8.8") and not AccessRules("any").private_only
    assert not AccessRules(["203.0.113.0/24"]).private_only
    with pytest.raises(ValueError, match="allowed_clients"):
        AccessRules(["home"])


# ------------------------------------------------------------------ addresses offered to other computers
IFACES = {"lo": ["127.0.0.1"], "enp3s0": ["192.168.1.40"], "docker0": ["172.17.0.1"], "tailscale0": ["100.101.102.103"],
          "br-1234": ["172.18.0.1"]}
TS = {"dns_name": "starlab.tail1234.ts.net", "short_name": "starlab", "ipv4": ["100.101.102.103"],
      "https_enabled": True, "serve": {}}
SERVE = {"Web": {"starlab.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8751"}}}}}


def test_lan_addresses_skip_docker_bridges_and_tailscale():
    assert lan_ipv4(IFACES) == ["192.168.1.40"]


def test_network_mode_lists_home_network_and_tailscale(env):
    env.cfg.raw["broker"] = {"host": "0.0.0.0", "port": 8751}
    urls = [a["url"] for a in addresses(env.cfg, IFACES, TS, mdns=True)]
    assert urls[1:] == ["http://192.168.1.40:8751", "http://starlab:8751", "http://100.101.102.103:8751"]
    assert urls[0].endswith(".local:8751")
    assert all("172.17" not in u for u in urls)


def test_tailscale_https_comes_first_when_serve_points_here(env):
    env.cfg.raw["broker"] = {"host": "127.0.0.1", "port": 8751}
    found = addresses(env.cfg, IFACES, {**TS, "serve": SERVE}, mdns=False)
    assert [a["url"] for a in found] == ["https://starlab.tail1234.ts.net", "http://127.0.0.1:8751"]
    assert "recommended" in found[0]["label"]
    assert not serve_proxies_to(SERVE, 9999)          # serve for another program is not ours


def test_local_mode_without_tailscale_is_this_computer_only(env):
    env.cfg.raw["broker"] = {"host": "127.0.0.1", "port": 8751}
    assert addresses(env.cfg, IFACES, {}, mdns=False) == [
        {"label": "this computer only", "url": "http://127.0.0.1:8751", "kind": "local"}]


# ------------------------------------------------------------------ first-run sign-in links
def test_first_run_links_last_a_day_and_use_their_own_signature(env):
    from aobs.web.auth import SETUP_LINK_S, login_url, sign
    url = login_url(env.cfg, "http://x:8751", first_run=True)
    exp, sig = url.rsplit("/", 2)[-2:]
    assert int(exp) - time.time() > SETUP_LINK_S - 60
    assert sig == sign(env.cfg, "setup-login", int(exp)) != sign(env.cfg, "login", int(exp))


def _get(port: int, path: str) -> tuple[int, str]:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # noqa: ANN002, ANN003
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_login_route_accepts_first_run_links_only_until_setup_finishes(env):
    from aobs.coordination.api import create_app
    from aobs.coordination.broker import Broker
    from aobs.scheduling.planner import Planner
    from aobs.scheduling.service import SchedulerService
    from aobs.web.access import AccessRules
    from aobs.web.auth import login_url
    from test_archive import Http
    from test_broker import FakeHw

    svc = SchedulerService(env.s, Planner(env.s, predictor=lambda rx, h: []), None)
    svc.start = lambda: None
    c = Http(create_app(Broker(env.cfg, FakeHw(), env.clock), "t", scheduler=svc, access=AccessRules()))
    try:
        st = env.s.config_store
        st.save("setup", {**st.current("setup")["data"], "completed": False}, None, "o")
        first = login_url(env.cfg, "", first_run=True)
        normal = login_url(env.cfg, "")
        code, _ = _get(c.port, first)
        assert code == 303
        st.save("setup", {**st.current("setup")["data"], "completed": True}, None, "o")
        code, body = _get(c.port, first)
        assert code == 403 and "starlab web-link" in body and "<h1>" in body       # a page, not JSON
        assert _get(c.port, normal)[0] == 303
        assert _get(c.port, "/rf/login/1/deadbeef")[0] == 403
    finally:
        c.server.should_exit = True


def test_setup_network_check(env):
    from aobs.settings.setup import Setup
    env.cfg.raw["broker"] = {"host": "0.0.0.0", "port": 8751}
    assert Setup(env.s)._network_check()[0] == "pass"
    env.cfg.raw["broker"] = {"host": "0.0.0.0", "port": 8751, "allowed_clients": ["any"]}
    assert Setup(env.s)._network_check()[0] == "warn"
    env.cfg.raw["broker"] = {"host": "127.0.0.1"}
    assert Setup(env.s)._network_check()[0] == "pass"
