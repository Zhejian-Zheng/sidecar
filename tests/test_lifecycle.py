"""Domain policies run without databases, devices, or a graph runtime."""

import pytest

from sidecar.lifecycle import (
    boundary_change,
    can_resume_queue,
    control_signal,
    execution_save,
    queue_control_status,
    queue_finish,
    valid_resume_note,
)


@pytest.mark.parametrize("requested", ["", "pause", "cancel"])
def test_cancel_cannot_be_downgraded(requested):
    assert control_signal("cancel", requested) == "cancel"


@pytest.mark.parametrize("current,requested", [("", "pause"), ("pause", "cancel"), ("pause", "")])
def test_other_controls_can_be_replaced(current, requested):
    assert control_signal(current, requested) == requested


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_queue_rejects_control_and_late_finish(status):
    with pytest.raises(ValueError, match="任务已经结束"):
        queue_control_status(status, "cancel")
    assert queue_finish(status, "cancel", "failed", "late") is None
    assert not can_resume_queue(status, "")


@pytest.mark.parametrize("signal,expected", [("cancel", "cancelled"), ("pause", "paused")])
@pytest.mark.parametrize("status", ["queued", "paused", "waiting_user", "interrupted"])
def test_unowned_queue_control_is_immediate(status, signal, expected):
    assert queue_control_status(status, signal) == expected
    assert queue_control_status("executing", signal) == "executing"


@pytest.mark.parametrize("status", ["paused", "waiting_user", "interrupted"])
def test_resume_requires_reviewable_state_without_cancel(status):
    assert can_resume_queue(status, "")
    assert can_resume_queue(status, "pause")
    assert not can_resume_queue(status, "cancel")


@pytest.mark.parametrize("status", ["queued", "executing", "running"])
def test_active_or_queued_task_cannot_be_requeued(status):
    assert not can_resume_queue(status, "")


@pytest.mark.parametrize(
    "note,expected",
    [("", False), (" \n", False), ("已核对", True), ("x" * 1000, True), ("x" * 1001, False)],
)
def test_review_note_bounds(note, expected):
    assert valid_resume_note(note) is expected


@pytest.mark.parametrize("proposed", ["completed", "failed", "waiting_user"])
def test_accepted_cancel_wins_before_queue_finish(proposed):
    result = queue_finish("executing", "cancel", proposed, "late")
    assert result.status == "cancelled"
    assert result.reason == "已取消；已执行的操作不会自动撤销"


def test_finish_preserves_execution_result_when_not_cancelled():
    result = queue_finish("executing", "pause", "completed", "verified")
    assert (result.status, result.reason) == ("completed", "verified")


@pytest.mark.parametrize(
    "saved,signal", [("cancelled", ""), ("completed", "cancel"), ("running", "cancel")]
)
def test_execution_index_keeps_cancel_even_after_engine_completion(saved, signal):
    result = execution_save(saved, signal, "completed", "verified")
    assert (result.status, result.reason) == ("cancelled", "Cancelled by user")


def test_execution_save_retains_reason_without_cancel():
    result = execution_save("running", "pause", "waiting_user", "check device")
    assert (result.status, result.reason) == ("waiting_user", "check device")


@pytest.mark.parametrize(
    "signal,expired,status,reason",
    [
        ("cancel", True, "cancelled", "Cancelled by user"),
        ("pause", True, "paused", "Paused by user"),
        ("", True, "failed", "Run time budget exhausted"),
        ("cancel", False, "cancelled", "Cancelled by user"),
        ("pause", False, "paused", "Paused by user"),
    ],
)
def test_boundary_controls_take_precedence_over_budget(signal, expired, status, reason):
    result = boundary_change(signal, expired)
    assert (result.status, result.reason) == (status, reason)


def test_boundary_allows_execution_without_control_or_timeout():
    assert boundary_change("", False) is None
