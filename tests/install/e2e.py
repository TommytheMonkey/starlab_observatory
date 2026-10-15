"""First-run workflow over HTTP against a freshly installed Starlab (simulation mode, no hardware).

    python3 e2e.py http://127.0.0.1:8751 <sign-in link>

Standard library only. Exits non-zero on the first failed expectation.
"""
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request

BASE, LINK = sys.argv[1].rstrip("/"), sys.argv[2]
jar = http.cookiejar.CookieJar()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


raw = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
results = []


def call(path, method="GET", body=None, expect=200):
    req = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-AOBS-RF": "1"})
    try:
        with raw.open(req, timeout=60) as r:
            code, data, headers = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        code, data, headers = e.code, e.read(), e.headers
    if code != expect:
        raise SystemExit(f"FAIL {method} {path}: HTTP {code}, expected {expect}: {data[:400]!r}")
    try:
        return json.loads(data), headers
    except ValueError:
        return data.decode(errors="replace"), headers


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f": {detail}" if detail else ""), flush=True)
    if not cond:
        raise SystemExit(1)


# sign in with the link the installer printed
_, h = call(LINK.replace(BASE, ""), expect=303)
check("sign-in link sets a session and goes to /", h["location"] == "/")
_, h = call("/", expect=303)
check("a fresh install lands in the setup wizard", h["location"] == "/setup")
page, _ = call("/setup")
check("setup page is served", "Setup · Starlab Observatory" in page)

st, _ = call("/v1/setup")
check("wizard starts at Welcome, not completed", st["step"] == "welcome" and not st["completed"])
cfg, _ = call("/v1/config")
check("empty database got default settings", cfg["documents"]["observatory"]["current"]["data"]["latitude"] is None)
check("bootstrap paths are the installer's", "/srv/starlab-data" in cfg["bootstrap"]["data_root"], cfg["bootstrap"]["data_root"])

call("/v1/setup/step", "POST", {"step": "welcome", "action": "done"})
obs = cfg["documents"]["observatory"]["current"]
call("/v1/config/observatory", "PUT", {"base_version": obs["version"], "data": {**obs["data"], "name": "Test Station",
     "timezone": "Europe/London", "latitude": 51.4779, "longitude": -0.0015, "elevation_m": 46}})
bad, _ = call("/v1/config/observatory", "PUT", {"data": {**obs["data"], "latitude": 123}}, expect=400)
check("invalid latitude refused", "between -90 and 90" in bad["error"])
st, _ = call("/v1/setup/step", "POST", {"step": "identity", "action": "done"})
check("progress persisted (identity done)", any(s["id"] == "identity" and s["state"] == "done" for s in st["steps"]))
call("/v1/setup/step", "POST", {"step": "hosts", "action": "skip"})

sim = call("/v1/config/simulation")[0]["current"]
call("/v1/config/simulation", "PUT", {"base_version": sim["version"], "data": {"enabled": True}})
inv, _ = call("/v1/inventory")
sims = [d for d in inv["discovered"] if d["simulated"]]
check("simulation mode offers simulated devices", len(sims) == 2 and all(d["support"]["status"] == "supported" for d in sims))
check("no real receiver is configured", inv["receivers"] == [])
call("/v1/setup/step", "POST", {"step": "discovery", "action": "done"})

row, _ = call("/v1/inventory/receivers", "POST", {"usb_path": "sim-1", "id": "SIM-A", "name": "Simulated A"})
check("simulated receiver added as configured, not verified", row["state"] == "configured" and row["readiness"] == "untested")
start, _ = call("/v1/inventory/receivers/SIM-A/verify", "POST", {})
deadline = time.time() + 240
while True:
    d, _ = call(f"/v1/rf/tests/{start['test_id']}")
    if d["progress"]["done"] or time.time() > deadline:
        break
    time.sleep(2)
