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
COMMAND_RE = re.compile(
    r"(?:请)?(?P<command>继续|恢复|查询|查看|状态|进度|取消|停止|暂停)\s*"
    r"(?:(?:[\w .-]{1,40}\s*的\s*)?任务(?:编号)?\s*)?[:：]?\s*"
    r"(?P<id>[0-9a-fA-F]{32})(?P<suffix>\s*的(?:状态|进度))?[。！!]?"
)
HELP_TEXT = """支持以下聊天指令（每次单独发送一条）：
- 查询／状态／进度 <任务编号>
- 取消／停止 <任务编号>（不会撤销已经执行的操作）
- 暂停 <任务编号>（在步骤边界生效）
- 继续／恢复 <任务编号>（先核对执行画面，不授权自动发送或支付）
- 任务列表／查看队列
- 清空队列／清除任务队列（取消所有未结束任务，保留历史）
- 帮助
也支持：取消 Gmail 的任务<编号>、查看任务<编号>的进度。
暂停或等待介入的任务会阻塞后续任务，需继续或取消。"""
STATUS_LABELS = {
    "queued": "等待执行",
    "executing": "正在执行",
    "running": "正在执行",
    "waiting_user": "等待用户介入",
    "paused": "已暂停",
    "interrupted": "执行中断",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


def information(text):
    # An informational reply has no persisted task and never enters the worker queue.
    return {"id": None, "status": "information", "message": text}, False


def chat_information(q, goal):
    if goal in {"帮助", "指令帮助", "help", "/help"}:
        return information(HELP_TEXT)
    if goal in {"任务列表", "查看任务列表", "查看队列", "任务队列"}:
        jobs = q.list()
        lines = ["近期任务（最多 50 条）："]
        for job in jobs:
            lines.append(f"- {job['id']}：{STATUS_LABELS.get(job['status'], job['status'])}")
            if blocker := job.get("blocked_by"):
                lines.append(f"  等待任务 {blocker['id']} 结束或恢复处理。")
        return information("\n".join(lines) if jobs else "当前没有任务。发送“帮助”查看指令。")
    return None


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
    from sidecar.apps import route_app

    # A transport retry must return the original task even after configuration changes.
    with q.db() as db:
        old = db.execute("SELECT id,body FROM jobs WHERE request_id=?", (request_id,)).fetchone()
    if old:
        if json.loads(old["body"])["goal"] != goal:
            raise ValueError("同一个提交编号不能用于不同任务")
        return q.get(old["id"])
    return q.submit(goal=goal, app=route_app(goal, q.settings()), request_id=request_id)


# 在发送响应头前完成校验、去重和入队，确保错误仍可返回正常 HTTP 状态码。
def prepare_completion(q: JobQueue, goal: str, request_id: str | None = None):
    goal = goal.strip()
    if goal in {"清空队列", "清除任务队列"}:
        result = q.clear()
        text = f"已取消 {result['cancelled']} 个任务。"
        if result["pending"]:
            text += f"另有 {result['pending']} 个执行中的任务已请求取消，等待步骤边界生效。"
        return information(text + "历史记录保留，已执行的操作不会撤销。")
    if info := chat_information(q, goal):
        return info
    match = COMMAND_RE.fullmatch(goal)
    if match and (
        re.search(r"不要|勿|别|不(?:取消|停止|暂停|继续|恢复)", goal)
        or (match["suffix"] and match["command"] in {"取消", "停止", "暂停", "继续", "恢复"})
    ):
        return information("未执行控制操作。请发送独立、明确的指令，例如：查询 <任务编号>。")
    if match:
        command, task_id = match["command"], match["id"].lower()
        job = q.get(task_id)
        if command in {"查询", "查看", "状态", "进度"} or job["status"] in TERMINAL:
            return job, False
        if command in {"取消", "停止", "暂停"}:
            return q.control(task_id, "pause" if command == "暂停" else "cancel"), False
        if job["status"] in HANDOFF:
            job = q.resume(task_id, goal)
        return job, True
    # Incomplete command-shaped requests must not become device tasks.
    if re.match(r"^(?:请)?(?:取消|停止|暂停|继续|恢复|查询|查看|状态|进度)", goal) and (
        re.fullmatch(
            r"(?:请)?(?:取消|停止|暂停|继续|恢复|查询|查看|状态|进度)\s*"
            r"(?:[\w .-]{1,40}\s*的\s*)?任务(?:编号)?\s*[:：]?\s*"
            r"[0-9a-fA-F]{0,31}(?:\s*的(?:状态|进度))?[。！!]?",
            goal,
        )
        or re.search(r"[0-9a-fA-F]{32}", goal)
        or re.fullmatch(r"(?:请)?(?:取消|停止|暂停|继续|恢复|查询|查看|状态|进度)[：: ]*", goal)
    ):
        return information(
            "未执行控制操作。请每次提供一个完整任务编号，"
            "例如：取消 <32位任务编号>。发送“帮助”查看用法。"
        )
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
    if job["status"] == "information":
        return ""
    heading = f"任务编号：{job['id']}\n"
    if is_demo_job(job, mode):
        heading += "🧪 离线演示模式：仅模拟记录文字，不会操作手机或真实 App。\n"
    return heading


# 把任务状态转成给客户端的可读文本；交接时附带“继续 <编号>”以便恢复。
def format_result(job, *, mode=None, include_heading=True):
    if job["status"] == "information":
        return job["message"]
    heading = result_heading(job, mode) if include_heading else ""
    status = job["status"]
    reason = job.get("reason") or ""
    state = job.get("state") or {}
    if job.get("pending_control"):
        label = "取消" if job["pending_control"] == "cancel" else "暂停"
        return heading + f"已收到{label}请求，等待当前操作结束后生效。查询进度：查询 {job['id']}"
    if status == "queued":
        text = "⏳ 等待执行，尚未开始。"
        if blocker := job.get("blocked_by"):
            label = STATUS_LABELS.get(blocker["status"], blocker["status"])
            text += f"\n前面的任务 {blocker['id']}（{label}）正在占用队列。"
            if blocker["status"] in HANDOFF:
                text += (
                    f"\n核对画面后回复：继续 {blocker['id']}，或放弃旧任务：取消 {blocker['id']}"
                )
        return heading + text + f"\n查看进度：查询 {job['id']}"
    if status == "completed":
        if is_demo_job(job, mode):
            goal = (job.get("request") or state.get("task") or {}).get("goal", "")
            return heading + f"🧪 离线演示已完成\n仅在本地模拟待办中验证了这条文字：{goal}"
        checks = (state.get("verification") or {}).get("checks", [])
        lines = ["✅ 任务已完成"]
        for check in checks:
            mark = "✓" if check.get("passed") else "✗"
            lines.append(f"- {check.get('criterion', '')}：{check.get('evidence', '')}（{mark}）")
        if "副屏应用未能退出" in reason:
            lines.append("⚠️ 副屏应用未能退出，请核对执行画面。")
        return heading + "\n".join(lines)
    if status in HANDOFF:
        if status == "paused":
            return (
                heading + f"已暂停：{reason}\n核对画面后，请回复：继续 {job['id']}"
                + f"\n放弃此任务，请回复：取消 {job['id']}"
            )
        if (state.get("observation") or {}).get("confirmation_targets") and state.get(
            "verification"
        ):
            lines = [f"⚠️ 需要你介入：{reason}"]
            for check in state["verification"].get("checks", []):
                mark = (
                    "已核对"
                    if check.get("passed")
                    else (
                        "待你最终提交" if check.get("requires_confirmation") else "未完成或未确认"
                    )
                )
                lines.append(f"- {check.get('criterion', '')}：{mark}。{check.get('evidence', '')}")
            lines.append(
                f"请在应用中核对并手动操作；完成后回复：继续 {job['id']}。"
                "回复继续不会授权自动点击发送或支付。"
            )
            lines.append(f"放弃此任务，请回复：取消 {job['id']}")
            return heading + "\n".join(lines)
        return (
            heading
            + f"⚠️ 需要你介入：{reason}\n处理完成后，请回复：继续 {job['id']}"
            + f"\n放弃此任务，请回复：取消 {job['id']}"
        )
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
