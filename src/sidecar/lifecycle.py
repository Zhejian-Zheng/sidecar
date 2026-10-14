"""Task lifecycle decisions, independent of persistence and execution frameworks.

Queue ownership and engine progress are distinct. In particular, a terminal
queue result is final, while an engine result can still lose to a cancellation
accepted before the queue finishes. Call these policies inside storage transactions.
"""

from dataclasses import dataclass

TERMINAL = frozenset({"completed", "failed", "cancelled"})
RESUMABLE = frozenset({"paused", "waiting_user", "interrupted"})


@dataclass(frozen=True)
class StatusChange:
    status: str
    reason: str


def control_signal(current: str, requested: str) -> str:
    """Cancellation cannot be cleared or downgraded by a later request."""
    return "cancel" if current == "cancel" else requested


def queue_control_status(status: str, signal: str) -> str:
    if status in TERMINAL:
        raise ValueError("任务已经结束")
    if status == "executing":
        return status
    return "cancelled" if signal == "cancel" else "paused"


def queue_finish(current: str, signal: str, proposed: str, reason: str) -> StatusChange | None:
    if current in TERMINAL:
        return None
    if signal == "cancel":
        return StatusChange("cancelled", "已取消；已执行的操作不会自动撤销")
    return StatusChange(proposed, reason)


def execution_save(saved_status: str, signal: str, proposed: str, reason: str) -> StatusChange:
    if saved_status == "cancelled" or signal == "cancel":
        return StatusChange("cancelled", "Cancelled by user")
    return StatusChange(proposed, reason)


def can_resume_queue(status: str, signal: str) -> bool:
    return status in RESUMABLE and signal != "cancel"


def valid_resume_note(note: str) -> bool:
    return bool(note.strip()) and len(note) <= 1000


def boundary_change(signal: str, expired: bool) -> StatusChange | None:
    if signal == "cancel":
        return StatusChange("cancelled", "Cancelled by user")
    if signal == "pause":
        return StatusChange("paused", "Paused by user")
    if expired:
        return StatusChange("failed", "Run time budget exhausted")
    return None
