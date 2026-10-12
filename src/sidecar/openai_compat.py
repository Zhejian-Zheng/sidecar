# OpenAI 兼容端点：让 Cherry Studio 等客户端把 Sidecar 当作一个“模型”调用。
"""OpenAI-compatible endpoints so LLM clients can drive Sidecar as a model."""

import asyncio
import hashlib
import json
import re
import time
from uuid import uuid4

from fastapi import Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from sidecar.jobs import JobQueue

MODEL_ID = "sidecar-agent"
TERMINAL = {"completed", "failed", "cancelled"}
HANDOFF = {"waiting_user", "paused", "interrupted"}
COMPLETION_TIMEOUT = 120
HEARTBEAT_INTERVAL = 10
# 控制命令必须占据整条消息，避免把否定句、引用或说明文字当作恢复授权。
COMMAND_RE = re.compile(r"(继续|查询)\s*[:：]?\s*([0-9a-fA-F]{32})")


class ChatMessage(BaseModel):
    role: str
    content: str | list


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    # Cherry Studio 会附带 temperature、top_p 等额外字段，这里忽略而不是拒绝。
    model_config = ConfigDict(extra="ignore")
    model: str = MODEL_ID
    messages: list[ChatMessage]
    stream: bool = False
    stream_options: StreamOptions | None = None


