# 手机控制入口：提供静态页面与受配对令牌保护的 API，设备执行交给独立 worker。
"""Authenticated phone console. The independent worker owns execution."""

import base64
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from sidecar import openai_compat
from sidecar.contracts import Message
from sidecar.conversation import ConversationUnavailable, default_timezone
from sidecar.jobs import JobQueue, Submission
from sidecar.store import Store

STATIC = Path(__file__).parent / "static"


# 创建或复用本地配对令牌，新文件只允许当前用户读写。
# 设置 SIDECAR_PAIRING_TOKEN 可固定令牌，跨目录、跨启动保持一致，便于客户端长期接入。
def pairing_token(home):
    configured = os.environ.get("SIDECAR_PAIRING_TOKEN")
    if configured and configured.strip():
        return configured.strip()
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = home / "pairing-token"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as file:
        file.write(token)
    return token


# 启动独立会话中的 worker，将日志写入运行目录；它不随浏览器关闭退出。
def start_worker(home):
    home = Path(home).resolve()
    with (home / "worker.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "sidecar.worker", "--home", str(home)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    return process


# 通过能否取得 worker 锁判断是否已有执行进程占用队列。
def worker_alive(home):
    store = Store(Path(home) / "worker-lock")
    try:
        with store.execution_lock():
            return False
    except RuntimeError:
        return True
    finally:
        store.close()


# 恢复请求必须携带用户核对现场后的说明。
class Resume(Message):
    note: str = Field(min_length=1, max_length=1000)


class ChatInput(Message):
    messages: list[openai_compat.ChatMessage] = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=100)
    app: str | None = Field(default=None, max_length=200)


