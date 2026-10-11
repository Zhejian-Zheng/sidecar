# 独立后台执行进程：浏览器断开不取消任务；进程重启先处理遗留执行状态。
"""Independent worker: browser disconnects do not cancel tasks."""

import argparse
import json
import os
import time
from pathlib import Path

from sidecar.contracts import Task
from sidecar.demo import PREFIX, DemoDevice, DemoPolicy
from sidecar.harness import TERMINAL, Harness
from sidecar.jobs import JobQueue
from sidecar.model import LangChainModel
from sidecar.store import Store


# 核对上次执行中的任务：同步终态、处理取消，其余标为需要人工核对的中断。
def recover(home):
    q = JobQueue(home)
    with q.db() as db:
        rows = db.execute("SELECT id,control FROM jobs WHERE status='executing'").fetchall()
    for row in rows:
        state = q.state(row["id"])
        if state and state["status"] in TERMINAL:
            q.finish(row["id"], state["status"], state["reason"])
        elif row["control"] == "cancel":
            q.control(row["id"], "cancel")
            if state:
                with Harness(q.engine, DemoDevice(q.engine), DemoPolicy()) as h:
                    h.control(row["id"], "cancel")
            q.finish(row["id"], "cancelled", "已取消中断任务")
        else:
            q.finish(row["id"], "interrupted", "执行进程中断，请核对设备后继续或取消")


# 领取一项任务，选择设备和模型，调用 Harness 后写回队列状态。
def run_next(home):
    q = JobQueue(home)
    job = q.claim()
    if not job:
        return False
    task_id = job["id"]
    try:
        config = q.settings()
        request = json.loads(job["body"])
        saved = q.state(task_id)
        if config.mode == "demo":
            device, model = DemoDevice(q.engine), DemoPolicy()
        else:
            from sidecar.android import AndroidDevice

            if (
                os.environ.get("SIDECAR_MODEL") != config.model
                or os.environ.get("SIDECAR_BASE_URL", "") != config.base_url
            ):
                raise ValueError("模型配置已变化，请重新启动服务")
            device = AndroidDevice(config.serial, config.package)
            model = LangChainModel.from_env()
        # 恢复时沿用已保存的任务与完成标准，不再请求模型规划。
        if saved:
            task = Task.model_validate(saved["task"])
        elif config.mode == "demo":
            task = Task(goal=request["goal"], criteria=[PREFIX + x for x in request["items"]])
        else:
            task = (
                Task(goal=request["goal"], criteria=request["criteria"])
                if request["criteria"]
                else model.plan(request["goal"])
            )
        with Harness(q.engine, device, model, max_decisions=50, max_seconds=600) as h:
            # 把启动期间收到的控制请求同步给 Harness，避免请求尚未进入执行状态。
            def prepare():
                control = q.raw(task_id)["control"]
                if control:
                    h.store.control(task_id, control)

            if saved:
                # A resumed task's fresh control request wins before execution.
                prepare()
                result = h.resume(task_id, job["note"])
            else:
                result = h.start(task, task_id=task_id, prepare=prepare)
        q.finish(task_id, result["status"], result["reason"])
    except Exception as exc:
        # Never store exception bodies: providers and devices may include secrets.
        q.finish(task_id, "failed", f"执行失败（{type(exc).__name__}），请检查设备和模型配置")
    return True


# 持有队列 worker 锁后循环取任务；空队列短暂等待，once 模式只检查一轮。
def run_worker(home, once=False):
    q = JobQueue(home)
    lock = Store(q.home / "worker-lock")
    try:
        with lock.execution_lock():
            recover(q.home)
            while True:
                worked = run_next(q.home)
                if once:
                    break
                if not worked:
                    time.sleep(0.5)
    except RuntimeError:
        return  # A worker is already servicing this queue.
    finally:
        lock.close()


# 解析独立 worker 的运行目录与单轮执行参数。
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run_worker(args.home, args.once)


if __name__ == "__main__":
    main()
