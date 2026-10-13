# 持久任务队列：网页负责提交与控制，worker 负责领取；执行状态保存在独立索引中。
"""Persistent single-device queue shared by the HTTP server and worker."""

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from sidecar.contracts import Message, Task
from sidecar.displays import DisplayBinding
from sidecar.harness import TERMINAL, Harness
from sidecar.offline import PREFIX, OfflineDevice, OfflinePolicy
from sidecar.store import Store


# 保存执行模式、设备和模型名称等配置，不在队列配置中保存密钥。
# allowed_packages 是唯一允许操作的 App 白名单；package 是未指定任务目标时的默认项。
class Settings(Message):
    mode: Literal["demo", "android"] = "demo"
    serial: str = "emulator-5554"
    package: str = ""
    allowed_packages: list[str] = Field(default_factory=list, max_length=256)
    app_aliases: dict[str, list[str]] = Field(default_factory=dict, max_length=256)
    model: str = ""
    base_url: str = ""
    display: DisplayBinding | None = None

    # Android 模式必须至少一个允许的 App 和模型；package 自动并入允许列表。
    @model_validator(mode="after")
    def validate_mode(self):
        if self.display and self.mode != "android":
            raise ValueError("隐藏执行屏只能用于 Android 模式")
        if self.mode == "android":
            allowed = list(dict.fromkeys(self.allowed_packages))
            if not allowed:
                # 只给 --package 时，它同时充当唯一白名单（向后兼容）。
                if not self.package:
                    raise ValueError("Android 模式需要至少一个允许的 App 包名")
                allowed = [self.package]
            elif self.package and self.package not in allowed:
                # 已给出白名单时，--package 只能作为默认目标，不能扩大白名单。
                raise ValueError(f"默认目标 App 不在允许列表内：{self.package}")
            if not self.model:
                raise ValueError("Android 模式需要配置模型")
            # 包名限定 ASCII，避免非 ASCII 名称通过校验后才在设备侧失败。
            if any(not re.fullmatch(r"[A-Za-z0-9_.]+", name) for name in allowed):
                raise ValueError("App 包名格式不正确")
            self.allowed_packages = allowed
            self.package = self.package or allowed[0]
            for package, names in self.app_aliases.items():
                if package not in allowed or not names or any(not n.strip() for n in names):
                    raise ValueError("App 别名必须非空，且只可绑定允许列表内的 App")
        return self


# 校验网页任务输入；app 指定本任务的目标 App，request_id 用于识别同一次提交的重试。
class Submission(Message):
    goal: str = Field(min_length=1, max_length=4000)
    items: list[str] = Field(default_factory=list, max_length=20)
    criteria: list[str] = Field(default_factory=list, max_length=20)
    app: str | None = Field(default=None, max_length=200)
    request_id: str | None = Field(default=None, max_length=100)


