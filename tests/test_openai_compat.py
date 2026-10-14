import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from openai import OpenAI

from sidecar import openai_compat
from sidecar.jobs import JobQueue
from sidecar.openai_compat import ChatMessage, extract_user_goal, format_result


def client(home):
    from sidecar.server import create_app

    return TestClient(create_app(home, token="test-pairing-secret"))


def auth():
    return {"Authorization": "Bearer test-pairing-secret"}


def test_confirmation_handoff_reports_verified_preparation_and_pending_final_step():
    content = format_result(
        {
            "id": "test-confirmation",
            "status": "waiting_user",
            "reason": "准备已完成；最终发送未执行",
            "state": {
                "observation": {"confirmation_targets": {"send": "Send"}},
                "verification": {
                    "checks": [
                        {"criterion": "内容已填写", "passed": True, "evidence": "Hello visible"},
                        {
                            "criterion": "邮件已发送",
                            "passed": False,
                            "requires_confirmation": True,
                            "evidence": "Send pending",
                        },
                    ]
                },
            },
        }
    )
    assert "内容已填写：已核对" in content
    assert "邮件已发送：待你最终提交" in content
    assert "最终发送未执行" in content
    assert "不会授权自动点击" in content


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


@pytest.mark.parametrize("stream", [False, True])
def test_chat_requires_auth(tmp_path, stream):
    with client(tmp_path) as c:
        response = c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "你好"}], "stream": stream},
        )
        assert response.status_code == 401
    assert JobQueue(tmp_path).list() == []


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
        assert "离线演示已完成" in content
        assert "复习架构" in content


