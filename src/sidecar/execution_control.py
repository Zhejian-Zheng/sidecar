"""Application service for task control; no graph, device or model is needed."""

from typing import ContextManager, Protocol

from sidecar.lifecycle import TERMINAL


class ControlStore(Protocol):
    def get(self, task_id: str) -> dict: ...
    def control(self, task_id: str, value: str) -> None: ...
    def save(self, state: dict) -> dict: ...
    def execution_lock(self) -> ContextManager[None]: ...
    def event(self, task_id: str, kind: str, payload: dict) -> None: ...


class ExecutionControl:
    """Caller owns the store connection; the executor owns action boundaries."""

    def __init__(self, store: ControlStore):
        self.store = store

    def request(self, task_id: str, signal: str) -> None:
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
