"""Control requests use real storage, without constructing an execution graph."""

import pytest

from sidecar.execution_control import ExecutionControl
from sidecar.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path)
    yield value
    value.close()


def test_pause_only_records_request(store):
    store.create({"task_id": "one", "status": "running", "reason": ""})
    ExecutionControl(store).request("one", "pause")
    assert store.signal("one") == "pause"
    assert store.get("one")["status"] == "running"
    assert store.events("one")[-1]["data"] == {"signal": "pause"}


@pytest.mark.parametrize("status", ["paused", "waiting_user", "running"])
def test_cancel_unowned_task_releases_workspace(store, status):
    store.create({"task_id": "one", "status": status, "reason": ""})
    ExecutionControl(store).request("one", "cancel")
    assert store.get("one")["status"] == "cancelled"
    store.create({"task_id": "two", "status": "running", "reason": ""})
    assert store.get("two")["status"] == "running"


def test_cancel_owned_task_waits_for_executor(store):
    store.create({"task_id": "one", "status": "running", "reason": ""})
    with store.execution_lock():
        ExecutionControl(store).request("one", "cancel")
        assert store.signal("one") == "cancel"
        assert store.get("one")["status"] == "running"
        with pytest.raises(ValueError, match="unfinished task"):
            store.create({"task_id": "two", "status": "running"})
    assert store.save(store.get("one"))["status"] == "cancelled"


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_control_is_rejected_without_event(store, status):
    store.create({"task_id": "one", "status": status, "reason": ""})
    with pytest.raises(ValueError, match="task is terminal"):
        ExecutionControl(store).request("one", "cancel")
    assert store.signal("one") == ""
    assert store.events("one") == []
