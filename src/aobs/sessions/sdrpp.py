"""SDR++ server sessions: what SDR++ can and cannot guarantee, and how Starlab makes it safe.

What SDR++ (1.2.x) does on its own, from its installed files and source (checked 2026-10-15, without
running it against hardware):
  * `sdrpp --server --addr A --port P --root DIR [--autostart]` runs headless and serves its source to
    remote SDR++ clients. --addr defaults to 0.0.0.0 (every interface) if omitted.
  * A source module picks its device by a saved name string (e.g. "Realtek RTL2838UHIDIR [OBS-R02]##2" in
    rtl_sdr_config.json) and, when that device is not found, falls back to the first device it can list.
  * In server mode the remote client is shown the server's source menu, so it can switch device, and
    source module, among everything the server can see.
So SDR++ configuration alone cannot pin a session to one physical receiver.

Starlab's safe alternative: run SDR++ in a bubblewrap sandbox where the selected receiver is the only USB
device that is visible (/sys/bus/usb/devices holds just that device) and the only one that can be
opened (/dev/bus/usb holds just its node). The private config also creates only that driver's source
instance. Whatever SDR++ or the client selects, no other receiver can be opened. SDRplay devices are
reached through the SDRplay API service rather than a device node, so they cannot be isolated this way
and are not offered. Without bubblewrap, no SDR++ session is offered.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
from dataclasses import dataclass
from typing import Any

# driver -> (SDR++ source module, instance name SDR++ uses)
MODULES = {"rtlsdr": ("rtl_sdr_source", "RTL-SDR Source"), "airspy": ("airspy_source", "Airspy Source")}
NOT_ISOLATABLE = {"sdrplay": "SDRplay receivers are reached through the SDRplay API service, not a USB device node, "
                             "so a session cannot be restricted to one receiver"}


@dataclass(frozen=True)
class Paths:
    sdrpp: str = "/usr/bin/sdrpp"
    plugins: str = "/usr/lib/sdrpp/plugins"
    resources: str = "/usr/share/sdrpp"
    bwrap: str = "/usr/bin/bwrap"


def paths_from(settings: dict[str, Any]) -> Paths:
    return Paths(sdrpp=str(settings.get("sdrpp_path", Paths.sdrpp)),
                 plugins=str(settings.get("sdrpp_plugins_dir", Paths.plugins)),
                 resources=str(settings.get("sdrpp_resources_dir", Paths.resources)),
                 bwrap=str(settings.get("bwrap_path", Paths.bwrap)))


def install_problems(driver: str, p: Paths) -> list[str]:
    out = []
    if driver in NOT_ISOLATABLE:
        return [NOT_ISOLATABLE[driver]]
    if driver not in MODULES:
        return [f"SDR++ sessions are not available for {driver} receivers"]
    if not (os.path.isfile(p.sdrpp) and os.access(p.sdrpp, os.X_OK)):
        out.append(f"SDR++ is not installed ({p.sdrpp} missing). Install it (e.g. the sdrpp package) and try again.")
    module = MODULES[driver][0]
    if not os.path.isfile(os.path.join(p.plugins, module + ".so")):
        out.append(f"SDR++'s {module} module is missing from {p.plugins}")
    if not (os.path.isfile(p.bwrap) and os.access(p.bwrap, os.X_OK)):
        out.append(f"bubblewrap ({p.bwrap}) is not installed; without it a session cannot be restricted to one "
                   "receiver, so none is started (sudo apt install bubblewrap)")
    return out


def write_root(root: pathlib.Path, driver: str, p: Paths, sample_rate: int | None = None) -> pathlib.Path:
    """A private SDR++ config folder for one session: only this driver's source instance exists."""
    module, instance = MODULES[driver]
    root.mkdir(parents=True, exist_ok=True)
    cfg = {"modulesDirectory": p.plugins, "resourcesDirectory": p.resources, "modules": [],
           "moduleInstances": {instance: {"module": module, "enabled": True}}, "source": instance,
           "showMenu": False}
    (root / "config.json").write_text(json.dumps(cfg, indent=2))
    return root


def sandbox_command(p: Paths, root: pathlib.Path, device_node: str, sysfs_device: str, usb_name: str,
                    bind_addr: str, port: int, program: list[str] | None = None) -> list[str]:
    """bwrap: the whole system read-only, a fresh /dev with only this receiver's USB node, a /sys/bus/usb/devices
    with only this receiver, private IPC, a writable private config folder. Network is shared (it serves)."""
    inner = program or [p.sdrpp, "--server", "--addr", bind_addr, "--port", str(port), "--root", str(root),
                        "--autostart"]
    return [p.bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--dev-bind", device_node, device_node,
            "--proc", "/proc", "--tmpfs", "/sys/bus/usb/devices", "--symlink", sysfs_device,
            f"/sys/bus/usb/devices/{usb_name}", "--tmpfs", "/tmp", "--bind", str(root), str(root),
            "--unshare-ipc", "--die-with-parent", "--setenv", "HOME", str(root), "--", *inner]


def which(name: str) -> str | None:
    return shutil.which(name)
