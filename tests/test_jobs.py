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
    from sidecar.jobs import JobQueue, Settings
    from sidecar.offline import PREFIX, OfflineDevice, OfflinePolicy
    from sidecar.worker import run_next

    class Model(OfflinePolicy):
        plans = 0

        def plan(self, goal):
            self.plans += 1
            return Task(goal=goal, criteria=[PREFIX + "原始任务"])

    model = Model()
    device = OfflineDevice(tmp_path / "engine")
    state = device.load()
    state["logged_in"] = False
    device.save(state)
    monkeypatch.setenv("SIDECAR_MODEL", "test")
    monkeypatch.delenv("SIDECAR_BASE_URL", raising=False)
    monkeypatch.setattr("sidecar.android.AndroidDevice", lambda *args, **kwargs: device)
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
    from sidecar.harness import Harness
    from sidecar.offline import PREFIX, OfflineDevice, OfflinePolicy
    from sidecar.worker import recover

    q = setup(tmp_path)
    task_id = q.submit("任务", ["一"])["id"]
    q.claim()
    with Harness(q.engine, OfflineDevice(q.engine), OfflinePolicy()) as h:
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


@pytest.mark.parametrize("cancel", [False, True])
def test_new_android_task_launches_app_unless_cancelled(tmp_path, monkeypatch, cancel):
    from sidecar.contracts import Task
    from sidecar.jobs import JobQueue, Settings
    from sidecar.offline import PREFIX, OfflineDevice, OfflinePolicy
    from sidecar.worker import run_next

    class Device(OfflineDevice):
        launched = False

        def launch(self):
            self.launched = True

        def observe(self):
            assert self.launched
            return super().observe()

    class Model(OfflinePolicy):
        def plan(self, goal):
            if cancel:
                q.control(job["id"], "cancel")
            return Task(goal=goal, criteria=[PREFIX + "测试"])

    d = Device(tmp_path / "engine")
    monkeypatch.setenv("SIDECAR_MODEL", "test")
    monkeypatch.delenv("SIDECAR_BASE_URL", raising=False)
    monkeypatch.setattr("sidecar.android.AndroidDevice", lambda *args, **kwargs: d)
    monkeypatch.setattr("sidecar.worker.LangChainModel.from_env", lambda: Model())
    q = JobQueue(tmp_path)
    q.configure(Settings(mode="android", package="com.example.todo", model="test"))
    job = q.submit("测试")
    run_next(tmp_path)
    assert d.launched is not cancel
    assert q.get(job["id"])["status"] == ("cancelled" if cancel else "completed")


def test_default_package_never_widens_the_allow_list():
    from sidecar.jobs import Settings

    # 只给 package：它同时充当唯一白名单。
    assert Settings(mode="android", package="com.a", model="m").allowed_packages == ["com.a"]
    # 只给白名单：默认目标取第一项。
    only_allowed = Settings(mode="android", allowed_packages=["com.b", "com.c"], model="m")
    assert only_allowed.package == "com.b"
    # 白名单已给出时，--package 不能扩大它。
    with pytest.raises(ValueError, match="不在允许列表"):
        Settings(mode="android", package="com.a", allowed_packages=["com.b"], model="m")
    with pytest.raises(ValueError, match="允许的 App"):
        Settings(mode="android", model="m")
    with pytest.raises(ValueError, match="包名"):
        Settings(mode="android", allowed_packages=["bad name"], model="m")
    # 非 ASCII 包名在启动校验就被拒绝，而不是等到设备侧失败。
    with pytest.raises(ValueError, match="包名"):
        Settings(mode="android", allowed_packages=["com.app.应用"], model="m")


def test_task_app_must_be_in_allow_list(tmp_path):
    from sidecar.jobs import JobQueue, Settings

    q = JobQueue(tmp_path)
    q.configure(
        Settings(mode="android", package="com.a", allowed_packages=["com.a", "com.b"], model="m")
    )
    assert q.target_package({"app": None}) == "com.a"
    assert q.target_package({"app": "com.b"}) == "com.b"
    with pytest.raises(ValueError, match="允许列表"):
        q.target_package({"app": "com.evil"})
    with pytest.raises(ValueError, match="允许列表"):
        q.submit("任务", app="com.evil")


