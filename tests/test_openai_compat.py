import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from sidecar import openai_compat
from sidecar.jobs import JobQueue
from sidecar.openai_compat import ChatMessage, extract_user_goal, format_result


def client(home):
    from sidecar.server import create_app

    return TestClient(create_app(home, token="test-pairing-secret"))


def auth():
    return {"Authorization": "Bearer test-pairing-secret"}


def start_demo_worker(tmp_path):
    from sidecar.jobs import JobQueue, Settings
    from sidecar.worker import run_worker

    JobQueue(tmp_path).configure(Settings(mode="demo"))
    thread = threading.Thread(target=run_worker, args=(str(tmp_path),), daemon=True)
    thread.start()
    return thread


def test_models_requires_auth_and_lists_agent(tmp_path):
    with client(tmp_path) as c:
        assert c.get("/v1/models").status_code == 401
        data = c.get("/v1/models", headers=auth()).json()
        assert data["object"] == "list"
        assert data["data"][0]["id"] == "sidecar-agent"


def test_chat_requires_auth(tmp_path):
    with client(tmp_path) as c:
        response = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "你好"}]},
        )
        assert response.status_code == 401


def test_chat_completion_runs_demo_task(tmp_path):
    start_demo_worker(tmp_path)
    with client(tmp_path) as c:
        response = c.post(
            "/v1/chat/completions",
            headers=auth(),
            json={"messages": [{"role": "user", "content": "创建一个复习架构的待办"}]},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "chat.completion"
        assert data["model"] == "sidecar-agent"
        content = data["choices"][0]["message"]["content"]
        assert "任务已完成" in content
        assert "复习架构" in content


def test_chat_completion_handoff_returns_resume_hint(tmp_path):
    engine = tmp_path / "engine"
    engine.mkdir(parents=True, exist_ok=True)
    (engine / "demo-device.json").write_text(
        json.dumps({"logged_in": False, "screen": "list", "draft": "", "items": []})
    )
    start_demo_worker(tmp_path)
    with client(tmp_path) as c:
        response = c.post(
            "/v1/chat/completions",
            headers=auth(),
            json={"messages": [{"role": "user", "content": "创建一个待办"}]},
        )
        assert response.status_code == 200
        content = response.json()["choices"][0]["message"]["content"]
        assert "需要你介入" in content
        assert re.search(r"继续 [0-9a-f]{32}", content)


def test_chat_completion_resume_after_handoff(tmp_path):
    engine = tmp_path / "engine"
    engine.mkdir(parents=True, exist_ok=True)
    device = engine / "demo-device.json"
    device.write_text(json.dumps({"logged_in": False, "screen": "list", "draft": "", "items": []}))
    start_demo_worker(tmp_path)
    with client(tmp_path) as c:
        first = c.post(
            "/v1/chat/completions",
            headers=auth(),
            json={"messages": [{"role": "user", "content": "创建一个准备面试的待办"}]},
        )
        content = first.json()["choices"][0]["message"]["content"]
        task_id = re.search(r"继续 ([0-9a-f]{32})", content).group(1)
        device.write_text(
            json.dumps({"logged_in": True, "screen": "list", "draft": "", "items": []})
        )
        second = c.post(
            "/v1/chat/completions",
            headers=auth(),
            json={"messages": [{"role": "user", "content": f"继续 {task_id}"}]},
        )
        assert second.status_code == 200
        content = second.json()["choices"][0]["message"]["content"]
        assert "任务已完成" in content


def test_extract_user_goal_prefers_last_user_text():
    messages = [
        ChatMessage(role="user", content="第一条"),
        ChatMessage(role="assistant", content="回复"),
        ChatMessage(role="user", content=[{"type": "text", "text": "第二条"}]),
    ]
    assert extract_user_goal(messages) == "第二条"


def test_extract_user_goal_requires_user_message():
    try:
        extract_user_goal([ChatMessage(role="assistant", content="只有助手")])
    except ValueError:
        return
    raise AssertionError("没有用户消息时应抛出 ValueError")


def test_format_result_variants():
    completed = {
        "id": "a",
        "status": "completed",
        "reason": "ok",
        "state": {
            "verification": {
                "checks": [
                    {"criterion": "存在X", "passed": True, "evidence": "看到X"},
                ]
            }
        },
    }
    assert "任务已完成" in format_result(completed)
    assert "存在X" in format_result(completed) and "看到X" in format_result(completed)

    handoff = {"id": "abc123", "status": "waiting_user", "reason": "需要登录"}
    assert "需要你介入" in format_result(handoff) and "继续 abc123" in format_result(handoff)

    assert "任务失败" in format_result({"id": "a", "status": "failed", "reason": "超时"})
    assert "已取消" in format_result({"id": "a", "status": "cancelled", "reason": "用户取消"})


def test_pairing_token_uses_fixed_env(monkeypatch, tmp_path):
    from sidecar.server import pairing_token

    monkeypatch.setenv("SIDECAR_PAIRING_TOKEN", "my-fixed-key")
    assert pairing_token(tmp_path) == "my-fixed-key"


@pytest.fixture
def immediate_timeout(monkeypatch):
    # 保留真实轮询与队列行为，只把 HTTP 等待预算缩短为零。
    wait = openai_compat._wait
    monkeypatch.setattr(openai_compat, "_wait", lambda q, task_id, timeout: wait(q, task_id, 0))


def chat(c, content="创建待办", **kwargs):
    return c.post(
        "/v1/chat/completions",
        headers={**auth(), **kwargs.pop("headers", {})},
        json={"messages": [{"role": "user", "content": content}], **kwargs},
    )


def test_timeout_returns_task_id_and_query_hint(tmp_path, immediate_timeout):
    with client(tmp_path) as c:
        response = chat(c)
        assert response.status_code == 200
        data = response.json()
        task_id = JobQueue(tmp_path).list()[0]["id"]
        assert data.get("task_id") == task_id
        assert f"查询 {task_id}" in data["choices"][0]["message"]["content"]
        assert c.get(f"/api/tasks/{task_id}", headers=auth()).json()["status"] == "queued"


def test_timeout_retry_reuses_task_after_restart_and_completion(tmp_path, immediate_timeout):
    with client(tmp_path) as c:
        assert chat(c).status_code == 200
    q = JobQueue(tmp_path)
    task_id = q.list()[0]["id"]
    with client(tmp_path) as c:
        assert chat(c).status_code == 200
        q.finish(task_id, "completed")
        assert "任务已完成" in chat(c).json()["choices"][0]["message"]["content"]
    assert [job["id"] for job in q.list()] == [task_id]


def test_concurrent_retries_create_one_task(tmp_path, immediate_timeout):
    with client(tmp_path) as c, ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: chat(c), range(4)))
    assert all(response.status_code == 200 for response in responses)
    assert len(JobQueue(tmp_path).list()) == 1


