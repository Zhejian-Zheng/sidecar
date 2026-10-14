# 保存任务索引、控制请求、事件和动作意图；图检查点由 Harness 单独保存。
"""Local task index, control mailbox, and write-ahead action journal."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


# 提供本地任务存储及工作目录执行锁，供执行者与控制入口协作。
class Store:
    # 创建任务、事件和动作日志表；WAL 模式便于读写进程协作。
    def __init__(self, home: Path):
        self.home = home
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(home / "tasks.sqlite", timeout=10)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, control TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, task TEXT NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS events_task_seq ON events(task, seq);
            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, payload TEXT NOT NULL,
                outcome TEXT NOT NULL DEFAULT 'started'
            );
        """)

    # 关闭本实例持有的 SQLite 连接。
    def close(self):
        self.connection.close()

    # 以非阻塞文件锁限制同一目录的执行者；进程退出后由系统释放锁。
    @contextmanager
    def execution_lock(self):
        # macOS/Linux local workspace lock; OS releases it if the worker dies.
        import fcntl

        with (self.home / "execution.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("workspace busy: another executor is active") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # 登记任务前检查是否有未结束任务占用环境，避免覆盖暂停现场。
    def create(self, state: dict):
        if self.connection.execute(
            "SELECT 1 FROM tasks WHERE id=?", (state["task_id"],)
        ).fetchone():
            raise ValueError("task already exists")
        for (payload,) in self.connection.execute("SELECT payload FROM tasks"):
            if json.loads(payload)["status"] not in {"completed", "failed", "cancelled"}:
                raise ValueError("unfinished task reserves this workspace; resume or cancel it")
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO tasks(id,payload) VALUES (?,?)",
                    (state["task_id"], json.dumps(state)),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("task already exists") from exc

    # 读取完整任务状态；未知编号统一抛出可处理的错误。
    def get(self, task_id: str) -> dict:
        row = self.connection.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("unknown task")
        return json.loads(row[0])

    # 在写事务中合并状态，防止其他连接刚提交的取消被旧状态覆盖。
    def save(self, state: dict) -> dict:
        with self.connection:
            # Serialize read/modify/write with controllers on other connections.
            # Cancellation is irreversible, including at an interrupt boundary.
            self.connection.execute("BEGIN IMMEDIATE")
            saved = self.get(state["task_id"])
            if saved["status"] == "cancelled" or self.signal(state["task_id"]) == "cancel":
                state = {**state, "status": "cancelled", "reason": "Cancelled by user"}
            self.connection.execute(
                "UPDATE tasks SET payload=? WHERE id=?", (json.dumps(state), state["task_id"])
            )
        return state

    # 写入控制请求；取消一旦登记，不允许后续请求清除或降级。
    def control(self, task_id: str, value: str):
        if value not in {"", "pause", "cancel"}:
            raise ValueError("control must be pause or cancel")
        self.get(task_id)
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET control=CASE WHEN control='cancel' THEN control ELSE ? END "
                "WHERE id=?",
                (value, task_id),
            )

    # 读取控制信号，供 Agent 在步骤边界响应。
    def signal(self, task_id: str) -> str:
        row = self.connection.execute("SELECT control FROM tasks WHERE id=?", (task_id,)).fetchone()
        return row[0]

    # 追加带时间的任务事件，用于追踪和页面展示。
    def event(self, task_id: str, kind: str, payload: dict):
        with self.connection:
            self.connection.execute(
                "INSERT INTO events(task,kind,payload) VALUES (?,?,?)",
                (task_id, kind, json.dumps(payload)),
            )

    # Fetch only the requested page in SQL, then return it in chronological order.
    # No limit preserves the existing CLI/full-history interface.
    def events(self, task_id: str, *, limit: int | None = None,
               before: int | None = None) -> list[dict]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        if before is not None and before < 1:
            raise ValueError("before must be positive")
        self.get(task_id)
        query = "SELECT seq,kind,payload,at FROM events WHERE task=?"
        params = [task_id]
        if before is not None:
            query += " AND seq < ?"
            params.append(before)
        query += " ORDER BY seq DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [
            {"sequence": r[0], "kind": r[1], "data": json.loads(r[2]), "at": r[3]}
            for r in reversed(rows)
        ]

    # 在设备动作前登记唯一意图；重复编号返回 False，阻止重复执行。
    def reserve(self, task_id: str, action_id: str, payload: dict) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO actions(id,task,payload) VALUES (?,?,?)",
                (action_id, task_id, json.dumps(payload)),
            )
        return cursor.rowcount == 1

    # 记录动作调用的结果状态；接口返回不等于业务结果已验证。
    def outcome(self, action_id: str, value: str):
        with self.connection:
            self.connection.execute("UPDATE actions SET outcome=? WHERE id=?", (value, action_id))

    # 检查是否存在已开始但尚未明确结果的动作。
    def unresolved(self, task_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM actions WHERE task=? AND outcome IN ('started','uncertain') LIMIT 1",
                (task_id,),
            ).fetchone()
            is not None
        )

    # 记录用户已核对不确定动作，不把人工核对等同于任务完成。
    def acknowledge(self, task_id: str):
        with self.connection:
            self.connection.execute(
                "UPDATE actions SET outcome='user_reviewed' WHERE task=? "
                "AND outcome IN ('started','uncertain')",
                (task_id,),
            )
