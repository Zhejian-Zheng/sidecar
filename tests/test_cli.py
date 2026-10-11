import json
import os
import subprocess
import sys


def run_cli(home, *args):
    return subprocess.run(
        [sys.executable, "-m", "sidecar.cli", "--home", str(home), *args],
        text=True,
        capture_output=True,
        timeout=20,
        env={**os.environ, "PYTHONPATH": "src"},
    )


def test_offline_demo_runs_and_persists_result(tmp_path):
    run = run_cli(tmp_path, "demo", "--item", "复习架构")
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["status"] == "completed"
    assert result["mode"] == "offline_demo"
    saved = run_cli(tmp_path, "status", result["task_id"])
    assert json.loads(saved.stdout)["status"] == "completed"
    assert json.loads((tmp_path / "demo-device.json").read_text())["items"] == ["复习架构"]


def test_demo_handoff_can_resume_in_another_process(tmp_path):
    run = run_cli(tmp_path, "demo", "--require-login", "--item", "材料")
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["status"] == "waiting_user"
    assert run_cli(tmp_path, "demo-login").returncode == 0
    resumed = run_cli(tmp_path, "resume", result["task_id"], "--note", "已登录")
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["status"] == "completed"


def test_unknown_task_returns_readable_error(tmp_path):
    result = run_cli(tmp_path, "status", "missing")
    assert result.returncode != 0
    assert "Traceback" not in result.stderr


def test_new_demo_cannot_change_device_while_old_task_waits(tmp_path):
    first = json.loads(run_cli(tmp_path, "demo", "--require-login").stdout)
    assert run_cli(tmp_path, "demo-login").returncode == 0
    device_before = (tmp_path / "demo-device.json").read_text()
    attempted = run_cli(tmp_path, "demo", "--require-login")
    assert attempted.returncode != 0
    assert (tmp_path / "demo-device.json").read_text() == device_before
    assert (
        json.loads(run_cli(tmp_path, "resume", first["task_id"], "--note", "已处理").stdout)[
            "status"
        ]
        == "completed"
    )