def test_idempotency_keys_allow_intentional_repetition_and_reject_conflicts(
    tmp_path, immediate_timeout
):
    with client(tmp_path) as c:
        for key in ("first", "first", "second"):
            assert chat(c, headers={"Idempotency-Key": key}).status_code == 200
        conflict = chat(c, "另一个任务", headers={"Idempotency-Key": "first"})
        assert conflict.status_code == 409
    assert len(JobQueue(tmp_path).list()) == 2


def test_different_conversation_context_creates_new_task(tmp_path, immediate_timeout):
    with client(tmp_path) as c:
        assert chat(c).status_code == 200
        assert (
            chat(
                c,
                messages=[
                    {"role": "user", "content": "上一个任务"},
                    {"role": "assistant", "content": "已完成"},
                    {"role": "user", "content": "创建待办"},
                ],
            ).status_code
            == 200
        )
    assert len(JobQueue(tmp_path).list()) == 2


@pytest.mark.parametrize("status", ["queued", "paused", "completed"])
def test_query_is_read_only(tmp_path, immediate_timeout, status):
    q = JobQueue(tmp_path)
    task_id = q.submit("原任务", items=["原任务"])["id"]
    q.finish(task_id, status)
    with client(tmp_path) as c:
        response = chat(c, f"查询 {task_id}")
    assert response.status_code == 200
    assert response.json().get("task_id") == task_id
    assert q.get(task_id)["status"] == status
    assert len(q.list()) == 1


@pytest.mark.parametrize(
    "template",
    [
        "请不要继续 {id}",
        "请解释‘继续 {id}’",
        "继续 {id} 不要执行",
        "说明\n继续 {id}",
        "继续 {id}0",
    ],
)
def test_embedded_resume_text_does_not_resume(tmp_path, template):
    q = JobQueue(tmp_path)
    task_id = q.submit("原任务", items=["原任务"])["id"]
    q.control(task_id, "pause")
    openai_compat.run_completion(q, template.format(id=task_id), timeout=0)
    assert q.get(task_id)["status"] == "paused"


@pytest.mark.parametrize("template", ["继续 {id}", " 继续：{id} \n"])
def test_standalone_resume_command(tmp_path, template):
    q = JobQueue(tmp_path)
    task_id = q.submit("原任务", items=["原任务"])["id"]
    q.control(task_id, "pause")
    result = openai_compat.run_completion(q, template.format(id=task_id.upper()), timeout=0)
    assert result["id"] == task_id
    assert q.get(task_id)["status"] == "queued"
    assert len(q.list()) == 1


@pytest.mark.parametrize("command", ["查询", "继续"])
def test_unknown_task_command_returns_404_without_submission(tmp_path, immediate_timeout, command):
    with client(tmp_path) as c:
        response = chat(c, f"{command} {'0' * 32}")
    assert response.status_code == 404
    assert JobQueue(tmp_path).list() == []


@pytest.mark.parametrize("resume", [False, True])
def test_stream_rejected_before_task_submission_or_resume(tmp_path, immediate_timeout, resume):
    q = JobQueue(tmp_path)
    goal = "创建待办"
    if resume:
        task_id = q.submit("原任务", items=["原任务"])["id"]
        q.control(task_id, "pause")
        goal = f"继续 {task_id}"
    before = q.list()
    with client(tmp_path) as c:
        response = chat(c, goal, stream=True)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["param"] == "stream"
    assert "关闭" in error["message"]
    assert q.list() == before
