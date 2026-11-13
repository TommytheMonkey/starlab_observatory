"""Who may reach the web page and API, and the addresses other computers can use to open it.

`[broker] allowed_clients` decides which client addresses get an answer at all. The default, "private",
answers this computer, home and office networks (10/8, 172.16/12, 192.168/16, link-local, IPv6 ULA) and
Tailscale (100.64/10, fd7a:115c:a1e0::/48), and refuses everything else. "any" turns the check off (only
behind a reverse proxy that does its own access control). CIDRs may be listed too.

Behind `tailscale serve` or a local reverse proxy every request arrives from 127.0.0.1, which is always
allowed: the proxy is responsible for who reaches it.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
from typing import Any

from ..config import Config

PRIVATE = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
           "100.64.0.0/10", "fc00::/7", "fe80::/10")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
WILDCARD_HOSTS = ("0.0.0.0", "::", "")
VIRTUAL_IF = ("lo", "docker", "br-", "veth", "virbr", "lxdbr", "lxcbr", "cni", "flannel", "podman", "tailscale")


class AccessRules:
    def __init__(self, entries: list[str] | str | None = None):
        if entries is None:
            entries = ["private"]
        if isinstance(entries, str):
            entries = [entries]
        self.any = False
        self.networks: list[Any] = []
        for e in entries:
            e = str(e).strip()
            if e == "any":
                self.any = True
            elif e == "private":
                self.networks += [ipaddress.ip_network(n) for n in PRIVATE]
            else:
                try:
                    self.networks.append(ipaddress.ip_network(e, strict=False))
                except ValueError as exc:
                    raise ValueError(f"[broker] allowed_clients: {e!r} is not 'private', 'any' or a network "
                                     "like 192.168.1.0/24") from exc
        self.networks.append(ipaddress.ip_network("127.0.0.0/8"))      # this computer and local proxies, always
        self.networks.append(ipaddress.ip_network("::1/128"))

    @property
    def private_only(self) -> bool:
        return not self.any and all(any(n.subnet_of(p) for p in map(ipaddress.ip_network, PRIVATE)
                                        if n.version == p.version) for n in self.networks)

    def allows(self, host: str | None) -> bool:
        if self.any:
            return True
        try:
            ip = ipaddress.ip_address((host or "").split("%")[0])
        except ValueError:
            return False                    # no address (unix socket, test client "testclient"): refuse
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in n for n in self.networks if n.version == ip.version)


def rules(config: Config) -> AccessRules:
    return AccessRules(config.section("broker").get("allowed_clients"))


REFUSED = ("Starlab only answers computers on this computer's private networks (home or office network, or "
           "Tailscale). Your address, {ip}, is not one of them. To reach Starlab from anywhere, install Tailscale on "
           "both computers; see the guide 'Opening Starlab from another computer'.")


# ------------------------------------------------------------------ addresses to offer to other computers

def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def tailscale_info() -> dict[str, Any]:
    """This computer's Tailscale name and addresses, and the HTTPS address if `tailscale serve` points at us."""
    try:
        st = json.loads(_run(["tailscale", "status", "--json"]) or "{}")
    except ValueError:
        return {}
    me = st.get("Self") or {}
    if st.get("BackendState") != "Running" or not me:
        return {}
    dns = str(me.get("DNSName") or "").rstrip(".")
    try:
        serve = json.loads(_run(["tailscale", "serve", "status", "--json"]) or "{}")
    except ValueError:
        serve = {}
    return {"dns_name": dns, "short_name": dns.split(".")[0] if dns else "",
            "ipv4": [a for a in me.get("TailscaleIPs") or [] if ":" not in a],
            "https_enabled": bool(st.get("CertDomains")), "serve": serve}


def serve_proxies_to(serve: dict[str, Any], port: int) -> bool:
    """True when a `tailscale serve` HTTPS handler forwards to this port on this computer."""
    for host in (serve.get("Web") or {}).values():
        for h in (host.get("Handlers") or {}).values():
            target = str(h.get("Proxy") or "")
            if target.rstrip("/").endswith(f":{port}") and any(
                    x in target for x in ("127.0.0.1", "localhost", "[::1]")):
                return True
    return False


def lan_ipv4(interfaces: dict[str, list[str]]) -> list[str]:
    out = []
    for name, addrs in interfaces.items():
        if name.startswith(VIRTUAL_IF):
            continue
        out += [a for a in addrs if ipaddress.ip_address(a).is_private and not a.startswith("127.")]
    return out


def primary_ipv4() -> list[str]:
    """The address this computer uses for its default route (no packet is sent): a fallback without `ip`."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.0.2.1", 9))
            ip = sk.getsockname()[0]
    except OSError:
        return []
    return [ip] if ipaddress.ip_address(ip).is_private and not ip.startswith("127.") else []


def mdns_active() -> bool:
    return _run(["systemctl", "is-active", "avahi-daemon"]).strip() == "active"


def addresses(config: Config, interfaces: dict[str, list[str]] | None = None,
              ts: dict[str, Any] | None = None, mdns: bool | None = None) -> list[dict[str, str]]:
    """Every base URL another computer (or this one) can open, best first, each with a short label."""
    b = config.section("broker")
    host, port = str(b.get("host", "127.0.0.1")), int(b.get("port", 8751))
    if ts is None:
        ts = tailscale_info()
    out: list[dict[str, str]] = []
    if ts.get("dns_name") and serve_proxies_to(ts.get("serve") or {}, port):
        out.append({"label": "Tailscale, HTTPS (recommended)", "url": f"https://{ts['dns_name']}", "kind": "tailscale"})
    if host in LOOPBACK_HOSTS:
        out.append({"label": "this computer only", "url": f"http://127.0.0.1:{port}", "kind": "local"})
        return out
    if host not in WILDCARD_HOSTS:
        out.append({"label": "listen address", "url": f"http://{host}:{port}", "kind": "network"})
        return out
    probe = interfaces is None
    if interfaces is None:
        from ..sessions.manager import interface_addresses
        interfaces = interface_addresses()
    lan = lan_ipv4(interfaces) or (primary_ipv4() if probe else [])
    if mdns if mdns is not None else mdns_active():
        out.append({"label": "home network", "url": f"http://{socket.gethostname()}.local:{port}", "kind": "network"})
    out += [{"label": "home network", "url": f"http://{ip}:{port}", "kind": "network"} for ip in lan]
    if ts.get("short_name"):
        out.append({"label": "Tailscale", "url": f"http://{ts['short_name']}:{port}", "kind": "tailscale"})
    out += [{"label": "Tailscale", "url": f"http://{ip}:{port}", "kind": "tailscale"} for ip in ts.get("ipv4") or []]
    if not out:
        out.append({"label": "this computer", "url": f"http://127.0.0.1:{port}", "kind": "local"})
    return out
