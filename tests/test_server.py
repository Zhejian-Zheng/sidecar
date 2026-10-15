from fastapi.testclient import TestClient


def client(home):
    from sidecar.server import create_app

    return TestClient(create_app(home, token="test-pairing-secret"))


def test_api_requires_pairing_token_and_does_not_leak_tasks(tmp_path):
    with client(tmp_path) as c:
        assert c.get("/").status_code == 200
        assert c.get("/api/tasks").status_code == 401
        assert c.get("/api/config", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert (
            c.get(
                "/api/config", headers={"Authorization": "Bearer test-pairing-secret"}
            ).status_code
            == 200
        )


def test_task_submit_status_and_cancel(tmp_path):
    headers = {"Authorization": "Bearer test-pairing-secret"}
    with client(tmp_path) as c:
        r = c.post("/api/tasks", headers=headers, json={"goal": "测试", "items": ["准备演示"]})
        assert r.status_code == 201
        task_id = r.json()["id"]
        assert c.get("/api/tasks", headers=headers).json()[0]["id"] == task_id
        assert c.post(f"/api/tasks/{task_id}/cancel", headers=headers).status_code == 200
        assert c.get(f"/api/tasks/{task_id}", headers=headers).json()["status"] == "cancelled"
        assert c.get(f"/api/tasks/{task_id}/frame").status_code == 401


def test_server_rejects_bad_task_and_unknown_task(tmp_path):
    headers = {"Authorization": "Bearer test-pairing-secret"}
    with client(tmp_path) as c:
        assert (
            c.post("/api/tasks", headers=headers, json={"goal": "", "items": []}).status_code == 422
        )
        assert c.get("/api/tasks/missing", headers=headers).status_code == 404


def test_config_exposes_allow_list_and_task_app_is_validated(tmp_path):
    from sidecar.jobs import JobQueue, Settings

    JobQueue(tmp_path).configure(
        Settings(mode="android", package="com.a", allowed_packages=["com.a", "com.b"], model="m")
    )
    headers = {"Authorization": "Bearer test-pairing-secret"}
    with client(tmp_path) as c:
        data = c.get("/api/config", headers=headers).json()
        assert data["allowed_packages"] == ["com.a", "com.b"]
        assert data["package"] == "com.a"
        # 目标 App 越界时，提交阶段返回可解释的 409，而不是排队后失败。
        rejected = c.post("/api/tasks", headers=headers, json={"goal": "任务", "app": "com.evil"})
        assert rejected.status_code == 409
        accepted = c.post("/api/tasks", headers=headers, json={"goal": "任务", "app": "com.b"})
        assert accepted.status_code == 201
        assert accepted.json()["request"]["app"] == "com.b"


def test_event_history_pagination_requires_auth_and_validates_cursor(tmp_path):
    from sidecar.jobs import JobQueue
    from sidecar.store import Store

    q = JobQueue(tmp_path)
    task_id = q.submit("任务", items=["一"])["id"]
    store = Store(q.engine)
    try:
        store.create({"task_id": task_id, "status": "completed"})
        for i in range(65):
            store.event(task_id, "step", {"index": i})
    finally:
        store.close()
    headers = {"Authorization": "Bearer test-pairing-secret"}
    url = f"/api/tasks/{task_id}/events"
    with client(tmp_path) as c:
        assert c.get(url).status_code == 401
        latest = c.get(url, headers=headers).json()
        assert len(latest) == 60
        assert latest[0]["data"]["index"] == 5
        older = c.get(url, headers=headers, params={"before": latest[0]["sequence"]}).json()
        assert [e["data"]["index"] for e in older] == [0, 1, 2, 3, 4]
        assert c.get(url, headers=headers, params={"limit": 501}).status_code == 422
        assert c.get(url, headers=headers, params={"before": 0}).status_code == 422
        assert c.get('/api/tasks/missing/events', headers=headers).status_code == 404


def test_video_and_report_are_authenticated_and_task_scoped(tmp_path):
    import hashlib
    import json
    import time

    from sidecar.jobs import JobQueue

    q = JobQueue(tmp_path)
    first = q.submit("first", items=["one"])["id"]
    second = q.submit("second", items=["two"])["id"]
    folder = tmp_path / "recordings" / first / "123-attempt"
    folder.mkdir(parents=True)
    image = b"private-frame"
    (folder / "latest.png").write_bytes(image)
    meta = {"status": "live", "received_at": time.monotonic(), "sequence": 1,
            "session_id": "123-attempt", "sha256": hashlib.sha256(image).hexdigest()}
    (folder / "stream.json").write_text(json.dumps(meta))
    (folder / "recording.mkv").write_bytes(b"recorded-video")
    headers = {"Authorization": "Bearer test-pairing-secret"}
    with client(tmp_path) as c:
        for endpoint in ("video", "live-frame", "recording", "report"):
            assert c.get(f"/api/tasks/{first}/{endpoint}").status_code == 401
            assert c.get(f"/api/tasks/missing/{endpoint}", headers=headers).status_code == 404
        assert c.get(f"/api/tasks/{first}/live-frame", headers=headers).content == image
        assert c.get(f"/api/tasks/{second}/live-frame", headers=headers).status_code == 204
        assert c.get(f"/api/tasks/{first}/recording", headers=headers).status_code == 409
        meta.update(status="stopped", recording_complete=True, recording_finalized=True)
        (folder / "stream.json").write_text(json.dumps(meta))
        assert c.get(f"/api/tasks/{first}/live-frame", headers=headers).status_code == 204
        assert c.get(f"/api/tasks/{first}/recording", headers=headers).content == b"recorded-video"
        report = c.get(f"/api/tasks/{first}/report", headers=headers).json()
        assert report["verification"] is None
        assert "尚未开始" in report["text"]
