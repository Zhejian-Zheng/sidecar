# OpenAI 兼容端点：让 Cherry Studio 等客户端把 Sidecar 当作一个“模型”调用。
"""OpenAI-compatible endpoints so LLM clients can drive Sidecar as a model."""

import hashlib
import json
import re
import time
from uuid import uuid4

from fastapi import Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from sidecar.jobs import JobQueue

MODEL_ID = "sidecar-agent"
TERMINAL = {"completed", "failed", "cancelled"}
HANDOFF = {"waiting_user", "paused", "interrupted"}
# 控制命令必须占据整条消息，避免把否定句、引用或说明文字当作恢复授权。
COMMAND_RE = re.compile(r"(继续|查询)\s*[:：]?\s*([0-9a-fA-F]{32})")


class ChatMessage(BaseModel):
    role: str
    content: str | list


class ChatCompletionRequest(BaseModel):
    # Cherry Studio 会附带 temperature、top_p 等额外字段，这里忽略而不是拒绝。
    model_config = ConfigDict(extra="ignore")
    model: str = MODEL_ID
    messages: list[ChatMessage]
    stream: bool = False


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


# 提交新任务或恢复旧任务，轮询直到终态、交接或超时。
def run_completion(q: JobQueue, goal: str, timeout: float = 120, request_id: str | None = None):
    match = COMMAND_RE.fullmatch(goal.strip())
    if match:
        command, task_id = match.group(1), match.group(2).lower()
        job = q.get(task_id)
        if command == "查询" or job["status"] in TERMINAL:
            return job
        if job["status"] in HANDOFF:
            q.resume(task_id, goal)
        return _wait(q, task_id, timeout)
    request_id = request_id or "openai:goal:" + hashlib.sha256(goal.encode()).hexdigest()
    job = submit_goal(q, goal, request_id)
    return _wait(q, job["id"], timeout)


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


# 把任务状态转成给客户端的可读文本；交接时附带“继续 <编号>”以便恢复。
def format_result(job):
    heading = f"任务编号：{job['id']}\n"
    status = job["status"]
    reason = job.get("reason") or ""
    state = job.get("state") or {}
    if status == "completed":
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
        if body.stream:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "Sidecar 暂不支持流式输出，请关闭流式输出并设置 stream=false。",
                        "type": "invalid_request_error",
                        "param": "stream",
                        "code": "stream_not_supported",
                    }
                },
            )
        try:
            goal = extract_user_goal(body.messages)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        # 使用完整对话区分不同轮次；指纹经队列事务持久化，并发与重启后仍去重。
        # 显式 key 可用于有意重做相同任务，保持同一 key 则复用原任务。
        if idempotency_key is not None:
            request_id = "openai:key:" + hashlib.sha256(idempotency_key.encode()).hexdigest()
        else:
            payload = json.dumps(body.model_dump(exclude={"stream"}), sort_keys=True)
            request_id = "openai:auto:" + hashlib.sha256(payload.encode()).hexdigest()
        job = run_completion(q, goal, request_id=request_id)
        return {
            "id": f"chatcmpl-{uuid4().hex}",
            "task_id": job["id"],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.model or MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": format_result(job)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
