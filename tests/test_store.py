import pytest

from sidecar.store import Store


def test_event_pages_are_ordered_isolated_and_skip_unrequested_payloads(tmp_path):
    store = Store(tmp_path)
    try:
        store.create({"task_id": "a", "status": "completed"})
        store.create({"task_id": "b", "status": "completed"})
        for i in range(7):
            store.event("a", "step", {"index": i})
            store.event("b", "step", {"index": 99})
        latest = store.events("a", limit=3)
        assert [event["data"]["index"] for event in latest] == [4, 5, 6]
        # Later appends must not shift the older page's cursor or duplicate entries.
        store.event("a", "step", {"index": 7})
        older = store.events("a", limit=3, before=latest[0]["sequence"])
        assert [event["data"]["index"] for event in older] == [1, 2, 3]
        assert [e["data"]["index"] for e in store.events("a", limit=3,
                before=older[0]["sequence"])] == [0]
        with store.connection:
            store.connection.execute("UPDATE events SET payload='invalid' WHERE seq=1")
        # Proves the limit applies before decoding, not after loading all history.
        assert len(store.events("a", limit=3)) == 3
    finally:
        store.close()


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": -1}, {"before": 0}])
def test_event_page_rejects_invalid_bounds(tmp_path, options):
    store = Store(tmp_path)
    try:
        store.create({"task_id": "a", "status": "completed"})
        with pytest.raises(ValueError):
            store.events("a", **options)
    finally:
        store.close()
