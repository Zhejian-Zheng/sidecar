# 独立后台执行进程：浏览器断开不取消任务；进程重启先处理遗留执行状态。
"""Independent worker: browser disconnects do not cancel tasks."""

import argparse
import json
import os
import time
from pathlib import Path

from sidecar.contracts import ModelAPIError, Task
from sidecar.harness import Harness
from sidecar.jobs import JobQueue
from sidecar.lifecycle import TERMINAL
from sidecar.model import LangChainModel
from sidecar.offline import PREFIX, OfflineDevice, OfflinePolicy
from sidecar.store import Store


# 把引擎里遗留的非终态任务收尾，解除它对工作区的占用。
def _settle_engine(q, task_id, reason):
    store = Store(q.engine)
    try:
        state = store.get(task_id)
        if state["status"] in TERMINAL:
            return
        state.update(status="cancelled", reason=reason)
        store.save(state)
        store.event(task_id, "status", {"status": state["status"], "reason": state["reason"]})
    except ValueError:
        pass  # 引擎里没有该任务。
    finally:
        store.close()


# 启动时对账队列与引擎：任何「引擎非终态但队列已终态」的组合都会永久锁死工作区。
def recover(home):
    q = JobQueue(home)
    with q.db() as db:
        rows = db.execute("SELECT id,control,status FROM jobs").fetchall()
    for row in rows:
        task_id, control, status = row["id"], row["control"], row["status"]
        state = q.state(task_id)
        if state and state["status"] in TERMINAL:
            # 引擎已有结论：用它对齐尚未写回的队列行。
            if status not in TERMINAL:
                q.finish(task_id, state["status"], state["reason"])
            continue
        if status in TERMINAL:
            # 队列已终态、引擎仍活着：这正是锁死工作区的组合，必须收尾引擎。
            _settle_engine(q, task_id, "队列已结束，清理遗留执行现场")
            continue
        if control == "cancel" or status == "cancelled":
            _settle_engine(q, task_id, "中断后已取消，不再发出新操作")
            q.finish(task_id, "cancelled", "已取消中断任务")
        elif status == "executing":
            q.finish(task_id, "interrupted", "执行进程中断，请核对设备后继续或取消")
        # paused / waiting_user：保留现场，等用户核对后恢复。


# 领取一项任务，选择设备和模型，调用 Harness 后写回队列状态。
def run_next(home):
    q = JobQueue(home)
    config = q.settings()
    # 前置校验放在领取之前：条件不满足时保持队列不动，避免把整个队列逐个消费成失败。
    if config.mode == "android" and (
        os.environ.get("SIDECAR_MODEL") != config.model
        or os.environ.get("SIDECAR_BASE_URL", "") != config.base_url
    ):
        return False
    job = q.claim()
    if not job:
        return False
    task_id = job["id"]
    try:
        request = json.loads(job["body"])
        saved = q.state(task_id)
        if config.mode == "demo":
            device, model = OfflineDevice(q.engine), OfflinePolicy()
        else:
            from sidecar.android import AndroidDevice

            options = {"display": config.display} if config.display else {}
            # 任务级目标 App：显式 app 优先，否则用默认 package；越界即拒绝执行。
            target = q.target_package(request, config)
            device = AndroidDevice(
                config.serial, target, allowed=config.allowed_packages, **options
            )
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
                elif not saved and (launch := getattr(device, "launch", None)):
                    launch()

            if saved:
                # A resumed task's fresh control request wins before execution.
                result = h.resume(task_id, job["note"], prepare=prepare)
            else:
                result = h.start(task, task_id=task_id, prepare=prepare)
        if config.display and result["status"] in TERMINAL:
            try:
                with device.execution_lock():
                    device.close_app()
            except Exception as exc:
                # A cleanup error must not erase verified task results or replay its actions.
                result["reason"] += "；副屏应用未能退出，请核对执行画面"
                store = Store(q.engine)
                try:
                    store.event(task_id, "cleanup_error", {"type": type(exc).__name__})
                finally:
                    store.close()
        q.finish(task_id, result["status"], result["reason"])
    except Exception as exc:
        # 先读引擎状态：已完成的任务不能被兜底报成失败，未完成的要留成可恢复的中断。
        state = q.state(task_id)
        if state and state["status"] in TERMINAL:
            q.finish(task_id, state["status"], state["reason"])
        elif state:
            q.finish(task_id, "interrupted", "执行中断，请核对设备后继续或取消")
        elif isinstance(exc, ModelAPIError):
            # 固定的可读消息（含状态码），不含请求或响应正文。
            q.finish(task_id, "failed", str(exc))
        else:
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
