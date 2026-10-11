import subprocess
import sys

import pytest


def setup(home):
    from sidecar.jobs import JobQueue, Settings

    q = JobQueue(home)
    q.configure(Settings())
    return q


def test_job_runs_in_separate_process_and_persists_result(tmp_path):
    q = setup(tmp_path)
    job = q.submit("准备面试", ["复习架构", "录制演示"], request_id="once")
    duplicate = q.submit("准备面试", ["复习架构", "录制演示"], request_id="once")
    assert job["id"] == duplicate["id"]
    result = subprocess.run(
        [sys.executable, "-m", "sidecar.worker", "--home", str(tmp_path), "--once"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    result = q.get(job["id"])
    assert result["status"] == "completed"
    assert len(result["state"]["verification"]["checks"]) == 2


def test_cancel_queued_task_never_reaches_harness(tmp_path):
    from sidecar.worker import run_next

    q = setup(tmp_path)
    job = q.submit("测试", ["不应该创建"])
    q.control(job["id"], "cancel")
    assert not run_next(tmp_path)
    assert q.get(job["id"])["status"] == "cancelled"


def test_interrupted_worker_requires_user_before_next_task(tmp_path):
    from sidecar.worker import recover, run_next

    q = setup(tmp_path)
    first = q.submit("任务一", ["一"])
    q.claim()
    second = q.submit("任务二", ["二"])
    recover(tmp_path)
    assert q.get(first["id"])["status"] == "interrupted"
    assert not run_next(tmp_path)
    q.control(first["id"], "cancel")
    assert run_next(tmp_path)
    assert q.get(second["id"])["status"] == "completed"


def test_resume_rejects_running_and_empty_note(tmp_path):
    q = setup(tmp_path)
    job = q.submit("任务", ["一"])
    with pytest.raises(ValueError):
        q.resume(job["id"], "")
    q.claim()
    with pytest.raises(ValueError):
        q.resume(job["id"], "核对好了")


def test_idempotency_key_cannot_silently_change_task(tmp_path):
    q = setup(tmp_path)
    q.submit("任务", ["一"], request_id="same")
    with pytest.raises(ValueError):
        q.submit("另一个任务", ["二"], request_id="same")


def test_resume_preserves_original_plan(tmp_path, monkeypatch):
    from sidecar.contracts import Task
    from sidecar.demo import PREFIX, DemoDevice, DemoPolicy
    from sidecar.jobs import JobQueue, Settings
    from sidecar.worker import run_next

    class Model(DemoPolicy):
        plans = 0

        def plan(self, goal):
            self.plans += 1
            return Task(goal=goal, criteria=[PREFIX + "原始任务"])

    model = Model()
    device = DemoDevice(tmp_path / "engine")
    state = device.load()
    state["logged_in"] = False
    device.save(state)
    monkeypatch.setenv("SIDECAR_MODEL", "test")
    monkeypatch.delenv("SIDECAR_BASE_URL", raising=False)
    monkeypatch.setattr("sidecar.android.AndroidDevice", lambda *args: device)
    monkeypatch.setattr("sidecar.worker.LangChainModel.from_env", lambda: model)
    q = JobQueue(tmp_path)
    q.configure(Settings(mode="android", package="com.example.todo", model="test"))
    job = q.submit("完成原始任务")
    run_next(tmp_path)
    assert q.get(job["id"])["status"] == "waiting_user"
    state = device.load()
    state["logged_in"] = True
    device.save(state)
    q.resume(job["id"], "已登录")
    run_next(tmp_path)
    assert q.get(job["id"])["status"] == "completed"
    assert model.plans == 1
    assert q.state(job["id"])["task"]["criteria"] == [PREFIX + "原始任务"]


def test_accepted_cancel_wins_during_planning(tmp_path, monkeypatch):
    from sidecar.jobs import JobQueue, Settings
    from sidecar.worker import run_next

    q = JobQueue(tmp_path)
    q.configure(Settings(mode="android", package="com.example.todo", model="test"))
    job = q.submit("任务")

    class Model:
        def plan(self, goal):
            q.control(job["id"], "cancel")
            raise TimeoutError("provider timed out")

    monkeypatch.setenv("SIDECAR_MODEL", "test")
    monkeypatch.delenv("SIDECAR_BASE_URL", raising=False)
    monkeypatch.setattr("sidecar.worker.LangChainModel.from_env", lambda: Model())
    run_next(tmp_path)
    assert q.get(job["id"])["status"] == "cancelled"


@pytest.mark.parametrize("late_status", ["completed", "failed", "waiting_user"])
def test_queue_finish_preserves_accepted_cancel(tmp_path, late_status):
    q = setup(tmp_path)
    task_id = q.submit("任务", ["一"])["id"]
    q.claim()
    q.control(task_id, "cancel")
    q.finish(task_id, late_status, "迟到的结果")
    assert q.get(task_id)["status"] == "cancelled"
    assert q.get(task_id)["reason"] != "迟到的结果"


def test_queue_terminal_result_rejects_cancel_and_late_finish(tmp_path):
    q = setup(tmp_path)
    task_id = q.submit("任务", ["一"])["id"]
    q.claim()
    q.finish(task_id, "completed", "已验证")
    with pytest.raises(ValueError, match="任务已经结束"):
        q.control(task_id, "cancel")
    q.finish(task_id, "failed", "迟到的异常")
    assert q.get(task_id)["status"] == "completed"
    assert q.get(task_id)["reason"] == "已验证"


@pytest.mark.parametrize("after_restart", [False, True])
def test_cancel_between_engine_completion_and_queue_finish(tmp_path, after_restart):
    from sidecar.contracts import Task
    from sidecar.demo import PREFIX, DemoDevice, DemoPolicy
    from sidecar.harness import Harness
    from sidecar.worker import recover

    q = setup(tmp_path)
    task_id = q.submit("任务", ["一"])["id"]
    q.claim()
    with Harness(q.engine, DemoDevice(q.engine), DemoPolicy()) as h:
        result = h.start(Task(goal="任务", criteria=[PREFIX + "一"]), task_id=task_id)
    assert result["status"] == "completed"
    q.control(task_id, "cancel")
    if after_restart:
        recover(tmp_path)
    else:
        q.finish(task_id, result["status"], result["reason"])
    job = q.get(task_id)
    assert job["status"] == job["state"]["status"] == "cancelled"
    # 取消停止后续执行，但不删除已发生的动作与验证证据。
    assert job["state"]["verification"] == result["verification"]


def test_cancel_and_finish_from_concurrent_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    q = setup(tmp_path)
    task_id = q.submit("任务", ["一"])["id"]
    q.claim()
    barrier = Barrier(2)

    def cancel():
        barrier.wait(timeout=5)
        try:
            q.control(task_id, "cancel")
        except ValueError:
            return False
        return True

    def finish():
        barrier.wait(timeout=5)
        q.finish(task_id, "completed", "已验证")

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancellation = pool.submit(cancel)
        completion = pool.submit(finish)
        accepted = cancellation.result(timeout=10)
        completion.result(timeout=10)
    assert q.get(task_id)["status"] == ("cancelled" if accepted else "completed")
