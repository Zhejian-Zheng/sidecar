import pytest

from sidecar.contracts import Action, Observation, Task
from sidecar.model import LangChainModel


class Response:
    def __init__(self, value):
        self.value = value
        self.messages = None

    def invoke(self, messages):
        self.messages = messages
        return self.value


def test_model_receives_latest_image_and_bounded_history():
    response = Response({"kind": "wait", "reason": "Loading"})
    model = LangChainModel(response, Response({"checks": []}))
    image = "data:image/png;base64,aGVsbG8="
    result = model.decide(
        Task(goal="Create item", criteria=["Item exists"]),
        Observation(text="Visible screen", image=image),
        [f"{i}:" + "x" * 1000 for i in range(30)],
    )
    assert result.kind == "wait"
    content = response.messages[-1].content
    assert content[-1]["image_url"]["url"] == image
    assert "Visible screen" in content[0]["text"]
    assert "29:" in content[0]["text"]
    assert "0:" + "x" * 100 not in content[0]["text"]
    assert len(content[0]["text"]) < 6000


def test_malformed_model_action_is_rejected():
    model = LangChainModel(Response({"kind": "type"}), Response({}))
    with pytest.raises(ValueError):
        model.decide(Task(goal="Write", criteria=["Written"]), Observation(text=""), [])


def test_model_accepts_structured_pydantic_response():
    model = LangChainModel(Response(Action(kind="finish", reason="Check result")), Response({}))
    assert (
        model.decide(Task(goal="Check", criteria=["Exists"]), Observation(text=""), []).kind
        == "finish"
    )


def test_planning_preserves_user_goal_and_validates_criteria():
    response = Response(Task(goal="被模型改写", criteria=["列表中存在准备演示"]))
    model = LangChainModel(Response({}), Response({}), planning_chain=response)
    task = model.plan("请添加准备演示")
    assert task.goal == "请添加准备演示"
    assert task.criteria == ["列表中存在准备演示"]
