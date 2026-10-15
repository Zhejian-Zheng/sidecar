import hashlib
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from sidecar import capture, displays


@pytest.fixture
def device(monkeypatch):
    binding = displays.DisplayBinding(id=7, unique_id="test", boot_id="boot")
    xml = (
        '<hierarchy><node display-id="0" text="private"/>'
        '<node display-id="7" text="Clock"/></hierarchy>'
    )
    state = SimpleNamespace(xml=xml, stopped=False)

    def stop():
        state.stopped = True

    monkeypatch.setattr(
        capture,
        "connect_device",
        lambda _: SimpleNamespace(dump_hierarchy=lambda: state.xml, stop_uiautomator=stop),
    )
    monkeypatch.setattr(displays, "verify", lambda *_: dict(width=320, height=640))
    monkeypatch.setattr(displays, "current", lambda *_: ("com.google.android.deskclock", "Clock"))
    monkeypatch.setattr(displays, "screenshot", lambda *_: Image.new("RGB", (320, 640)))
    monkeypatch.setattr(displays, "shell", lambda *_: "")
    return binding, state


def test_capture_preserves_scoped_tree_and_file_hashes(tmp_path, device):
    binding, state = device
    out = tmp_path / "sample"
    result = capture.capture("device", binding, out, "list")
    xml = (out / "hierarchy.xml").read_bytes()
    assert b"private" not in xml
    assert b"Clock" in xml
    assert result["xml_sha256"] == hashlib.sha256(xml).hexdigest()
    assert result["image_sha256"] == hashlib.sha256((out / "screen.png").read_bytes()).hexdigest()
    assert json.loads((out / "metadata.json").read_text()) == result
    assert result["locale"] is None
    assert result["time_12_24"] is None
    assert state.stopped


def test_page_change_saves_no_fixture(tmp_path, device, monkeypatch):
    binding, state = device

    def screenshot(*_):
        state.xml = state.xml.replace("Clock", "Dialog")
        return Image.new("RGB", (320, 640))

    monkeypatch.setattr(displays, "screenshot", screenshot)
    with pytest.raises(RuntimeError, match="Page changed"):
        capture.capture("device", binding, tmp_path / "sample", "list")
    assert not (tmp_path / "sample").exists()
    assert state.stopped


def test_existing_sample_is_not_overwritten(tmp_path, device):
    binding, _ = device
    (tmp_path / "keep").write_text("original")
    with pytest.raises(FileExistsError):
        capture.capture("device", binding, tmp_path, "list")
    assert (tmp_path / "keep").read_text() == "original"


def test_stale_binding_saves_nothing(tmp_path, device, monkeypatch):
    binding, _ = device

    def stale(*_):
        raise RuntimeError("stale display")

    monkeypatch.setattr(displays, "verify", stale)
    with pytest.raises(RuntimeError, match="stale display"):
        capture.capture("device", binding, tmp_path / "sample", "list")
    assert not (tmp_path / "sample").exists()
