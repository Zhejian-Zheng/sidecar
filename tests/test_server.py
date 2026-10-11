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
