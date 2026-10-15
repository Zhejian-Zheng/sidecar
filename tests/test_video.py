import hashlib
import io
import json
import struct
import time

import pytest

from sidecar.video import latest_frame, read_png


def publish(path, *, status="live", age=0, data=b"image"):
    meta = {"status": status, "received_at": time.monotonic() - age,
            "sequence": 1, "sha256": hashlib.sha256(data).hexdigest()}
    (path / "latest.png").write_bytes(data)
    (path / "stream.json").write_text(json.dumps(meta))
    return meta


def test_preview_never_serves_stopped_stale_or_mixed_frames(tmp_path):
    publish(tmp_path)
    assert latest_frame(tmp_path)[0] == b"image"
    (tmp_path / "latest.png").write_bytes(b"next-generation")
    with pytest.raises(TimeoutError):
        latest_frame(tmp_path)
    for params in ({"status": "stopped"}, {"age": 4}, {"age": -10}):
        publish(tmp_path, **params)
        with pytest.raises(TimeoutError):
            latest_frame(tmp_path)


def test_observation_waits_for_a_frame_after_its_barrier(tmp_path):
    publish(tmp_path)
    with pytest.raises(TimeoutError):
        latest_frame(tmp_path, after=time.monotonic())
    before = time.monotonic()
    publish(tmp_path)
    assert latest_frame(tmp_path, after=before)[0] == b"image"


def test_png_reader_handles_real_frames_and_rejects_truncated_or_unbounded_data():
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (24, 32), "blue").save(out, format="PNG")
    data = out.getvalue()
    stream = io.BytesIO(data + data)
    assert read_png(stream) == data
    assert read_png(stream) == data
    with pytest.raises(EOFError):
        read_png(io.BytesIO(data[:-3]))
    with pytest.raises(ValueError):
        read_png(io.BytesIO(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 9_000_000) + b"IDAT"))


def test_stream_image_rejects_another_display_session(tmp_path, monkeypatch):
    from sidecar import video
    from sidecar.displays import DisplayBinding

    binding = DisplayBinding(id=2, unique_id="expected", boot_id="boot")
    monkeypatch.setattr(video, "latest_frame", lambda *a, **kw: (b"png", {"display": {
        "id": 2, "unique_id": "replaced", "boot_id": "boot"}}))
    with pytest.raises(RuntimeError, match="another display"):
        video.image_from_stream(tmp_path, binding)


@pytest.mark.parametrize("stalled", [False, True])
def test_failed_session_start_releases_started_processes(tmp_path, monkeypatch, stalled):
    from sidecar import displays, video
    from sidecar.displays import DisplayBinding

    server = tmp_path / "scrcpy-server"
    server.write_bytes(b"server")
    monkeypatch.setenv("SCRCPY_SERVER_PATH", str(server))
    monkeypatch.setattr(video.shutil, "which", lambda name: "/bin/scrcpy")
    monkeypatch.setattr(displays, "verify", lambda *args: {})

    def run(args):
        if "--version" in args:
            return b"scrcpy 5.0.1"
        raise RuntimeError("device disconnected during push")

    monkeypatch.setattr(displays, "run", run)
    processes = []

    class Process:
        returncode = None

        def __init__(self, *a, **kw):
            self.index = len(processes)
            self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
            processes.append(self)

        def poll(self):
            return self.returncode

        def send_signal(self, signal):
            self.returncode = 0

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, **kw):
            if stalled and self.index == 1:
                raise video.subprocess.TimeoutExpired("recorder", 3)
            return self.returncode

    monkeypatch.setattr(video.subprocess, "Popen", Process)
    binding = DisplayBinding(id=2, unique_id="expected", boot_id="boot")
    session = video.VideoSession(tmp_path, "task", "test", binding)
    with pytest.raises(RuntimeError, match="disconnected"):
        with session:
            pytest.fail("session should not start")
    assert len(processes) == 2 and all(p.returncode is not None for p in processes)
    if stalled:
        assert "recorder_shutdown" in session.cleanup_errors
    assert not json.loads((session.directory / "stream.json").read_text())["recording_complete"]