def test_worker_binds_requested_app_with_allow_list(tmp_path, monkeypatch):
    from sidecar.contracts import Task
    from sidecar.jobs import JobQueue, Settings
    from sidecar.offline import PREFIX, OfflinePolicy
    from sidecar.worker import run_next

    seen = {}

    class Device:
        def __init__(self, *args, **kwargs):
            seen["package"] = args[1]
            seen["allowed"] = kwargs.get("allowed")

        def launch(self):
            pass

        def observe(self):
            raise RuntimeError("stop after construction")

    class Model(OfflinePolicy):
        def plan(self, goal):
            return Task(goal=goal, criteria=[PREFIX + "测试"])

    monkeypatch.setenv("SIDECAR_MODEL", "m")
    monkeypatch.delenv("SIDECAR_BASE_URL", raising=False)
    monkeypatch.setattr("sidecar.android.AndroidDevice", Device)
    monkeypatch.setattr("sidecar.worker.LangChainModel.from_env", lambda: Model())
    q = JobQueue(tmp_path)
    q.configure(
        Settings(mode="android", package="com.a", allowed_packages=["com.a", "com.b"], model="m")
    )
    q.submit("任务", app="com.b")
    run_next(tmp_path)
    assert seen == {"package": "com.b", "allowed": ["com.a", "com.b"]}


@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_worker_closes_finished_hidden_app_but_keeps_handoff(
    tmp_path, monkeypatch, handoff, cleanup_failure
):
    from sidecar.displays import DisplayBinding
    from sidecar.jobs import JobQueue, Settings
    from sidecar.offline import PREFIX, OfflineDevice, OfflinePolicy
    from sidecar.worker import run_next

    class Device(OfflineDevice):
        closed = False

        def launch(self):
            pass

        def execution_lock(self):
            from contextlib import nullcontext

            return nullcontext()

        def close_app(self):
            self.closed = True
            if cleanup_failure:
                raise RuntimeError("device unavailable")

    device = Device(tmp_path / "engine")
    state = device.load()
    state["logged_in"] = not handoff
    device.save(state)
    monkeypatch.setenv("SIDECAR_MODEL", "test")
    monkeypatch.delenv("SIDECAR_BASE_URL", raising=False)
    monkeypatch.setattr("sidecar.android.AndroidDevice", lambda *a, **kw: device)
    monkeypatch.setattr("sidecar.worker.LangChainModel.from_env", lambda: OfflinePolicy())
    q = JobQueue(tmp_path)
    q.configure(
        Settings(
            mode="android",
            package="com.clock",
            model="test",
            display=DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-a"),
        )
    )
    job = q.submit("task", criteria=[PREFIX + "test"])
    run_next(tmp_path)
    assert q.get(job["id"])["status"] == ("waiting_user" if handoff else "completed")
    assert device.closed is not handoff
    assert any(e["kind"] == "cleanup_error" for e in q.events(job["id"])) == (
        cleanup_failure and not handoff
    )


def test_recover_settles_engine_left_behind_by_a_terminal_queue_row(tmp_path):
    """队列已终态、引擎仍非终态时会永久锁死工作区；recover 必须收尾引擎。"""
    from sidecar.jobs import JobQueue
    from sidecar.store import Store
    from sidecar.worker import recover

    q = JobQueue(tmp_path)
    job = q.submit("任务", items=["事项"])
    task_id = job["id"]
    q.claim()
    # 人为制造 H1 的组合：队列行已是终态，引擎里却留着未完成的现场。
    store = Store(q.engine)
    try:
        store.create(
            {
                "task_id": task_id,
                "task": {"goal": "任务", "criteria": ["待办存在：事项"]},
                "device_id": "offline-demo:x",
                "status": "waiting_user",
                "reason": "等待",
                "decisions": 0,
                "history": [],
                "observation": None,
                "action": None,
                "verification": None,
                "fingerprint": "",
                "stalls": 0,
                "limits": {"decisions": 30, "stalls": 4, "seconds": 180},
            }
        )
    finally:
        store.close()
    q.finish(task_id, "cancelled", "已取消")

    recover(tmp_path)

    store = Store(q.engine)
    try:
        assert store.get(task_id)["status"] == "cancelled"
    finally:
        store.close()
    # 工作区不再被占用，新任务可以正常登记。
    assert q.submit("新任务", items=["第二项"])["status"] == "queued"