# 组装页面、鉴权、响应头和任务 API；创建应用本身不会启动 worker。
def create_app(home, token=None):
    default_timezone()  # Validate once at startup, before serving any requests.
    q = JobQueue(home)
    secret = token or pairing_token(home)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    # 核对 Bearer 配对令牌，保护任务、截图和控制接口。
    def authenticate(authorization: str = Header(default="")):
        # 头可能携带 latin-1 高字节；按 bytes 比较，避免 compare_digest 抛 TypeError。
        given = authorization.encode("utf-8", "surrogateescape")
        if not secrets.compare_digest(given, ("Bearer " + secret).encode()):
            raise HTTPException(401, "请先输入配对码连接 Sidecar")

    # 禁止缓存敏感响应，并限制页面脚本、资源来源和嵌入方式。
    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    # 把未知任务转换成统一的 404 响应。
    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": "任务不存在"}, status_code=404)

    # 把状态冲突或业务校验错误转换成可向用户解释的响应。
    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ConversationUnavailable)
    async def conversation_unavailable(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.post("/api/chat", dependencies=[Depends(authenticate)])
    def chat(body: ChatInput):
        goal = openai_compat.extract_user_goal(body.messages)
        job, _ = openai_compat.prepare_completion(
            q, goal, "chat:" + body.request_id, messages=body.messages, app=body.app,
        )
        message = openai_compat.format_result(job, mode=q.settings().mode)
        return {"task_id": job["id"], "message": message, "status": job["status"]}

    # OpenAI 兼容端点，供 Cherry Studio 等客户端调用。
    openai_compat.register(app, q, authenticate)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # 返回手机控制页面；任务数据仍需通过已鉴权的 API 获取。
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # 返回运行模式、设备和 worker 状态，不返回模型密钥。
    @app.get("/api/config", dependencies=[Depends(authenticate)])
    def config():
        value = q.settings()
        return {
            "mode": value.mode,
            "package": value.package,
            "allowed_packages": value.allowed_packages,
            "serial": value.serial,
            "hidden_display": value.display is not None,
            "worker_online": worker_alive(q.home),
            "background_push": False,
        }

    # 返回任务列表摘要，避免列表携带完整观察和截图。
    @app.get("/api/tasks", dependencies=[Depends(authenticate)])
    def tasks():
        # List projection never includes screenshots or complete observation history.
        return [{k: v for k, v in job.items() if k != "state"} for job in q.list()]

    # 保存任务并返回编号，不在 HTTP 请求内运行 Agent。
    @app.post("/api/tasks", status_code=201, dependencies=[Depends(authenticate)])
    def submit(body: Submission):
        return q.submit(**body.model_dump())

    # 返回单项任务、验证结果和近期事件，截图通过独立接口获取。
    @app.get("/api/tasks/{task_id}", dependencies=[Depends(authenticate)])
    def status(task_id: str):
        result = q.get(task_id)
        result["report"] = openai_compat.format_result(result, mode=q.settings().mode)
        if result["state"]:
            result["state"] = {k: v for k, v in result["state"].items() if k != "observation"}
        result["events"] = q.events(task_id)
        return result

    # Stable cursor pagination: use the first sequence in a page to request older events.
    @app.get("/api/tasks/{task_id}/events", dependencies=[Depends(authenticate)])
    def events(
        task_id: str,
        limit: int = Query(default=60, ge=1, le=500),
        before: int | None = Query(default=None, ge=1),
    ):
        return q.events(task_id, limit=limit, before=before)

    # 读取最近观察的图片；无图片时返回 204，这不是连续视频流。
    @app.get("/api/tasks/{task_id}/frame", dependencies=[Depends(authenticate)])
    def frame(task_id: str):
        state = q.get(task_id)["state"] or {}
        image = (state.get("observation") or {}).get("image")
        if not image:
            return Response(status_code=204)
        prefix, data = image.split(",", 1)
        return Response(
            base64.b64decode(data), media_type="image/png" if "png" in prefix else "image/jpeg"
        )

    # Media is task-scoped and protected by the same pairing token as task state.
    def video_directory(task_id):
        from sidecar.video import session_directory

        q.get(task_id)
        return session_directory(q.home, task_id)

    @app.get("/api/tasks/{task_id}/video", dependencies=[Depends(authenticate)])
    def video_status(task_id: str):
        directory = video_directory(task_id)
        if directory is None:
            return {"status": "unavailable", "recording_available": False}
        meta = json.loads((directory / "stream.json").read_text())
        if meta["status"] == "live" and not 0 <= time.monotonic() - meta["received_at"] < 3:
            meta["status"] = "stalled"
        meta["recording_available"] = (
            meta.get("recording_finalized", False) and (directory / "recording.mkv").exists()
        )
        return meta

    @app.get("/api/tasks/{task_id}/live-frame", dependencies=[Depends(authenticate)])
    def live_frame(task_id: str):
        from sidecar.video import latest_frame

        directory = video_directory(task_id)
        if directory is None:
            return Response(status_code=204)
        try:
            data, meta = latest_frame(directory)
        except TimeoutError:
            return Response(status_code=204)
        return Response(data, media_type="image/png", headers={
            "X-Frame-Sequence": str(meta["sequence"]),
            "X-Video-Session": meta["session_id"],
        })

    @app.get("/api/tasks/{task_id}/recording", dependencies=[Depends(authenticate)])
    def recording(task_id: str):
        directory = video_directory(task_id)
        if directory is None or not (directory / "recording.mkv").exists():
            raise HTTPException(404, "还没有任务录像")
        meta = json.loads((directory / "stream.json").read_text())
        if not meta.get("recording_finalized", False):
            raise HTTPException(409, "录像尚未结束，请在本轮执行结束后下载")
        suffix = "" if meta.get("recording_complete") else "-incomplete"
        return FileResponse(directory / "recording.mkv", media_type="video/x-matroska",
                            filename=f"{task_id}-{directory.name}{suffix}.mkv")

    @app.get("/api/tasks/{task_id}/report", dependencies=[Depends(authenticate)])
    def report(task_id: str):
        job = q.get(task_id)
        return {"task_id": task_id, "status": job["status"],
                "text": openai_compat.format_result(job, mode=q.settings().mode),
                "verification": (job["state"] or {}).get("verification")}

    # 提交暂停请求，实际停止需要等待执行流程到达步骤边界。
    @app.post("/api/tasks/{task_id}/pause", dependencies=[Depends(authenticate)])
    def pause(task_id: str):
        return q.control(task_id, "pause")

    # 提交取消请求；不会撤销已经生效的 App 操作。
    @app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(authenticate)])
    def cancel(task_id: str):
        return q.control(task_id, "cancel")

    # 保存交接备注并重新入队，由 worker 执行恢复。
    @app.post("/api/tasks/{task_id}/resume", dependencies=[Depends(authenticate)])
    def resume(task_id: str, body: Resume):
        return q.resume(task_id, body.note)

    return app
