# Agent 主循环：观察 → 决策 → 校验并执行 → 再观察；完成前验证，必要时交接。
"""A single GUI decision loop with explicit execution and recovery boundaries."""

import sqlite3
import time
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langsmith import tracing_context

from sidecar.contracts import Action, DecisionModel, Device, Observation, Task, Verification
from sidecar.store import Store

TERMINAL = {"completed", "failed", "cancelled"}


# LangGraph 节点共享的任务状态，包含当前动作、历史和运行预算。
class State(TypedDict):
    task_id: str
    task: dict
    device_id: str
    status: str
    reason: str
    decisions: int
    history: list[str]
    observation: dict | None
    action: dict | None
    verification: dict | None
    fingerprint: str
    stalls: int
    limits: dict


# 管理单个 Agent 的执行边界、持久化、人工接管与结果验证。
class Harness:
    # 连接设备与模型，建立带 SQLite 检查点的状态图，并配置运行预算。
    def __init__(
        self,
        home: Path,
        device: Device,
        model: DecisionModel,
        max_decisions: int = 30,
        max_stalls: int = 4,
        max_seconds: float = 180,
    ):
        if max_decisions < 1 or max_stalls < 1 or max_seconds <= 0:
            raise ValueError("budgets must be positive")
        self.device, self.model = device, model
        self.store = Store(Path(home))
        self.limits = {"decisions": max_decisions, "stalls": max_stalls, "seconds": max_seconds}
        self.connection = sqlite3.connect(
            Path(home) / "checkpoints.sqlite", check_same_thread=False
        )
        graph = StateGraph(State)
        # 五个节点共享同一份状态；条件路由将终止、交接和再次观察集中处理。
        for name in ("observe", "decide", "execute", "verify", "handoff"):
            graph.add_node(name, self._persisted(getattr(self, f"_{name}")))
        graph.add_edge(START, "observe")
        graph.add_conditional_edges("observe", self._after_observe)
        graph.add_conditional_edges("decide", self._after_decide)
        graph.add_conditional_edges("execute", self._after_action)
        graph.add_conditional_edges("verify", self._after_action)
        graph.add_conditional_edges("handoff", self._after_action)
        self.graph = graph.compile(checkpointer=SqliteSaver(self.connection))
        self.deadline = 0.0

    # 支持 with 写法，便于结束执行时统一释放数据库资源。
    def __enter__(self):
        return self

    # 关闭图检查点和任务索引的数据库连接。
    def __exit__(self, *_):
        self.connection.close()
        self.store.close()

    # 用任务编号隔离图状态；图递归限制之外还另有业务步数预算。
    def _config(self, task_id):
        return {"configurable": {"thread_id": task_id}, "recursion_limit": 1000}

    # 包装图节点，使每次状态变化同时写入可供页面查询的任务索引。
    def _persisted(self, operation):
        # 先运行节点，再合并保存结果；持久层确认的取消状态优先。
        def node(state):
            update = operation(state)
            saved = self.store.save({**state, **update})
            return {**update, "status": saved["status"], "reason": saved["reason"]}

        return node

    # 持有执行锁后登记新任务；准备回调只在任务获准启动后执行。
    def start(
        self, task: Task, task_id: str | None = None, prepare: Callable[[], None] | None = None
    ) -> dict:
        with self.store.execution_lock(), self._device_lock():
            state = State(
                task_id=task_id or uuid4().hex,
                task=task.model_dump(),
                device_id=self.device.device_id,
                status="running",
                reason="",
                decisions=0,
                history=[],
                observation=None,
                action=None,
                verification=None,
                fingerprint="",
                stalls=0,
                limits=self.limits,
            )
            self.store.create(state)
            self.store.event(state["task_id"], "started", {"device_id": self.device.device_id})
            if prepare:
                try:
                    prepare()
                except Exception as exc:
                    state.update(status="failed", reason=f"Setup error: {type(exc).__name__}")
                    self.store.save(state)
                    return state
            return self._run(state, state)

    # 核对设备和任务状态，记录用户交接说明，再从新观察恢复。
    def resume(self, task_id: str, note: str) -> dict:
        if not note.strip() or len(note) > 1000:
            raise ValueError("resume needs a note of 1–1000 characters after reviewing the device")
        with self.store.execution_lock(), self._device_lock():
            saved = self.store.get(task_id)
            if saved["device_id"] != self.device.device_id:
                raise ValueError("device mismatch")
            if saved["status"] in TERMINAL:
                raise ValueError("cannot resume a terminal task")
            snapshot = self.graph.get_state(self._config(task_id))
            state = dict(snapshot.values) or saved
            if state["status"] in TERMINAL:
                self.store.save(state)
                raise ValueError("cannot resume a terminal task")
            if self.store.signal(task_id) == "cancel":
                state.update(status="cancelled", reason="Cancelled by user")
                self.store.save(state)
                return state
            self.store.control(task_id, "")
            self.store.event(
                task_id,
                "user_resumed",
                {"note": note, "uncertain_action_reviewed": self.store.unresolved(task_id)},
            )
            self.store.acknowledge(task_id)
            if any(t.interrupts for t in snapshot.tasks):
                return self._run(Command(resume={"note": note}), state)
            # Recovery after process death never continues an old pending GUI action.
            update = {
                "status": "running",
                "reason": "Recovered; re-observe the device",
                "action": None,
                "history": (state["history"] + [note])[-8:],
            }
            if not snapshot.values:
                # Setup can die before LangGraph has written its first checkpoint.
                # Use the complete task index, without replaying setup or old actions.
                state = {**state, **update}
                return self._run(state, state)
            self.graph.update_state(self._config(task_id), update, as_node="handoff")
            return self._run(None, state)

    # 使用设备提供的执行锁；没有设备锁的模拟环境仍受工作目录锁约束。
    def _device_lock(self):
        lock = getattr(self.device, "execution_lock", None)
        return lock() if lock else nullcontext()

    # 运行状态图，将异常转成任务状态并保存最终结果，避免记录异常正文。
    def _run(self, value, state):
        self.deadline = time.monotonic() + state["limits"]["seconds"]
        try:
            # Do not inherit a global tracing opt-in for unrelated private GUI data.
            with tracing_context(enabled=False):
                result = self.graph.invoke(value, self._config(state["task_id"]))
            result = {k: v for k, v in result.items() if not k.startswith("__")}
        except Exception as exc:
            result = dict(self.graph.get_state(self._config(state["task_id"])).values) or state
            result.update(status="failed", reason=f"Runtime error: {type(exc).__name__}")
            self.store.event(state["task_id"], "error", {"type": type(exc).__name__})
        result = self.store.save(result)
        self.store.event(
            result["task_id"], "status", {"status": result["status"], "reason": result["reason"]}
        )
        return result

    # 读取已持久化的任务状态，供 CLI 或其他入口查询。
    def status(self, task_id):
        return self.store.get(task_id)

    # 按发生顺序读取该任务的执行记录。
    def events(self, task_id):
        return self.store.events(task_id)

    # 提交暂停或取消请求；无人执行的遗留任务可以直接确认取消。
    def control(self, task_id, signal):
        saved = self.store.get(task_id)
        if saved["status"] in TERMINAL:
            raise ValueError("task is terminal")
        self.store.control(task_id, signal)
        if signal == "cancel" and saved["status"] in {"paused", "waiting_user"}:
            saved.update(status="cancelled", reason="Cancelled by user")
            self.store.save(saved)
        elif signal == "cancel":
            try:
                with self.store.execution_lock():
                    # No executor owns the workspace: acknowledge an orphaned task.
                    self.store.save(self.store.get(task_id))
            except RuntimeError:
                pass  # A live executor acknowledges at its next boundary.
        self.store.event(task_id, "control", {"signal": signal})

    # 只对模型连接故障有限重试，每次尝试前检查取消、暂停和超时。
    def _model_call(self, state, call):
        for attempt in range(2):
            if self._boundary(state):
                return None
            try:
                return call()
            except (TimeoutError, ConnectionError):
                if self._boundary(state):
                    return None
                if attempt:
                    raise
                self.store.event(state["task_id"], "model_retry", {"attempt": 2})
                time.sleep(0.25)

    # 在步骤边界检查用户控制和时间预算，返回需要终止或暂停的状态。
    def _boundary(self, state):
        signal = self.store.signal(state["task_id"])
        if signal == "cancel":
            return {"status": "cancelled", "reason": "Cancelled by user"}
        if signal == "pause":
            return {"status": "paused", "reason": "Paused by user"}
        if time.monotonic() >= self.deadline:
            return {"status": "failed", "reason": "Run time budget exhausted"}
        return {}

    # 读取新界面并记录简要观察事件，截图仍保存在观察数据中。
    def _capture(self, state):
        observation = self.device.observe()
        self.store.event(
            state["task_id"],
            "observation",
            {
                "id": observation.id,
                "text": observation.text[:2000],
                "has_image": observation.image is not None,
            },
        )
        return observation

    # 刷新界面、清除旧动作，并检查敏感页面、步数和连续无进展次数。
    def _observe(self, state):
        if stop := self._boundary(state):
            return stop
        if state["decisions"] >= state["limits"]["decisions"]:
            return {"status": "failed", "reason": "Decision budget exhausted"}
        observation = self._capture(state)
        fingerprint = observation.fingerprint()
        stalls = state["stalls"] + 1 if state["fingerprint"] == fingerprint else 0
        update = {
            "observation": observation.model_dump(),
            "fingerprint": fingerprint,
            "stalls": stalls,
            "status": "running",
            "action": None,
        }
        if observation.requires_human:
            update.update(status="waiting_user", reason="Device requires user intervention")
        elif stalls >= state["limits"]["stalls"]:
            update.update(status="failed", reason="No visible progress")
        return update

    # 请求一个结构化动作；模型等待期间收到的控制请求仍要优先处理。
    def _decide(self, state):
        if stop := self._boundary(state):
            return stop
        action = self._model_call(
            state,
            lambda: self.model.decide(
                Task.model_validate(state["task"]),
                Observation.model_validate(state["observation"]),
                state["history"][-8:],
            ),
        )
        if action is None:
            return self._boundary(state)
        action = Action.model_validate(action)
        self.store.event(state["task_id"], "decision", action.model_dump())
        update = {"action": action.model_dump(), "decisions": state["decisions"] + 1}
        if action.kind == "handoff":
            update.update(status="waiting_user", reason=action.reason)
        update.update(self._boundary(state))
        return update

    # 重新观察并校验旧动作，先登记意图再执行；结果不确定时转人工核对。
    def _execute(self, state):
        if stop := self._boundary(state):
            return stop
        action = Action.model_validate(state["action"])
        observation = self._capture(state)
        if observation.requires_human:
            return {"status": "waiting_user", "reason": "Device requires user intervention"}
        previous = Observation.model_validate(state["observation"])
        validate = getattr(self.device, "is_current", None)
        current = (
            validate(action, previous, observation)
            if validate
            else observation.fingerprint() == state["fingerprint"]
        )
        if not current:
            return self._history(state, "Screen changed before action; discarded stale action")
        if action.target and action.target not in observation.elements:
            return {"status": "failed", "reason": "Action references an unknown target"}
        permission = self.device.assess(action, observation)
        if permission != "allow":
            return {
                "status": "waiting_user" if permission == "handoff" else "failed",
                "reason": f"Device policy: {permission}; {action.reason}",
            }
        if stop := self._boundary(state):
            return stop
        action_id = f"{state['task_id']}:{state['decisions']}"
        # 先记意图再操作，崩溃后才能识别可能已生效的动作，避免直接重放。
        if not self.store.reserve(state["task_id"], action_id, action.model_dump()):
            return {"status": "waiting_user", "reason": "Action already recorded; check device"}
        try:
            self.device.execute(action)
        except Exception as exc:
            self.store.outcome(action_id, "uncertain")
            self.store.event(
                state["task_id"],
                "uncertain_action",
                {"action_id": action_id, "error_type": type(exc).__name__},
            )
            return {
                "status": "waiting_user",
                "reason": "Action outcome unknown; check device",
                **self._history(state, "Last action outcome unknown; do not blindly repeat"),
            }
        self.store.outcome(action_id, "returned")
        self.store.event(state["task_id"], "action_returned", {"action_id": action_id})
        return self._history(state, f"{action.kind}: {action.reason}; check next observation")

    # 用新的观察逐项验证；证据不足时继续循环，而不是直接报告完成。
    def _verify(self, state):
        if stop := self._boundary(state):
            return stop
        observation = self._capture(state)
        if observation.requires_human:
            return {"status": "waiting_user", "reason": "Verification needs user intervention"}
        task = Task.model_validate(state["task"])
        value = self._model_call(state, lambda: self.model.verify(task, observation))
        if value is None:
            return self._boundary(state)
        verification = Verification.model_validate(value)
        if stop := self._boundary(state):
            return stop
        evidence = {**verification.model_dump(), "observation_id": observation.id}
        self.store.event(state["task_id"], "verification", evidence)
        if verification.satisfies(task):
            return {
                "status": "completed",
                "reason": "All criteria verified",
                "verification": evidence,
                "observation": observation.model_dump(),
            }
        return {
            "verification": evidence,
            **self._history(state, "Verification failed: " + verification.model_dump_json()),
        }

    # 通过图中断保存交接点；用户交还控制后清除旧动作并重新观察。
    def _handoff(self, state):
        stop = self._boundary(state)
        if stop.get("status") in TERMINAL:
            return stop
        saved = self.store.save(state)
        if saved["status"] == "cancelled":
            return {"status": saved["status"], "reason": saved["reason"]}
        response = interrupt(
            {
                "task_id": state["task_id"],
                "status": state["status"],
                "reason": state["reason"],
                "action": state["action"],
            }
        )
        return {
            "status": "running",
            "reason": "User returned control; re-observe",
            "action": None,
            "stalls": 0,
            **self._history(state, "User: " + response["note"]),
        }

    # 只保留最近八条短记录，限制送给模型的历史上下文。
    @staticmethod
    def _history(state, entry):
        return {"history": (state["history"] + [entry[:600]])[-8:]}

    # 统一判断结束或交接分支，供各节点的路由复用。
    @staticmethod
    def _stop_route(state):
        if state["status"] in TERMINAL:
            return END
        if state["status"] in {"waiting_user", "paused"}:
            return "handoff"
        return None

    # 观察正常后进入决策，否则结束或交接。
    def _after_observe(self, state):
        return self._stop_route(state) or "decide"

    # 完成请求进入验证，普通动作进入执行；控制状态优先。
    def _after_decide(self, state):
        return self._stop_route(state) or (
            "verify" if state["action"]["kind"] == "finish" else "execute"
        )

    # 执行、验证或交接结束后，决定退出还是再次观察。
    def _after_action(self, state):
        return self._stop_route(state) or "observe"
