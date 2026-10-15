# 定义模块间共享的数据格式：任务、观察、动作、验证结果和设备接口。
"""Validated messages shared by the model, harness, and device adapters."""

import hashlib
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# 统一拒绝未知字段，并去掉字符串两端的空白。
class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# 完成门禁按 App 拆分：每个子模型只携带自己的期望字段；kind 用于判别，
# 新增 App 时只需新增一个子模型并加入下方联合类型，不必改动已有子模型。
class ClockAlarmRequirement(Message):
    kind: Literal["clock_alarm_enabled"] = "clock_alarm_enabled"
    intent: Literal["enable", "disable", "unknown"] = "enable"
    expected_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class GmailDraftRequirement(Message):
    kind: Literal["gmail_draft_saved"] = "gmail_draft_saved"
    recipient: str | None = Field(default=None, max_length=200)
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=4000)


VerificationRequirement = Annotated[
    ClockAlarmRequirement | GmailDraftRequirement,
    Field(discriminator="kind"),
]


class UINode(Message):
    node_id: str
    parent_id: str | None = None
    resource_id: str = ""
    klass: str = ""
    package: str = ""
    text: str = ""
    description: str = ""
    visible: bool | None = None
    window_id: str | None = None
    checkable: bool | None = None
    checked: bool | None = None


# 保存用户目标和逐项完成标准，作为规划、执行和验证的共同依据。
class Task(Message):
    goal: str = Field(min_length=1, max_length=4000)
    criteria: list[str] = Field(min_length=1, max_length=20)
    requirement: VerificationRequirement | None = None

    # 检查完成标准非空、不重复且长度合理，避免无法核对的任务。
    @field_validator("criteria")
    @classmethod
    def valid_criteria(cls, value):
        value = [c.strip() for c in value]
        if any(not c or len(c) > 500 for c in value) or len(set(value)) != len(value):
            raise ValueError("criteria must be nonempty, unique and at most 500 characters")
        return value


# 保存一次界面观察；元素用于定位，截图用于补充视觉信息。
class Observation(Message):
    id: str = Field(default_factory=lambda: uuid4().hex)
    text: str = Field(max_length=16000)
    elements: dict[str, str] = Field(default_factory=dict, max_length=200)
    image: str | None = Field(default=None, max_length=6_000_000)
    ui: list[UINode] = Field(default_factory=list, max_length=1000)
    ui_truncated: bool = False
    package: str | None = None
    app_version: str | None = None
    viewport: tuple[int, int] | None = None
    display_session: str | None = None
    requires_human: bool = False
    # Identified controls that the executor must leave to the user.
    confirmation_targets: dict[str, str] = Field(default_factory=dict, max_length=200)

    # 只接收内联图片，避免把任意远程图片链接传入后续流程。
    @field_validator("image")
    @classmethod
    def local_image_only(cls, value):
        if value is not None and not value.startswith(
            ("data:image/png;base64,", "data:image/jpeg;base64,")
        ):
            raise ValueError("image must be an inline PNG or JPEG, never a remote URL")
        return value

    # 计算界面内容指纹，忽略每次采集都会变化的观察编号。
    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json(exclude={"id"}).encode()).hexdigest()


# 定义允许的单步动作及参数；坐标采用 0 到 1 的相对位置。
class Action(Message):
    kind: Literal["tap", "type", "swipe", "back", "wait", "handoff", "finish"]
    reason: str = Field(min_length=1, max_length=500)
    target: str | None = None
    text: str | None = Field(default=None, max_length=2000)
    direction: Literal["up", "down", "left", "right"] | None = None
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)

    # 按动作类型检查必需参数，拒绝坐标与控件混用等无效组合。
    @model_validator(mode="after")
    def valid_arguments(self):
        if self.kind == "tap":
            if bool(self.target) == (self.x is not None or self.y is not None):
                raise ValueError("tap needs a target OR normalized x/y")
            if not self.target and (self.x is None or self.y is None):
                raise ValueError("tap needs both x and y")
        elif self.kind == "type":
            if not self.text:
                raise ValueError("type needs nonempty text")
        elif self.kind == "swipe" and self.direction is None:
            raise ValueError("swipe needs a direction")
        allowed = {
            "tap": {"target", "x", "y"},
            "type": {"target", "text"},
            "swipe": {"direction"},
        }.get(self.kind, set())
        for key in {"target", "text", "direction", "x", "y"} - allowed:
            if getattr(self, key) is not None:
                raise ValueError(f"{key} is invalid for {self.kind}")
        return self


# 记录一项完成标准的判断和可见证据。
class Check(Message):
    criterion: str
    passed: bool
    evidence: str = Field(max_length=1000)
    requires_confirmation: bool = False


# 汇总逐项验证结果；模型声称完成后仍须经过这里检查。
class Verification(Message):
    checks: list[Check] = Field(max_length=20)

    # 只有全部标准恰好被覆盖、全部通过且有证据，才判定验证通过。
    def satisfies(self, task: Task) -> bool:
        return (
            len(self.checks) == len(task.criteria)
            and {c.criterion for c in self.checks} == set(task.criteria)
            and all(
                c.passed and not c.requires_confirmation and c.evidence.strip() for c in self.checks
            )
        )

    def ready_for_confirmation(self, task: Task) -> bool:
        return (
            len(self.checks) == len(task.criteria)
            and {c.criterion for c in self.checks} == set(task.criteria)
            and any(c.passed for c in self.checks)
            and any(c.requires_confirmation for c in self.checks)
            and all(c.passed != c.requires_confirmation and c.evidence.strip() for c in self.checks)
        )


# 准备阶段的可读失败；只有这类消息才允许跨越进程边界并展示给用户。
class DeviceSetupError(RuntimeError):
    """Setup-stage device failure whose message is safe to show the user."""


# 模型服务对请求的可读拒绝（含状态码），同样允许展示给用户。
class ModelAPIError(RuntimeError):
    """Model provider rejection whose message is safe to show the user."""


# 设备适配器的接口约定，让核心流程不依赖具体手机工具。
class Device(Protocol):
    device_id: str

    # 读取当前界面，返回文字、控件及可选截图。
    def observe(self) -> Observation: ...
    # 由设备侧策略决定动作可执行、应拒绝，还是需要人工交接。
    def assess(
        self, action: Action, observation: Observation
    ) -> Literal["allow", "handoff", "deny"]:
        """Trusted adapter policy; sensitive actions must not default to allow."""
        ...

    # 执行一个有超时边界的动作；异常不代表操作一定没有生效。
    def execute(self, action: Action) -> None:
        """Bounded call. Any exception means the effect may already have happened."""
        ...


# 模型接口约定：下一步决策与结果验证分别调用。
class DecisionModel(Protocol):
    # 根据任务、最新界面和近期历史，选择一个动作。
    def decide(self, task: Task, observation: Observation, history: list[str]) -> Action: ...
    # 根据新的界面证据逐项核对任务完成标准。
    def verify(self, task: Task, observation: Observation) -> Verification: ...
