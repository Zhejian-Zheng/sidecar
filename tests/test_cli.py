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
    assert json.loads((tmp_path / "offline-device.json").read_text())["items"] == ["复习架构"]


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
    device_before = (tmp_path / "offline-device.json").read_text()
    attempted = run_cli(tmp_path, "demo", "--require-login")
    assert attempted.returncode != 0
    assert (tmp_path / "offline-device.json").read_text() == device_before
    assert (
        json.loads(run_cli(tmp_path, "resume", first["task_id"], "--note", "已处理").stdout)[
            "status"
        ]
        == "completed"
    )


def test_export_demo_evidence_preserves_mode_and_never_overwrites(tmp_path):
    home = tmp_path / "runtime"
    result = json.loads(run_cli(home, "demo", "--item", "演示验收").stdout)
    output = tmp_path / "evidence"
    exported = run_cli(home, "export", result["task_id"], "--output", str(output))
    assert exported.returncode == 0, exported.stderr
    report = json.loads((output / "report.json").read_text())
    assert report["mode"] == "offline_demo"
    assert report["status"] == "completed"
    assert report["verification"]["checks"][0]["passed"] is True
    assert json.loads((output / "events.json").read_text())
    assert not (output / "observation.png").exists()
    repeated = run_cli(home, "export", result["task_id"], "--output", str(output))
    assert repeated.returncode != 0
    assert json.loads((output / "report.json").read_text()) == report


def test_export_queued_task_does_not_claim_completion(tmp_path):
    from sidecar.jobs import JobQueue

    home = tmp_path / "runtime"
    task = JobQueue(home).submit("等待执行", items=["一"])
    output = tmp_path / "evidence"
    exported = run_cli(home, "export", task["id"], "--output", str(output))
    assert exported.returncode == 0, exported.stderr
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "queued"
    assert report["verification"] is None
    assert json.loads((output / "events.json").read_text()) == []


def test_export_android_screenshot_matches_verification_observation(tmp_path):
    import base64

    from sidecar.jobs import JobQueue, Settings
    from sidecar.store import Store

    home = tmp_path / "runtime"
    q = JobQueue(home)
    q.configure(Settings(mode="android", package="com.google.android.deskclock", model="test"))
    task_id = q.submit("核对闹钟")["id"]
    store = Store(q.engine)
    try:
        store.create(
            {
                "task_id": task_id,
                "status": "completed",
                "reason": "verified",
                "task": {"goal": "核对闹钟", "criteria": ["已开启"]},
                "verification": {"observation_id": "screen-1", "checks": []},
                "observation": {
                    "id": "screen-1",
                    "text": "11:58 PM",
                    "image": "data:image/png;base64," + base64.b64encode(b"fixture").decode(),
                },
            }
        )
    finally:
        store.close()
    q.finish(task_id, "completed")
    output = tmp_path / "evidence"
    exported = run_cli(home, "export", task_id, "--output", str(output))
    assert exported.returncode == 0, exported.stderr
    report = json.loads((output / "report.json").read_text())
    assert report["mode"] == "android"
    assert report["observation"]["id"] == report["verification"]["observation_id"]
    assert "image" not in report["observation"]
    assert (output / "observation.png").read_bytes() == b"fixture"


def test_clear_queue_cli_cancels_tasks_and_handles_empty_queue(tmp_path):
    from sidecar.jobs import JobQueue

    q = JobQueue(tmp_path)
    job = q.submit("queued", items=["queued"])
    result = run_cli(tmp_path, "clear-queue")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"cancelled": 1, "pending": 0}
    assert q.get(job["id"])["status"] == "cancelled"
    result = run_cli(tmp_path, "clear-queue")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"cancelled": 0, "pending": 0}


def test_clear_queue_cli_settles_orphaned_executor(tmp_path):
    from sidecar.jobs import JobQueue

    q = JobQueue(tmp_path)
    job = q.submit("orphan", items=["orphan"])
    q.claim()
    result = run_cli(tmp_path, "clear-queue")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"cancelled": 1, "pending": 0}
    assert q.get(job["id"])["status"] == "cancelled"