# 从对话里取最后一条用户消息的文本；兼容字符串和分段列表两种 content。
def extract_user_goal(messages):
    for message in reversed(messages):
        if message.role != "user":
            continue
        if isinstance(message.content, str):
            text = message.content.strip()
        else:
            text = "".join(
                str(item.get("text", ""))
                for item in message.content
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip()
        if text:
            return text
    raise ValueError("没有找到用户消息")


# 离线演示需要至少一条待办；把整句目标当作待办，验证链路时无需结构化输入。
def submit_goal(q: JobQueue, goal: str, request_id: str):
    if q.settings().mode == "demo":
        return q.submit(goal=goal, items=[goal], request_id=request_id)
    return q.submit(goal=goal, request_id=request_id)


# 在发送响应头前完成校验、去重和入队，确保错误仍可返回正常 HTTP 状态码。
def prepare_completion(q: JobQueue, goal: str, request_id: str | None = None):
    match = COMMAND_RE.fullmatch(goal.strip())
    if match:
        command, task_id = match.group(1), match.group(2).lower()
        job = q.get(task_id)
        if command == "查询" or job["status"] in TERMINAL:
            return job, False
        if job["status"] in HANDOFF:
            job = q.resume(task_id, goal)
        return job, True
    request_id = request_id or "openai:goal:" + hashlib.sha256(goal.encode()).hexdigest()
    return submit_goal(q, goal, request_id), True


# 非流式调用保持同步等待的原有行为。
def run_completion(q: JobQueue, goal: str, timeout: float = 120, request_id: str | None = None):
    job, wait = prepare_completion(q, goal, request_id)
    return _wait(q, job["id"], timeout) if wait else job


def _wait(q: JobQueue, task_id: str, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        job = q.get(task_id)
        status = job["status"]
        if status in TERMINAL or status in HANDOFF:
            return job
        if time.monotonic() >= deadline:
            return job
        time.sleep(0.3)


def is_demo_job(job, mode=None):
    # 查询历史任务时以其实际执行设备为准，不能用当前配置覆盖历史模式。
    device_id = (job.get("state") or {}).get("device_id")
    return device_id.startswith("offline-demo:") if device_id else mode == "demo"


def result_heading(job, mode=None):
    heading = f"任务编号：{job['id']}\n"
    if is_demo_job(job, mode):
        heading += "🧪 离线演示模式：仅模拟记录文字，不会操作手机或真实 App。\n"
    return heading


# 把任务状态转成给客户端的可读文本；交接时附带“继续 <编号>”以便恢复。
def format_result(job, *, mode=None, include_heading=True):
    heading = result_heading(job, mode) if include_heading else ""
    status = job["status"]
    reason = job.get("reason") or ""
    state = job.get("state") or {}
    if status == "completed":
        if is_demo_job(job, mode):
            goal = (job.get("request") or state.get("task") or {}).get("goal", "")
            return heading + f"🧪 离线演示已完成\n仅在本地模拟待办中验证了这条文字：{goal}"
        checks = (state.get("verification") or {}).get("checks", [])
        lines = ["✅ 任务已完成"]
        for check in checks:
            mark = "✓" if check.get("passed") else "✗"
            lines.append(f"- {check.get('criterion', '')}：{check.get('evidence', '')}（{mark}）")
        return heading + "\n".join(lines)
    if status in HANDOFF:
        return heading + f"⚠️ 需要你介入：{reason}\n处理完成后，请回复：继续 {job['id']}"
    if status == "cancelled":
        return heading + f"已取消：{reason}"
    if status == "failed":
        return heading + f"任务失败：{reason}"
    return heading + f"⏳ 任务仍在执行中（{status}）。稍后请回复：查询 {job['id']}"


# SSE 推送真实任务状态；断开连接仅结束此生成器，不影响独立 worker。
async def stream_completion(q, job, wait, model, include_usage=False, mode=None):
    completion_id = f"chatcmpl-{uuid4().hex}"
    created = int(time.time())
    task_id = job["id"]

    def chunk(delta, finish_reason=None, usage=False):
        data = {
            "id": completion_id,
            "task_id": task_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if include_usage:
            data["usage"] = None
        if usage:
            data["choices"] = []
            data["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"

    yield chunk({"role": "assistant", "content": result_heading(job, mode)})
    deadline = time.monotonic() + COMPLETION_TIMEOUT
    heartbeat = time.monotonic() + HEARTBEAT_INTERVAL
    last_status = None
    while True:
        status = job["status"]
        if not wait or status in TERMINAL | HANDOFF or time.monotonic() >= deadline:
            yield chunk({"content": format_result(job, mode=mode, include_heading=False)})
            break
        if status != last_status:
            label = {"queued": "等待执行", "executing": "正在执行", "running": "正在执行"}
            yield chunk({"content": f"⏳ {label.get(status, status)}…\n"})
            last_status = status
        if time.monotonic() >= heartbeat:
            yield ": keep-alive\n\n"
            heartbeat = time.monotonic() + HEARTBEAT_INTERVAL
        await asyncio.sleep(min(0.3, HEARTBEAT_INTERVAL))
        job = await asyncio.to_thread(q.get, task_id)
    yield chunk({}, finish_reason="stop")
    if include_usage:
        yield chunk({}, usage=True)
    yield "data: [DONE]\n\n"


# 挂载 OpenAI 兼容路由；复用现有的 Bearer 配对令牌鉴权。
def register(app, q: JobQueue, authenticate):
    @app.get("/v1/models", dependencies=[Depends(authenticate)])
    def models():
        return {
            "object": "list",
            "data": [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "sidecar"}],
        }

    @app.post("/v1/chat/completions", dependencies=[Depends(authenticate)])
    def chat_completions(
        body: ChatCompletionRequest, idempotency_key: str | None = Header(default=None)
    ):
        try:
            goal = extract_user_goal(body.messages)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        # 使用完整对话区分不同轮次；指纹经队列事务持久化，并发与重启后仍去重。
        # 显式 key 可用于有意重做相同任务，保持同一 key 则复用原任务。
        if idempotency_key is not None:
            request_id = "openai:key:" + hashlib.sha256(idempotency_key.encode()).hexdigest()
        else:
            payload = json.dumps(
                body.model_dump(exclude={"stream", "stream_options"}), sort_keys=True
            )
            request_id = "openai:auto:" + hashlib.sha256(payload.encode()).hexdigest()
        if body.stream:
            job, wait = prepare_completion(q, goal, request_id)
            return StreamingResponse(
                stream_completion(
                    q,
                    job,
                    wait,
                    body.model or MODEL_ID,
                    include_usage=bool(body.stream_options and body.stream_options.include_usage),
                    mode=q.settings().mode,
                ),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no"},
            )
        job = run_completion(q, goal, timeout=COMPLETION_TIMEOUT, request_id=request_id)
        return {
            "id": f"chatcmpl-{uuid4().hex}",
            "task_id": job["id"],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.model or MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": format_result(job, mode=q.settings().mode),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