# 协调队列记录与 Harness 状态，面向网页提供统一任务视图。
class JobQueue:
    # 创建队列目录、任务表和配置表；engine 子目录留给 Harness。
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.engine = self.home / "engine"
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, request_id TEXT UNIQUE,
                body TEXT NOT NULL, status TEXT NOT NULL, control TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, body TEXT)")

    # 每次操作使用短连接；正常提交事务，异常回滚并关闭连接。
    @contextmanager
    def db(self):
        db = sqlite3.connect(self.home / "queue.sqlite", timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    # 有未完成任务时禁止切换执行配置，避免恢复到不同环境。
    def configure(self, settings):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT body FROM settings WHERE id=1").fetchone()
            value = settings.model_dump_json()
            if (
                old
                and old[0] != value
                and db.execute(
                    "SELECT 1 FROM jobs WHERE status NOT IN ('completed','failed','cancelled')"
                ).fetchone()
            ):
                raise ValueError("先结束当前任务，再切换执行配置")
            db.execute("INSERT OR REPLACE INTO settings VALUES (1,?)", (value,))

    # 读取保存的配置；未配置时默认使用离线演示。
    def settings(self):
        with self.db() as db:
            row = db.execute("SELECT body FROM settings WHERE id=1").fetchone()
        return Settings.model_validate_json(row[0]) if row else Settings()

    # 解析任务的目标 App：显式 app 优先，否则用默认 package；必须落在允许列表内。
    def target_package(self, request, settings=None):
        settings = settings or self.settings()
        target = request.get("app") or settings.package
        if target not in settings.allowed_packages:
            raise ValueError(f"目标 App 不在允许列表：{target}")
        return target

    # 校验输入并入队；任务级 app 越界时在提交阶段就拒绝。
    def submit(self, goal, items=None, criteria=None, app=None, request_id=None):
        request = Submission(
            goal=goal,
            items=items or [],
            criteria=criteria or [],
            app=app,
            request_id=request_id,
        )
        # 归一化幂等键：strip 后为空视为「没有幂等键」，避免 '' 与空白互相误撞。
        request_id = request.request_id or None
        settings = self.settings()
        if settings.mode == "demo":
            if not request.items:
                raise ValueError("离线演示需要至少一条待办事项")
            # PREFIX 计入 criteria 上限；超长时给中文说明，而不是回吐 pydantic 原文。
            if any(len(PREFIX) + len(x) > 500 for x in request.items):
                raise ValueError("离线演示的每条待办最多 495 字，请缩短任务描述")
            Task(goal=goal, criteria=[PREFIX + x for x in request.items])
        else:
            self.target_package(request.model_dump(), settings)
            if request.criteria:
                Task(goal=goal, criteria=request.criteria)
        body = request.model_dump_json(exclude={"request_id"})
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["body"] != body:
                    raise ValueError("同一个提交编号不能用于不同任务")
                task_id = old["id"]
            else:
                task_id = uuid4().hex
                db.execute(
                    "INSERT INTO jobs(id,request_id,body,status) VALUES (?,?,?,?)",
                    (task_id, request_id, body, "queued"),
                )
        return self.get(task_id)

    # 读取原始队列记录，保留供 worker 使用的请求 JSON。
    def raw(self, task_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError("任务不存在")
        return dict(row)

    # 按需读取 Harness 状态；任务尚未进入执行器时返回 None。
    def state(self, task_id):
        if not (self.engine / "tasks.sqlite").exists():
            return None
        store = Store(self.engine)
        try:
            return store.get(task_id)
        except ValueError:
            return None
        finally:
            store.close()

    # 合并队列和执行状态，向页面暴露尚未确认的控制请求。
    def get(self, task_id):
        row = self.raw(task_id)
        row["request"] = json.loads(row.pop("body"))
        state = self.state(task_id)
        row["state"] = state
        if row["status"] == "executing" and state:
            row["status"], row["reason"] = state["status"], state["reason"]
        if row["control"] and row["status"] not in TERMINAL:
            row["pending_control"] = row["control"]
        return row

    # 按创建顺序倒序返回最近五十项任务及其当前状态。
    def list(self):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs ORDER BY seq DESC LIMIT 50")]
        return [self.get(i) for i in ids]

    # 在写事务中领取最早的待执行任务；暂停或交接任务仍阻止后续领取。
    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM jobs WHERE status NOT IN ('queued','completed','failed','cancelled')"
            ).fetchone():
                return None
            row = db.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY seq LIMIT 1"
            ).fetchone()
            if row:
                db.execute("UPDATE jobs SET status='executing' WHERE id=?", (row["id"],))
                return dict(row)
        return None

    # 原子核对控制请求再写回；已接受的取消优先，既有终态不被迟到结果覆盖。
    def finish(self, task_id, status, reason=""):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,control FROM jobs WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError("任务不存在")
            if row["status"] in TERMINAL:
                return
            if row["control"] == "cancel":
                status, reason = "cancelled", "已取消；已执行的操作不会自动撤销"
                # 执行器可能先完成、控制请求后到达。先同步任务索引再提交队列，
                # 中途崩溃仍保留 executing 记录，重启可再次对账；不删除已有证据。
                if self.state(task_id):
                    store = Store(self.engine)
                    try:
                        store.control(task_id, "cancel")
                        state = store.save(store.get(task_id))
                        store.event(
                            task_id,
                            "status",
                            {"status": state["status"], "reason": state["reason"]},
                        )
                    finally:
                        store.close()
            db.execute("UPDATE jobs SET status=?, reason=? WHERE id=?", (status, reason, task_id))

    # 先记录队列控制请求，再向已有的 Harness 任务转交信号。
    def control(self, task_id, signal):
        if signal not in {"pause", "cancel"}:
            raise ValueError("未知控制指令")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError("任务不存在")
            if row["status"] in TERMINAL:
                raise ValueError("任务已经结束")
            if row["control"] == "cancel":
                return self.get(task_id)
            db.execute("UPDATE jobs SET control=? WHERE id=?", (signal, task_id))
            if row["status"] != "executing":
                db.execute(
                    "UPDATE jobs SET status=? WHERE id=?",
                    ("cancelled" if signal == "cancel" else "paused", task_id),
                )
        if self.state(task_id):
            with Harness(self.engine, OfflineDevice(self.engine), OfflinePolicy()) as harness:
                try:
                    harness.control(task_id, signal)
                except ValueError:
                    pass  # Worker may have finished between the two databases.
        return self.get(task_id)

    # 核对状态与备注后重新入队，等待 worker 从当前现场恢复。
    def resume(self, task_id, note):
        if not note.strip() or len(note) > 1000:
            raise ValueError("请填写核对结果，最多 1000 字")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError("任务不存在")
            if row[0] not in {"paused", "waiting_user", "interrupted"}:
                raise ValueError("当前任务不能恢复")
            db.execute(
                "UPDATE jobs SET status='queued', control='', note=?, reason='' WHERE id=?",
                (note, task_id),
            )
        return self.get(task_id)

    # 读取任务最近六十条执行事件，供页面展示。
    def events(self, task_id):
        self.raw(task_id)
        if not self.state(task_id):
            return []
        store = Store(self.engine)
        try:
            return store.events(task_id)[-60:]
        finally:
            store.close()
