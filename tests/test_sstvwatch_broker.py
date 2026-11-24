"""The broker's side of the SSTV watch: its Slack upload, the SSTV page proxy, and the processing queue's priority
for Starlab's own (fast) audio decoders."""

from __future__ import annotations

import pathlib

from PIL import Image

from aobs.web.sstvwatch_proxy import READ_PATHS
from test_archive import recorded
from test_processing_watchdog import analyze, controlled
from test_scheduler import env  # noqa: F401  (fixture)
from test_slack import enable, service


def test_sstv_watch_decodes_go_to_slack_with_their_thumbnails_and_nothing_outside_its_folder(env, tmp_path):
    root = tmp_path / "sstv-watch"
    root.mkdir()
    thumb = root / "000001.thumb.jpg"
    Image.new("RGB", (32, 32), (200, 10, 10)).save(thumb)
    outside = tmp_path / "secret.png"
    Image.new("RGB", (32, 32)).save(outside)
    env.s.config.raw.setdefault("scheduler", {})["sstvwatch_data_dir"] = str(root)   # survives settings reloads
    sv, fake = service(env)
    enable(env, notify_completed=False, notify_receiver_offline=False, notify_storage=False)
    sv._init_state()
    sv.tick()                                                  # the first pass only sets the cursor (no replay)
    with env.s.engine.begin() as conn:
        env.s.notify(conn, "sstv", None, ":frame_with_picture: SSTV 14.230 MHz USB (20m) · Robot 36",
                     {"source": "sstv-watch", "images": [{"path": str(thumb), "title": "20m Robot 36"},
                                                         {"path": str(outside), "title": "nope"},
                                                         {"path": str(root / "../secret.png"), "title": "nope"}]})
    posted = sv.tick()
    assert posted == [":frame_with_picture: SSTV 14.230 MHz USB (20m) · Robot 36"]
    assert [m for m, *_ in fake.calls].count("files.getUploadURLExternal") == 1   # only the file in its folder
    assert sv.tick() == []                                     # posted once


def test_the_sstv_page_proxy_forwards_only_the_gallery_and_its_files():
    for ok in ("images", "facets", "images/12", "images/12/image", "images/12/thumb", "images/12/audio", "images/12/iq"):
        assert READ_PATHS.fullmatch(ok), ok
    for bad in ("images/12/../../etc", "release", "images/x", "status.json", "images/12/raw"):
        assert not READ_PATHS.fullmatch(bad), bad


def test_sstv_and_aprs_decodes_go_ahead_of_satdump_in_the_queue(env):
    jid, _ = recorded(env, satdump={"enabled": False}, postprocess={"decode": False})
    proc = controlled(env)
    slow, fast = analyze(proc, jid), analyze(proc, jid)
    proc._set(slow, kind="decode", resolved={"pipeline": "meteor_m2-x_lrpt"})
    proc._set(fast, kind="decode", resolved={"pipeline": "sstv"})
    proc.step()
    assert proc.current["attempt_id"] == fast                 # queued later, started first
    assert proc.attempt(slow)["status"] == "queued"