def test_chat_completion_handoff_returns_resume_hint(tmp_path):
    engine = tmp_path / "engine"
    engine.mkdir(parents=True, exist_ok=True)
    (engine / "offline-device.json").write_text(
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
    device = engine / "offline-device.json"
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
        assert "离线演示已完成" in content


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
        assert "离线演示已完成" in chat(c).json()["choices"][0]["message"]["content"]
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


def stream_chunks(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert frames[-1] == "[DONE]"
    chunks = [json.loads(frame) for frame in frames[:-1]]
    assert len({chunk["id"] for chunk in chunks}) == 1
    assert len({chunk["created"] for chunk in chunks}) == 1
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert all(chunk["model"] == "sidecar-agent" for chunk in chunks)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    return chunks


def stream_text(chunks):
    return "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks)


def test_stream_runs_demo_and_returns_result(tmp_path):
    start_demo_worker(tmp_path)
    with client(tmp_path) as c:
        chunks = stream_chunks(chat(c, "创建流式演示待办", stream=True))
    text = stream_text(chunks)
    assert "离线演示已完成" in text and "流式演示待办" in text
    task_id = JobQueue(tmp_path).list()[0]["id"]
    assert task_id in text
    assert all(chunk["task_id"] == task_id for chunk in chunks)


@pytest.mark.parametrize("status", ["queued", "paused", "waiting_user", "failed", "cancelled"])
def test_stream_query_returns_current_status_without_side_effects(tmp_path, status):
    q = JobQueue(tmp_path)
    task_id = q.submit("原任务", items=["原任务"])["id"]
    q.finish(task_id, status, "状态说明")
    before = q.list()
    with client(tmp_path) as c:
        chunks = stream_chunks(chat(c, f"查询 {task_id}", stream=True))
    assert format_result(before[0], mode="demo") in stream_text(chunks)
    assert q.list() == before


def test_stream_resume_and_handoff(tmp_path):
    q = JobQueue(tmp_path)
    task_id = q.submit("原任务", items=["原任务"])["id"]
    q.control(task_id, "pause")
    q.engine.mkdir()
    (q.engine / "offline-device.json").write_text(
        json.dumps({"logged_in": False, "screen": "list", "draft": "", "items": []})
    )
    start_demo_worker(tmp_path)
    with client(tmp_path) as c:
        chunks = stream_chunks(chat(c, f"继续 {task_id}", stream=True))
    assert f"继续 {task_id}" in stream_text(chunks)
    assert q.get(task_id)["status"] == "waiting_user"
    assert len(q.list()) == 1


def test_stream_timeout_heartbeat_and_retry_without_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(openai_compat, "COMPLETION_TIMEOUT", 0.1, raising=False)
    monkeypatch.setattr(openai_compat, "HEARTBEAT_INTERVAL", 0.01, raising=False)
    with client(tmp_path) as c:
        response = chat(c, stream=True)
        chunks = stream_chunks(response)
        task_id = JobQueue(tmp_path).list()[0]["id"]
        assert f"查询 {task_id}" in stream_text(chunks)
        assert ": keep-alive" in response.text
        # stream_options 和 stream 不应改变同一次任务的去重身份。
        retry = chat(c, stream=False, stream_options={"include_usage": True})
        assert retry.json()["task_id"] == task_id
    assert len(JobQueue(tmp_path).list()) == 1


def test_openai_sdk_consumes_stream_and_optional_usage(tmp_path):
    start_demo_worker(tmp_path)
    with client(tmp_path) as c:
        sdk = OpenAI(api_key="test-pairing-secret", base_url="http://testserver/v1", http_client=c)
        chunks = list(
            sdk.chat.completions.create(
                model="sidecar-agent",
                messages=[{"role": "user", "content": "SDK 流式待办"}],
                stream=True,
                stream_options={"include_usage": True},
            )
        )
    assert "离线演示已完成" in "".join(
        chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices
    )
    assert chunks[-2].choices[0].finish_reason == "stop"
    assert chunks[-1].choices == []
    assert chunks[-1].usage.total_tokens == 0


@pytest.mark.parametrize("goal,expected", [("", 400), (f"查询 {'0' * 32}", 404)])
def test_stream_validation_happens_before_sse_headers(tmp_path, goal, expected):
    with client(tmp_path) as c:
        response = chat(c, goal, stream=True)
    assert response.status_code == expected
    assert response.headers["content-type"].startswith("application/json")
    assert JobQueue(tmp_path).list() == []


@pytest.fixture
def live_client(tmp_path):
    from sidecar.server import create_app

    # 真正的 socket 验证首段即时到达；TestClient 会缓冲整个响应，不能证明这一点。
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(
            uvicorn.Config(create_app(tmp_path, token="test-pairing-secret"), log_level="error")
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            with httpx.Client(
                base_url=f"http://127.0.0.1:{sock.getsockname()[1]}",
                headers=auth(),
                timeout=3,
                trust_env=False,
            ) as c:
                yield c
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_live_stream_delivers_task_id_before_work_and_disconnect_preserves_task(
    tmp_path, live_client
):
    from sidecar.worker import run_next

    body = {
        "model": "sidecar-agent",
        "messages": [{"role": "user", "content": "连接测试"}],
        "stream": True,
    }
    with live_client.stream("POST", "/v1/chat/completions", json=body) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("data: "):
                first = json.loads(line[6:])
                break
        task_id = first["task_id"]
        assert task_id in first["choices"][0]["delta"]["content"]
        assert JobQueue(tmp_path).get(task_id)["status"] == "queued"
    assert JobQueue(tmp_path).get(task_id)["status"] == "queued"
    assert run_next(tmp_path)
    # 客户端关闭响应后，worker 仍能完成；重试流式请求仍返回同一任务。
    response = live_client.post("/v1/chat/completions", json=body)
    assert "离线演示已完成" in stream_text(stream_chunks(response))
    assert [job["id"] for job in JobQueue(tmp_path).list()] == [task_id]


def test_live_stream_pushes_status_change_then_completion(tmp_path, live_client):
    q = JobQueue(tmp_path)
    with live_client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "进度测试"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        lines = response.iter_lines()
        chunks = (json.loads(line[6:]) for line in lines if line.startswith("data: "))
        first = next(chunks)
        task_id = first["task_id"]
        q.finish(task_id, "executing")
        for chunk in chunks:
            if "正在执行" in chunk["choices"][0]["delta"].get("content", ""):
                break
        else:
            pytest.fail("没有在任务执行期间收到状态更新")
        q.finish(task_id, "completed")
        remaining = list(lines)
        assert any("离线演示已完成" in line for line in remaining)
        assert "data: [DONE]" in remaining


@pytest.mark.parametrize("stream", [False, True])
def test_demo_alarm_request_is_labeled_as_simulation_without_history_leak(tmp_path, stream):
    q = JobQueue(tmp_path)
    q.engine.mkdir()
    (q.engine / "offline-device.json").write_text(
        json.dumps(
            {
                "logged_in": True,
                "screen": "list",
                "draft": "",
                "items": ["先前的私人待办"],
            }
        )
    )
    start_demo_worker(tmp_path)
    with client(tmp_path) as c:
        response = chat(c, "帮我设置晚上9点的闹钟", stream=stream)
    text = (
        stream_text(stream_chunks(response))
        if stream
        else response.json()["choices"][0]["message"]["content"]
    )
    assert "离线演示已完成" in text
    assert "不会操作手机或真实 App" in text
    assert "任务已完成" not in text
    assert "先前的私人待办" not in text
    assert "帮我设置晚上9点的闹钟" in text
    assert text.count("任务编号：") == 1


def test_historical_demo_result_keeps_label_after_switching_to_android(tmp_path):
    from sidecar.jobs import Settings
    from sidecar.worker import run_next

    q = JobQueue(tmp_path)
    job = q.submit("历史演示", items=["历史演示"])
    run_next(tmp_path)
    q.configure(Settings(mode="android", package="test.clock", model="test-model"))
    with client(tmp_path) as c:
        response = chat(c, f"查询 {job['id']}")
    text = response.json()["choices"][0]["message"]["content"]
    assert "离线演示已完成" in text
    assert "不会操作手机或真实 App" in text


def test_android_result_is_not_presented_as_demo():
    job = {
        "id": "android-test",
        "status": "completed",
        "state": {
            "device_id": "android:test-clock",
            "verification": {
                "checks": [
                    {
                        "criterion": "21:00 闹钟存在",
                        "passed": True,
                        "evidence": "时钟页面显示 21:00",
                    },
                ]
            },
        },
    }
    text = format_result(job)
    assert "任务已完成" in text and "时钟页面显示 21:00" in text
    assert "离线演示" not in text


def test_chat_goal_routes_to_named_allowed_app_before_queueing(tmp_path):
    from sidecar.jobs import JobQueue, Settings
    from sidecar.openai_compat import prepare_completion

    q = JobQueue(tmp_path)
    q.configure(
        Settings(
            mode="android",
            package="com.tencent.weread",
            allowed_packages=["com.tencent.weread", "notion.id", "com.custom.notes"],
            app_aliases={"com.custom.notes": ["我的笔记"]},
            model="test",
        )
    )
    for goal, package in [
        ("帮我在notion中的private创建测试", "notion.id"),
        ("在微信读书找西游记", "com.tencent.weread"),
        ("在我的笔记新建清单", "com.custom.notes"),
    ]:
        job, _ = prepare_completion(q, goal)
        assert job["request"]["app"] == package
        assert prepare_completion(q, goal)[0]["id"] == job["id"]


def test_unallowed_or_ambiguous_chat_app_is_rejected_before_queueing(tmp_path):
    from sidecar.jobs import JobQueue, Settings
    from sidecar.openai_compat import prepare_completion

    q = JobQueue(tmp_path)
    q.configure(Settings(mode="android", package="com.tencent.weread", model="test"))
    with pytest.raises(ValueError, match="允许"):
        prepare_completion(q, "在Notion创建页面")
    assert not q.list()
    q.configure(
        Settings(
            mode="android",
            package="com.tencent.weread",
            allowed_packages=["com.tencent.weread", "notion.id"],
            model="test",
        )
    )
    with pytest.raises(ValueError, match="多个"):
        prepare_completion(q, "从微信读书复制到Notion")
    assert not q.list()


def test_chat_retry_keeps_original_target_after_app_configuration_changes(tmp_path):
    from sidecar.jobs import JobQueue, Settings
    from sidecar.openai_compat import prepare_completion

    q = JobQueue(tmp_path)
    q.configure(Settings(mode="android", package="com.a", model="test"))
    first, _ = prepare_completion(q, "创建清单", "stable")
    q.control(first["id"], "cancel")
    q.configure(
        Settings(mode="android", package="com.b", allowed_packages=["com.b", "com.c"], model="test")
    )
    retry, _ = prepare_completion(q, "创建清单", "stable")
    assert retry["id"] == first["id"]
    assert retry["request"]["app"] == "com.a"
    assert len(q.list()) == 1


@pytest.mark.parametrize(
    "template,expected",
    [
        ("取消 Gmail 的任务{id}", "cancelled"),
        ("停止任务 {id}", "cancelled"),
        ("请暂停任务：{id}", "paused"),
        ("恢复任务{id}", "queued"),
        ("查看任务{id}的进度", "paused"),
        ("状态 {id}", "paused"),
    ],
)
def test_chat_control_aliases(tmp_path, template, expected):
    q = JobQueue(tmp_path)
    job = q.submit("原任务", items=["原任务"])
    q.control(job["id"], "pause")
    result, _ = openai_compat.prepare_completion(q, template.format(id=job["id"]))
    assert result["id"] == job["id"]
    assert q.get(job["id"])["status"] == expected
    assert len(q.list()) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "command", ["帮助", "任务列表", "查看队列", "取消任务", "取消 " + "a" * 32 + " " + "b" * 32]
)
def test_chat_information_never_submits(tmp_path, immediate_timeout, monkeypatch, stream, command):
    monkeypatch.setattr(openai_compat, "COMPLETION_TIMEOUT", 0)
    with client(tmp_path) as c:
        response = chat(c, command, stream=stream)
    assert response.status_code == 200
    content = (
        stream_text(stream_chunks(response))
        if stream
        else response.json()["choices"][0]["message"]["content"]
    )
    assert content
    assert JobQueue(tmp_path).list() == []


def test_queued_result_identifies_blocking_handoff(tmp_path):
    q = JobQueue(tmp_path)
    first = q.submit("first", items=["first"])
    q.finish(first["id"], "waiting_user", "check input")
    second = q.submit("second", items=["second"])
    result, wait = openai_compat.prepare_completion(q, "查询 " + second["id"])
    text = format_result(result)
    assert not wait
    assert "等待执行" in text and "仍在执行中" not in text
    assert first["id"] in text
    assert "取消 " + first["id"] in text


@pytest.mark.parametrize("template", ["不要取消 {id}", "请解释“取消 {id}”", "取消 {id} 不要执行"])
def test_negated_and_quoted_cancel_does_not_control(tmp_path, template):
    q = JobQueue(tmp_path)
    job = q.submit("original", items=["original"])
    openai_compat.prepare_completion(q, template.format(id=job["id"]))
    assert q.get(job["id"])["status"] == "queued"


@pytest.mark.parametrize("template", ["取消任务{id}的进度", "取消不要停止的任务{id}"])
def test_ambiguous_control_does_not_mutate_queue(tmp_path, template):
    q = JobQueue(tmp_path)
    job = q.submit("original", items=["original"])
    openai_compat.prepare_completion(q, template.format(id=job["id"]))
    assert q.get(job["id"])["status"] == "queued"
    assert len(q.list()) == 1


@pytest.mark.parametrize("stream", [False, True])
def test_cancel_through_http_is_idempotent(tmp_path, stream):
    q = JobQueue(tmp_path)
    job = q.submit("original", items=["original"])
    with client(tmp_path) as c:
        for _ in range(2):
            response = chat(c, "取消 gamil 的任务" + job["id"], stream=stream)
            text = (
                stream_text(stream_chunks(response))
                if stream
                else response.json()["choices"][0]["message"]["content"]
            )
            assert "已取消" in text
    assert q.get(job["id"])["status"] == "cancelled"
    assert len(q.list()) == 1


def test_chat_queue_lists_blocker_without_modification(tmp_path):
    q = JobQueue(tmp_path)
    first = q.submit("first", items=["first"])
    q.control(first["id"], "pause")
    second = q.submit("second", items=["second"])
    result, wait = openai_compat.prepare_completion(q, "查看队列")
    text = format_result(result)
    assert not wait
    assert first["id"] in text and second["id"] in text
    assert "已暂停" in text and "等待执行" in text
    assert q.get(first["id"])["status"] == "paused"
    assert q.get(second["id"])["status"] == "queued"


@pytest.mark.parametrize(
    "goal",
    ["查看 Gmail 中关于任务分配的邮件", "查询今天的任务安排", "查看任务安排"],
)
def test_normal_goals_mentioning_tasks_are_submitted(tmp_path, goal):
    q = JobQueue(tmp_path)
    job, wait = openai_compat.prepare_completion(q, goal)
    assert wait
    assert job["status"] == "queued"
    assert q.get(job["id"])["request"]["goal"] == goal
    assert len(q.list()) == 1


@pytest.mark.parametrize(
    "command", ["暂停", "取消任务", "请查看任务编号：abc123", "取消 Gmail 的任务"]
)
def test_incomplete_queue_commands_still_do_not_submit(tmp_path, command):
    q = JobQueue(tmp_path)
    job, wait = openai_compat.prepare_completion(q, command)
    assert not wait
    assert job["status"] == "information"
    assert q.list() == []


@pytest.mark.parametrize("stream", [False, True])
def test_acknowledged_pause_reports_resume_instructions(tmp_path, stream):
    q = JobQueue(tmp_path)
    job = q.submit("original", items=["original"])
    with client(tmp_path) as c:
        for command in ("暂停 ", "查询 "):
            response = chat(c, command + job["id"], stream=stream)
            text = (
                stream_text(stream_chunks(response))
                if stream
                else response.json()["choices"][0]["message"]["content"]
            )
            assert "已暂停" in text
            assert "等待当前操作结束" not in text
            assert "继续 " + job["id"] in text
    assert "pending_control" not in q.get(job["id"])
    assert q.claim() is None


def test_unacknowledged_pause_remains_pending_until_worker_finishes(tmp_path):
    q = JobQueue(tmp_path)
    job = q.submit("original", items=["original"])
    q.claim()
    pending = q.control(job["id"], "pause")
    assert pending["pending_control"] == "pause"
    assert "等待当前操作结束" in format_result(pending)
    q.finish(job["id"], "paused", "Paused by user")
    paused = q.get(job["id"])
    assert "pending_control" not in paused
    assert "继续 " + job["id"] in format_result(paused)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("command", ["清空队列", "清除任务队列"])
def test_chat_clear_queue_cancels_without_creating_tasks(
    tmp_path, stream, command, immediate_timeout, monkeypatch
):
    monkeypatch.setattr(openai_compat, "COMPLETION_TIMEOUT", 0)
    q = JobQueue(tmp_path)
    jobs = [q.submit(str(i), items=[str(i)]) for i in range(2)]
    with client(tmp_path) as c:
        response = chat(c, command, stream=stream)
    assert response.status_code == 200
    text = (
        stream_text(stream_chunks(response))
        if stream
        else response.json()["choices"][0]["message"]["content"]
    )
    assert "已取消 2" in text
    assert len(q.list()) == 2
    assert all(q.get(job["id"])["status"] == "cancelled" for job in jobs)


@pytest.mark.parametrize("goal", ["不要清空队列", "解释一下“清空队列”", "清空队列后做什么"])
def test_embedded_clear_command_does_not_cancel_existing_tasks(tmp_path, goal):
    q = JobQueue(tmp_path)
    job = q.submit("original", items=["original"])
    openai_compat.prepare_completion(q, goal)
    assert q.get(job["id"])["status"] == "queued"
