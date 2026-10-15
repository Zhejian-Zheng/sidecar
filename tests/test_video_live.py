"""Opt-in device smoke test: SIDECAR_LIVE_VIDEO=1 pytest tests/test_video_live.py.

Opens Settings on a new secondary display; does not change settings. This is not
proof of simultaneous human typing. Requires an idle Android 15 test device.
"""
import json
import os
import shutil
import subprocess
import time

import pytest

pytestmark = pytest.mark.skipif(os.getenv("SIDECAR_LIVE_VIDEO") != "1", reason="opt-in device test")


def test_real_stream_recording_and_primary_foreground(tmp_path):
    from sidecar.displays import current, hidden_display, launch
    from sidecar.video import VideoSession, latest_frame

    serial = os.getenv("SIDECAR_TEST_SERIAL", "emulator-5554")
    before = current(serial, 0)
    with hidden_display(serial) as display:
        launch(serial, display, "com.android.settings")
        with VideoSession(tmp_path, "smoke", serial, display) as video:
            _, first = latest_frame(video.directory, timeout=15)
            _, second = latest_frame(video.directory, after=time.monotonic(), timeout=8)
            assert second["sequence"] > first["sequence"]
            assert second["display"] == display.model_dump()
        meta = json.loads((video.directory / "stream.json").read_text())
        assert meta["recording_complete"] and meta["recording_finalized"]
        assert meta["status"] == "stopped"
        assert current(serial, 0) == before
        if shutil.which("ffprobe"):
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height",
                 "-of", "json", str(video.directory / "recording.mkv")],
                capture_output=True, check=True, timeout=10,
            )
            assert json.loads(probe.stdout)["streams"][0]["codec_name"] == "h264"