def test_config_drift_does_not_consume_the_queue(tmp_path, monkeypatch):
    """模型配置漂移时，worker 不应把排队的任务消费掉。"""
    from sidecar.jobs import JobQueue, Settings
    from sidecar.worker import run_next

    q = JobQueue(tmp_path)
    q.configure(Settings(mode="android", package="com.a", model="expected-model"))
    job = q.submit("任务")
    monkeypatch.setenv("SIDECAR_MODEL", "different-model")
    monkeypatch.delenv("SIDECAR_BASE_URL", raising=False)

    assert run_next(tmp_path) is False
    assert q.raw(job["id"])["status"] == "queued"


def test_worker_fallback_defers_to_engine_terminal_status(tmp_path, monkeypatch):
    """兜底异常不能把引擎里已经完成的任务改写成失败。"""
    from sidecar import harness as harness_module
    from sidecar.jobs import JobQueue, Settings
    from sidecar.worker import run_next

    q = JobQueue(tmp_path)
    q.configure(Settings(mode="demo"))
    job = q.submit("任务", items=["事项"])

    original_exit = harness_module.Harness.__exit__

    def failing_exit(self, *args):
        original_exit(self, *args)
        raise RuntimeError("boom after completion")

    monkeypatch.setattr(harness_module.Harness, "__exit__", failing_exit)
    run_next(tmp_path)

    assert q.get(job["id"])["status"] == "completed"


@pytest.mark.parametrize("signal,expected", [("pause", "paused"), ("cancel", "cancelled")])
def test_fresh_control_during_resume_prevents_device_actions(
    tmp_path, monkeypatch, signal, expected
):
    from sidecar.offline import OfflineDevice
    from sidecar.worker import run_next

    q = setup(tmp_path)
    device = OfflineDevice(q.engine)
    screen = device.load()
    screen["logged_in"] = False
    device.save(screen)
    task_id = q.submit("任务", ["不能提前创建"])["id"]
    run_next(tmp_path)
    assert q.get(task_id)["status"] == "waiting_user"
    screen["logged_in"] = True
    device.save(screen)
    q.resume(task_id, "已登录")
    original_claim = type(q).claim

    def claim_then_control(self):
        job = original_claim(self)
        if job:
            self.control(job["id"], signal)
        return job

    monkeypatch.setattr(type(q), "claim", claim_then_control)
    run_next(tmp_path)
    assert q.get(task_id)["status"] == expected
    assert device.load()["items"] == []
    if signal == "pause":
        monkeypatch.setattr(type(q), "claim", original_claim)
        q.resume(task_id, "再次确认继续")
        run_next(tmp_path)
        assert q.get(task_id)["status"] == "completed"
        assert device.load()["items"] == ["不能提前创建"]


def test_clear_queue_cancels_all_unfinished_jobs_and_keeps_history(tmp_path):
    from sidecar.jobs import JobQueue

    q = JobQueue(tmp_path)
    done = q.submit("done", items=["done"])
    q.finish(done["id"], "completed")
    jobs = [q.submit(str(i), items=[str(i)]) for i in range(55)]
    q.control(jobs[0]["id"], "pause")
    result = q.clear()
    assert result == {"cancelled": 55, "pending": 0}
    assert all(q.get(job["id"])["status"] == "cancelled" for job in jobs)
    assert q.get(done["id"])["status"] == "completed"
    assert q.claim() is None
    assert q.clear() == {"cancelled": 0, "pending": 0}


def test_clear_queue_signals_executor_and_releases_saved_handoff(tmp_path):
    from sidecar.jobs import JobQueue
    from sidecar.worker import run_next

    q = JobQueue(tmp_path)
    first = q.submit("first", items=["first"])
    q.engine.mkdir()
    (q.engine / "offline-device.json").write_text(
        '{"logged_in": false, "screen": "list", "draft": "", "items": []}'
    )
    run_next(tmp_path)
    assert q.get(first["id"])["status"] == "waiting_user"
    q.clear()
    assert q.state(first["id"])["status"] == "cancelled"
    second = q.submit("second", items=["second"])
    q.claim()
    assert q.clear() == {"cancelled": 0, "pending": 1}
    assert q.get(second["id"])["pending_control"] == "cancel"
    third = q.submit("third", items=["third"])
    assert q.claim() is None
    q.finish(second["id"], "completed")
    assert q.get(second["id"])["status"] == "cancelled"
    assert q.claim()["id"] == third["id"]


