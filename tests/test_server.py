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
