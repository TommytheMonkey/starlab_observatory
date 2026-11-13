# SDR++ server sessions

Select a receiver, choose **Test ▾ → SDR++ Server → Start**, and connect to that receiver from SDR++ on
another computer (source *SDR++ Server*, the address and port shown).

## What SDR++ can and cannot guarantee

Investigated 2026-10-15 from SDR++ 1.2.1's installed files and source code. SDR++ was **not** run against
the station's receivers, because its source modules open devices while enumerating them.

| Behaviour | Consequence |
|---|---|
| `sdrpp --server --addr A --port P --root DIR` serves its source; `--addr` defaults to `0.0.0.0` | Starlab always passes an explicit address. Allowed: `127.0.0.1`, the Tailscale address, trusted addresses from the config. `0.0.0.0` only if `[sessions] allow_all_interfaces = true` is set deliberately. |
| A source module selects its device by a saved *name* (e.g. `Realtek RTL2838UHIDIR [OBS-R02]##2`). If that device isn't found, it falls back to the first device it can list | Configuration cannot guarantee which receiver is served: the fallback is silent. |
| In server mode the remote client sees the server's source menu and can switch device, or source module, among everything the server can reach | A client could move to another receiver, including one that is recording. |

**Conclusion: SDR++ cannot by itself restrict a server session to one physical receiver.**

## Starlab's safe alternative

Starlab runs SDR++ inside a **bubblewrap sandbox**:

- **Only one visible device:** `/sys/bus/usb/devices` contains just the selected receiver, so SDR++ lists one device, and its "fall back to the first device" is that receiver.
- **Only one openable device:** `/dev/bus/usb` contains just that receiver's node, so every other receiver is impossible to open, whatever SDR++ or the client selects.
- **One source instance:** the private SDR++ configuration creates only that driver's source instance.
- **Private IPC:** no shared-memory route to other radio services.
- **The rest of the system is read-only:** the network is shared, because the server must serve.

What this means in practice:

- **SDRplay receivers are not offered.** SDRplay devices are reached through the SDRplay API service, not a USB device node, so the sandbox can't isolate one RSP.
- **No bubblewrap, no session.** Without bubblewrap installed, no session is started, and the page says why.
- **Evidence of the device served:** once SDR++ opens the receiver, the page shows that SDR++'s process holds that receiver's device node (from `/proc`).
- **Not yet confirmed on real hardware:** that SDR++ actually streams correctly inside the sandbox needs a real receiver. It's an owner-run step in `docs/release-checklist.md`; development never opens a radio.

## Ownership and scheduling

- **Eligible receivers:**
  - run by the broker (not an independent legacy recorder);
  - not behind the antenna switch, which a session doesn't control yet;
  - RTL-SDR or Airspy, or simulated.
- **Starting refuses when:**
  - the receiver isn't on USB, is open or claimed by any program, is leased, or is already in a session;
  - anything is recording or reserved on it now;
  - a booking would start before the session ends, unless *End before booked recordings* is on.
- **End before booked recordings** (on by default):
  - the session is shortened to end 2 minutes before the next booking;
  - a warning goes out 5 minutes ahead;
  - Starlab ends it on time.
  - With the policy off, recordings can't be booked on that receiver while the session runs, and an
    automatic pass that arrives anyway is **skipped with a reason and a notification**, never silently.
- **Last-moment check:** when a recording starts, the runner checks again. A yielding session is stopped;
  a non-yielding one makes the recording fail loudly instead of colliding.
- **Coordinator lease:** on stations with the original recorders, a session also takes the legacy
  coordinator lease, so those tools see the receiver as busy.

## Lifecycle and cleanup

- **Each session:**
  - has a unique ID;
  - runs in its own session and process group;
  - carries `STARLAB_SESSION_ID` in its environment.
- **Stopping** signals only that process group, and only after `/proc` shows the marker.
- **If the server exits by itself:** the session is marked failed and the receiver is freed.
- **Idle sessions:** a session with no connected client for 30 minutes is ended. Clients are counted from `/proc/net/tcp`.
- **After a broker restart:** sessions whose server is still running are adopted, the rest are closed.
- **Hard lifetime:** every server has its own limit (coreutils `timeout`), so it ends even if the broker is down when it should have ended.

## For brief 4 (remote hosts)

Backends implement `problems(rx)`, `launch(...)` and `served(row)` (`aobs/sessions/backends.py`). A remote
backend will launch the same sandboxed SDR++ through the `starlab-remote` helper over SSH. The manager's
ownership, scheduling and cleanup rules stay the same.
