import io
import json
from types import SimpleNamespace

import pytest

from sidecar import android_rpc, displays
from sidecar.android import AndroidDevice
from sidecar.contracts import Task
from sidecar.harness import Harness


@pytest.mark.parametrize(
    "failure, stage, message",
    [
        ("verify", "display_verify", "执行屏校验失败"),
        ("foreground", "foreground", "无法读取执行屏当前应用"),
        ("dump", "hierarchy", "界面读取失败"),
        ("filter", "hierarchy", "界面读取失败"),
        ("screenshot", "screenshot", "截图失败"),
        ("changed", "page_changed", "读取期间页面发生变化"),
        ("connect", "connection", "设备连接失败"),
        ("release", "release", "界面读取连接释放失败"),
        ("dump_and_release", "hierarchy", "界面读取失败"),
    ],
)
def test_observation_failure_survives_rpc_and_is_saved_safely(
    tmp_path, monkeypatch, failure, stage, message
):
    def fail():
        raise RuntimeError("private-email-and-token")

    device = SimpleNamespace(
        dump_hierarchy=fail if failure in {"dump", "dump_and_release"} else lambda: "<hierarchy/>",
        stop_uiautomator=fail if failure in {"release", "dump_and_release"} else lambda: None,
    )
    monkeypatch.setattr(
        android_rpc, "connect_device", lambda serial: fail() if failure == "connect" else device
    )
    monkeypatch.setattr(
        displays, "verify",
        lambda *args: fail() if failure == "verify" else {"width": 320, "height": 640},
    )
    reads = []

    def current(*args):
        if failure == "foreground":
            fail()
        reads.append(1)
        return (
            "com.other" if failure == "changed" and len(reads) % 2 == 0 else "com.test",
            ".Main",
        )

    monkeypatch.setattr(displays, "current", current)
    monkeypatch.setattr(
        displays,
        "filter_hierarchy",
        lambda *args: fail() if failure == "filter" else "<hierarchy/>",
    )
    monkeypatch.setattr(
        displays, "screenshot", lambda *args: fail() if failure == "screenshot" else object()
    )
    calls = []

    def subprocess_run(*args, **kwargs):
        calls.append(1)
        stdout = io.StringIO()
        monkeypatch.setattr(android_rpc.sys, "stdin", io.StringIO(kwargs["input"]))
        from contextlib import redirect_stdout

        with redirect_stdout(stdout):
            android_rpc.main()
        assert "private-email-and-token" not in stdout.getvalue()
        return SimpleNamespace(returncode=0, stdout=stdout.getvalue())

    monkeypatch.setattr("sidecar.android.subprocess.run", subprocess_run)
    android = AndroidDevice(
        "test",
        "com.test",
        lock_root=tmp_path / "locks",
        display={"id": 95, "unique_id": "test", "boot_id": "test"},
    )
    with Harness(tmp_path / "engine", android, object()) as harness:
        result = harness.start(Task(goal="save draft", criteria=["draft saved"]))
        assert result["status"] == "failed"
        assert message in result["reason"]
        assert result["decisions"] == 0
        events = harness.events(result["task_id"])
        error = next(e["data"] for e in events if e["kind"] == "error")
        assert error["stage"] == stage
        assert "private-email-and-token" not in json.dumps(events)
        assert harness.status(result["task_id"])["reason"] == result["reason"]
    assert len(calls) == 2  # Read-only retry remains bounded.


@pytest.mark.parametrize("stage", ["screenshot", "private-email-and-token", {"secret": "value"}])
def test_rpc_diagnostic_fields_cannot_inject_exception_text(monkeypatch, stage):
    from sidecar.android import Transport
    from sidecar.errors import ObservationError

    response = {"error": {"observation_stage": stage, "message": "private-email-and-token"}}
    monkeypatch.setattr(
        "sidecar.android.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(response)),
    )
    with pytest.raises(ObservationError) as caught:
        Transport("test").call("observe")
    assert caught.value.stage == ("screenshot" if stage == "screenshot" else "unknown")
    assert "private-email-and-token" not in str(caught.value)


def test_unclassified_runtime_failure_records_graph_stage_without_private_body(tmp_path):
    class BrokenDevice:
        device_id = "test"

        def observe(self):
            raise RuntimeError("private-email-and-token")

    with Harness(tmp_path, BrokenDevice(), object()) as harness:
        result = harness.start(Task(goal="test", criteria=["test"]))
        events = harness.events(result["task_id"])
        assert result["status"] == "failed"
        assert "执行失败" in result["reason"]
        assert "private-email-and-token" not in json.dumps([result, events])
        assert next(e["data"]["stage"] for e in events if e["kind"] == "error") == "observe"


@pytest.mark.parametrize(
    "failure, expected_stage",
    [
        ("dump", "hierarchy"),
        ("screenshot", "screenshot"),
        ("changed", "page_changed"),
        (None, None),
    ],
)
def test_primary_display_observation_diagnostics(monkeypatch, capsys, failure, expected_stage):
    def fail():
        raise RuntimeError("private-email-and-token")

    reads = []

    def current():
        reads.append(1)
        return {
            "package": "com.test",
            "activity": "Other" if failure == "changed" and len(reads) > 1 else "Main",
        }

    frame = SimpleNamespace(thumbnail=lambda size: None, save=lambda out, **kw: out.write(b"png"))
    device = SimpleNamespace(
        settings={},
        app_current=current,
        dump_hierarchy=fail if failure == "dump" else lambda: "<hierarchy/>",
        screenshot=fail if failure == "screenshot" else lambda: frame,
    )
    monkeypatch.setattr(android_rpc, "connect_device", lambda serial: device)
    monkeypatch.setattr(android_rpc.sys, "stdin", io.StringIO('{"serial":"test","op":"observe"}'))
    android_rpc.main()
    output = capsys.readouterr().out
    assert "private-email-and-token" not in output
    data = json.loads(output)
    if expected_stage:
        assert data["error"]["observation_stage"] == expected_stage
    else:
        assert data["package"] == "com.test"
        assert data["image"] == "cG5n"