check("simulated test completed and was analysed", d["progress"]["done"] and "failed" not in d["progress"]["label"].lower(),
      d["progress"]["label"])
check("analysis produced metrics from the simulated samples", (d.get("metrics") or {}).get("summary") is not None)
check("the test ran on the simulator", (d["job"]["receiver"] or {}).get("driver") == "sim")
rx = next(r for r in call("/v1/inventory")[0]["receivers"] if r["id"] == "SIM-A")
check("readiness is 'simulated', never 'verified'", rx["readiness"] == "simulated" and rx["state"] == "simulated",
      f"{rx['readiness']}/{rx['state']}")
check("readiness evidence says no hardware was tested", "no hardware was tested" in (rx["readiness_detail"] or {}).get("note", ""))
call("/v1/setup/step", "POST", {"step": "receivers", "action": "done"})

sat = call("/v1/config/satellites")[0]["current"]
call("/v1/config/satellites", "PUT", {"base_version": sat["version"], "data": {**sat["data"], "selected": [57166, 59051, 25544],
     "min_elevation_deg": 10}})
pred, _ = call("/v1/passes/predicted?hours=48")
check("built-in predictor finds passes offline", pred["location_set"] and len(pred["passes"]) > 3, f"{len(pred['passes'])} passes")
check("orbital elements are labelled as the shipped sample",
      all(e["source"] == "sample elements shipped with Starlab" for e in pred["satellites"]["elements"] if e["available"]))
call("/v1/setup/step", "POST", {"step": "satellites", "action": "done"})
sch = call("/v1/config/scheduling")[0]["current"]
call("/v1/config/scheduling", "PUT", {"base_version": sch["version"], "data": {"mode": "hybrid"}})
call("/v1/setup/step", "POST", {"step": "scheduling", "action": "done"})
call("/v1/setup/step", "POST", {"step": "integrations", "action": "skip"})

p = next(x for x in pred["passes"] if x["norad"] in (57166, 59051))
booked, _ = call("/v1/passes/book", "POST", {"norad": p["norad"], "aos": p["aos"], "receiver_id": "SIM-A"})
check("a predicted pass can be booked on a simulated receiver", booked["ok"] and booked["simulated"])
again = call("/v1/passes/predicted?hours=48")[0]
import datetime as _dt
near = [x for x in again["passes"] if x["norad"] == p["norad"]
        and abs((_dt.datetime.fromisoformat(x["aos"]) - _dt.datetime.fromisoformat(p["aos"])).total_seconds()) < 60]
check("the booking shows on the pass", near and any(b["receiver_id"] == "SIM-A" for b in near[0]["booked"]))
again_book = call("/v1/passes/book", "POST", {"norad": p["norad"], "aos": p["aos"], "receiver_id": "SIM-A"}, expect=409)[0]
check("booking the same receiver twice for one pass is refused", "Not booked" in again_book["error"])

val, _ = call("/v1/setup/validate", "POST", {})
check("validation passes (warnings allowed)", val["ok"], [c for c in val["checks"] if c["level"] == "fail"])
check("validation reports automatic recording off",
      any(c["check"] == "Automatic recording" and "off on every receiver" in c["detail"] for c in val["checks"]))
done, _ = call("/v1/setup/finish", "POST", {})
check("setup finished", done["completed"])
_, h = call("/", expect=303)
check("after setup the landing page is the station page", h["location"] == "/rf")
exp, _ = call("/v1/config/export")
blob = json.dumps(exp).lower()
check("export contains no credentials", not any(w in blob for w in ("api_token", "password", "rf_ui_key")))
rfv, _ = call("/v1/rf")
check("station page data loads with the simulated receiver",
      rfv["receiver_info"]["SIM-A"]["state"] == "simulated" and rfv["receiver_info"]["SIM-A"]["driver"] == "sim")
print(f"\nE2E: {sum(ok for _, ok in results)}/{len(results)} checks passed")
