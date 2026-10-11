"""Exercise real LangChain/OpenAI serialization against an offline HTTP endpoint."""

import json

import httpx
import pytest

from sidecar.contracts import Observation, Task
from sidecar.model import LangChainModel


@pytest.fixture
def endpoint(monkeypatch):
    import langchain_openai

    requests = []
    replies = []

    def respond(request):
        requests.append((request.url, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "id": "offline-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(replies.pop(0)),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    original = langchain_openai.ChatOpenAI
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            langchain_openai,
            "ChatOpenAI",
            lambda **kw: original(
                **kw,
                http_client=client,
            ),
        )
        monkeypatch.setenv("SIDECAR_MODEL", "deepseek-flash")
        monkeypatch.setenv("SIDECAR_API_KEY", "offline-test-key")
        monkeypatch.setenv("SIDECAR_BASE_URL", "https://api.deepseek.com/v1")
        yield requests, replies


def test_deepseek_uses_json_mode_for_plan_decision_and_verification(endpoint):
    requests, replies = endpoint
    replies.extend(
        [
            {"goal": "changed", "criteria": ["待办存在：准备演示"]},
            {"kind": "tap", "target": "add", "reason": "添加事项"},
            {
                "checks": [
                    {
                        "criterion": "待办存在：准备演示",
                        "passed": True,
                        "evidence": "列表显示准备演示",
                    }
                ]
            },
        ]
    )
    model = LangChainModel.from_env()
    task = model.plan("添加准备演示")
    obs = Observation(text="列表", elements={"add": "添加"}, image="data:image/png;base64,aGVsbG8=")
    assert task.goal == "添加准备演示"
    assert model.decide(task, obs, []).target == "add"
    assert model.verify(task, obs).satisfies(task)
    for url, body in requests:
        assert url.path == "/v1/chat/completions"
        assert body["response_format"] == {"type": "json_object"}
        assert body["thinking"] == {"type": "disabled"}
        instructions = " ".join(m["content"] for m in body["messages"] if m["role"] == "system")
        assert "JSON" in instructions and "properties" in instructions
    assert requests[1][1]["messages"][-1]["content"][-1]["image_url"]["url"] == obs.image


def test_deepseek_text_model_omits_image_and_rejects_visual_coordinates(endpoint, monkeypatch):
    requests, replies = endpoint
    monkeypatch.setenv("SIDECAR_MODEL", "deepseek-v4-pro")
    replies.append({"kind": "tap", "x": 0.5, "y": 0.5, "reason": "猜测坐标"})
    model = LangChainModel.from_env()
    action = model.decide(
        Task(goal="添加", criteria=["已添加"]),
        Observation(
            text="列表", elements={"add": "添加按钮"}, image="data:image/png;base64,aGVsbG8="
        ),
        [],
    )
    assert "image_url" not in json.dumps(requests[0][1])
    assert action.kind == "handoff"


def test_other_compatible_endpoints_keep_schema_mode(endpoint, monkeypatch):
    requests, replies = endpoint
    monkeypatch.setenv("SIDECAR_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("SIDECAR_MODEL", "vision-model")
    replies.append({"kind": "wait", "reason": "加载中"})
    action = LangChainModel.from_env().decide(
        Task(goal="添加", criteria=["已添加"]), Observation(text="加载"), []
    )
    assert action.kind == "wait"
    assert requests[0][1]["response_format"]["type"] == "json_schema"
    assert "thinking" not in requests[0][1]


def test_deepseek_invalid_action_is_rejected_after_json_parsing(endpoint):
    _, replies = endpoint
    replies.append({"kind": "tap", "x": 8, "y": 0.5, "reason": "错误坐标"})
    with pytest.raises(ValueError):
        LangChainModel.from_env().decide(
            Task(goal="添加", criteria=["已添加"]), Observation(text="列表"), []
        )


def test_planning_disables_inherited_tracing():
    from langsmith import get_tracing_context

    class Response:
        def invoke(self, messages):
            assert get_tracing_context()["enabled"] is False
            return {"goal": "添加", "criteria": ["已添加"]}

    assert LangChainModel(None, None, planning_chain=Response()).plan("添加").goal == "添加"
