# 离线文字界面模拟器：用于验证执行机制，不会操作真实手机或第三方账号。
"""Offline, text-only GUI simulation. This is NOT a third-party App or phone driver."""

import json
from pathlib import Path

from sidecar.contracts import Action, Check, Observation, Task, Verification

PREFIX = "待办存在："


# 用本地 JSON 模拟清单页、编辑页和登录状态，实现统一设备接口。
class DemoDevice:
    # 用数据文件的绝对路径区分演示设备，避免跨目录误恢复。
    def __init__(self, home: Path):
        self.path = Path(home) / "demo-device.json"
        self.device_id = "offline-demo:" + str(self.path.resolve())

    # 读取模拟状态；首次运行从已登录的空清单开始。
    def load(self):
        if not self.path.exists():
            return {"logged_in": True, "screen": "list", "draft": "", "items": []}
        return json.loads(self.path.read_text())

    # 先写临时文件再替换，避免正常读写过程看到只写了一半的 JSON。
    def save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    # 把模拟页面转换为观察；未登录时要求人工交接。
    def observe(self):
        state = self.load()
        if not state["logged_in"]:
            return Observation(
                text="OFFLINE DEMO: Login required. User must log in.", requires_human=True
            )
        elements = (
            {"add": "新建待办"}
            if state["screen"] == "list"
            else {"title": "待办标题输入框", "save": "保存待办"}
        )
        return Observation(
            text="OFFLINE DEMO\n" + json.dumps(state, ensure_ascii=False), elements=elements
        )

    # 只允许演示环境定义的控件及动作，不接受任意点击。
    def assess(self, action, observation):
        if observation.requires_human:
            return "handoff"
        if action.kind in {"wait", "back"}:
            return "allow"
        if action.kind == "tap" and action.target in {"add", "save"}:
            return "allow"
        if action.kind == "type" and action.target == "title":
            return "allow"
        return "deny"

    # 模拟打开、输入、保存和返回；修改只发生在本地状态文件中。
    def execute(self, action):
        state = self.load()
        if not state["logged_in"]:
            raise RuntimeError("User needs to log in")
        if action.kind == "tap" and action.target == "add" and state["screen"] == "list":
            state.update(screen="editor", draft="")
        elif action.kind == "type" and action.target == "title" and state["screen"] == "editor":
            state["draft"] = action.text
        elif action.kind == "tap" and action.target == "save" and state["screen"] == "editor":
            if not state["draft"]:
                raise ValueError("Empty title")
            state["items"].append(state["draft"])
            state.update(screen="list", draft="")
        elif action.kind == "back":
            state.update(screen="list", draft="")
        elif action.kind != "wait":
            raise ValueError("Unsupported demo action")
        self.save(state)


# 用于测试的确定性策略，不具备理解任意自然语言的能力。
class DemoPolicy:
    """Rule-based test policy; does not understand arbitrary natural language."""

    # 从演示观察中还原模拟状态，供规则决策和验证使用。
    @staticmethod
    def _screen(observation):
        return json.loads(observation.text.split("\n", 1)[1])

    # 找出缺少的事项，再按当前页面选择添加、输入、保存或验证。
    def decide(self, task: Task, observation: Observation, history: list[str]) -> Action:
        state = self._screen(observation)
        missing = [
            c.removeprefix(PREFIX)
            for c in task.criteria
            if c.removeprefix(PREFIX) not in state["items"]
        ]
        if not missing:
            return Action(kind="finish", reason="演示列表已包含全部目标事项，进入验证")
        if state["screen"] == "list":
            return Action(kind="tap", target="add", reason="打开新建页面")
        if state["draft"] != missing[0]:
            return Action(kind="type", target="title", text=missing[0], reason="填写缺少的事项")
        return Action(kind="tap", target="save", reason="保存当前事项")

    # 直接检查模拟清单是否包含目标事项，返回逐项证据。
    def verify(self, task: Task, observation: Observation) -> Verification:
        state = self._screen(observation)
        return Verification(
            checks=[
                Check(
                    criterion=c,
                    passed=c.removeprefix(PREFIX) in state["items"],
                    evidence=json.dumps(state["items"], ensure_ascii=False),
                )
                for c in task.criteria
            ]
        )