def test_clear_queue_reaches_active_engine_before_any_device_action(tmp_path):
    from sidecar.contracts import Task
    from sidecar.harness import Harness
    from sidecar.jobs import JobQueue
    from sidecar.offline import OfflineDevice, OfflinePolicy

    q = JobQueue(tmp_path)
    job = q.submit("first", items=["first"])
    q.claim()
    q.submit("second", items=["second"])

    def clear_during_execution():
        assert q.clear() == {"cancelled": 1, "pending": 1}
        assert q.claim() is None

    with Harness(q.engine, OfflineDevice(q.engine), OfflinePolicy()) as harness:
        result = harness.start(
            Task(goal="first", criteria=["待办存在：first"]),
            task_id=job["id"],
            prepare=clear_during_execution,
        )
    assert result["status"] == "cancelled"
    assert OfflineDevice(q.engine).load()["items"] == []
    assert not any(event["kind"] == "action_returned" for event in q.events(job["id"]))
    q.finish(job["id"], result["status"], result["reason"])
    assert q.get(job["id"])["status"] == "cancelled"


def test_resume_never_clears_pending_cancel(tmp_path):
    q = setup(tmp_path)
    task_id = q.submit("任务", ["一"])["id"]
    with q.db() as db:
        db.execute("UPDATE jobs SET status='paused', control='cancel' WHERE id=?", (task_id,))
    with pytest.raises(ValueError, match="当前任务不能恢复"):
        q.resume(task_id, "已核对")
    assert q.raw(task_id)["control"] == "cancel"


@pytest.mark.parametrize("operation", ["pause", "cancel", "clear"])
@pytest.mark.parametrize("with_engine", [False, True])
def test_queue_control_needs_no_agent_construction(tmp_path, monkeypatch, operation, with_engine):
    from sidecar.harness import Harness
    from sidecar.offline import OfflineDevice, OfflinePolicy
    from sidecar.store import Store

    q = setup(tmp_path)
    task_id = q.submit("任务", ["一"])["id"]
    if with_engine:
        q.claim()
        with q.db() as db:
            db.execute("UPDATE jobs SET status='paused' WHERE id=?", (task_id,))
        store = Store(q.engine)
        store.create({"task_id": task_id, "status": "paused", "reason": ""})
        store.close()

    def forbidden(*args, **kwargs):
        raise AssertionError("control must not construct an agent, device or model")

    for cls in (Harness, OfflineDevice, OfflinePolicy):
        monkeypatch.setattr(cls, "__init__", forbidden)
    if operation == "clear":
        assert q.clear() == {"cancelled": 1, "pending": 0}
    else:
        q.control(task_id, operation)
    assert q.get(task_id)["status"] == ("paused" if operation == "pause" else "cancelled")
    assert not (q.engine / "checkpoints.sqlite").exists()


def test_clear_continues_past_completed_engine_and_closes_connections(tmp_path, monkeypatch):
    from sidecar.store import Store

    q = setup(tmp_path)
    first = q.submit("first", ["one"])["id"]
    second = q.submit("second", ["two"])["id"]
    store = Store(q.engine)
    store.create({"task_id": first, "status": "completed", "reason": "verified"})
    store.event(first, "verification", {"evidence": "saved"})
    store.create({"task_id": second, "status": "waiting_user", "reason": "review"})
    store.close()

    # Retain connections so CPython cannot hide a missing explicit close by GC.
    connections = []
    original_init = Store.__init__

    def track_connection(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        connections.append(self.connection)

    monkeypatch.setattr(Store, "__init__", track_connection)
    assert q.clear() == {"cancelled": 2, "pending": 0}
    import sqlite3

    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert q.state(second)["status"] == "cancelled"
    assert q.events(first)[0]["data"]["evidence"] == "saved"
    store = Store(q.engine)
    try:
        store.create({"task_id": "next", "status": "running", "reason": ""})
        assert store.get("next")["status"] == "running"
    finally:
        store.close()
