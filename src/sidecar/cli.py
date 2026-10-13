# 命令行入口：离线演示、模型演示、设备观察和网页服务共用项目配置。
"""Local CLI for demo, Android observation, and the phone console."""

import argparse
import json
import os
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from sidecar.contracts import Task
from sidecar.demo import PREFIX, DemoDevice, DemoPolicy
from sidecar.harness import Harness
from sidecar.model import LangChainModel


# 以可读 JSON 输出结果，保留中文，方便终端查看及其他程序解析。
def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


# 集中定义子命令；--home 是所有模式共用的运行数据目录。
def parser():
    root = argparse.ArgumentParser(description="Sidecar GUI Harness")
    root.add_argument("--home", type=Path, default=Path(".sidecar"))
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run a simulated task; no real phone is controlled")
    demo.add_argument("--item", action="append")
    demo.add_argument("--require-login", action="store_true")
    demo.add_argument(
        "--llm", action="store_true", help="Send demo observations to configured model"
    )
    demo.add_argument("--goal", help="Natural-language goal (requires --llm)")
    resume = commands.add_parser("resume")
    resume.add_argument("task_id")
    resume.add_argument("--note", required=True)
    for name in ("status", "events", "pause", "cancel"):
        commands.add_parser(name).add_argument("task_id")
    commands.add_parser("demo-login", help="Simulate the user logging in; no real account involved")
    observe = commands.add_parser("observe", help="Read an Android screen locally; no model call")
    observe.add_argument("--serial", default="emulator-5554")
    observe.add_argument("--package", required=True)
    observe.add_argument("--output", type=Path)
    serve = commands.add_parser("serve", help="Start phone console and independent task worker")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--android", action="store_true")
    serve.add_argument(
        "--hidden-display",
        action="store_true",
        help="Use a hidden secondary Android display (experimental)",
    )
    serve.add_argument("--serial", default="emulator-5554")
    serve.add_argument("--package", default="", help="Default target app when a task omits one")
    serve.add_argument(
        "--allowed-packages",
        default="",
        help="Comma-separated allow-list of app packages, e.g. com.a,com.b (one app per task)",
    )
    serve.add_argument(
        "--app-alias",
        action="append",
        default=[],
        metavar="PACKAGE=NAME",
        help="Name used to select an allowed app in chat; repeat for more names",
    )
    worker = commands.add_parser("worker", help="Run the queue worker independently")
    worker.add_argument("--once", action="store_true")
    return root


# 根据命令选择观察、worker、网页或演示路径，并统一处理可读错误。
def main():
    args = parser().parse_args()
    try:
        home = args.home.resolve()
        # 队列服务退出后仍能取消遗留任务，避免新副屏因未结束任务无法启动。
        if (home / "queue.sqlite").exists() and args.command in {
            "pause",
            "cancel",
            "status",
            "events",
            "resume",
        }:
            from sidecar.jobs import JobQueue
            from sidecar.server import worker_alive
            from sidecar.worker import recover

            queue = JobQueue(home)
            try:
                queue.raw(args.task_id)
            except KeyError:
                pass  # 同目录中的旧 CLI 演示任务仍交给原入口处理。
            else:
                if args.command in {"pause", "cancel", "resume"} and not worker_alive(home):
                    recover(home)
                if args.command in {"pause", "cancel"}:
                    result = queue.control(args.task_id, args.command)
                elif args.command == "resume":
                    result = queue.resume(args.task_id, args.note)
                elif args.command == "events":
                    result = queue.events(args.task_id)
                else:
                    result = queue.get(args.task_id)
                emit(result)
                return
        # 只读设备检查：在本地保存截图，不调用模型或执行点击。
        if args.command == "observe":
            import base64

            from sidecar.android import AndroidDevice

            device = AndroidDevice(args.serial, args.package)
            with device.execution_lock():
                observation = device.observe()
            output = args.output or home / "android-screen.png"
            if observation.image:
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                output.write_bytes(base64.b64decode(observation.image.split(",", 1)[1]))
            emit(
                {
                    "device": device.device_id,
                    "requires_human": observation.requires_human,
                    "elements": len(observation.elements),
                    "image": str(output.resolve()) if observation.image else None,
                }
            )
            return
        if args.command == "worker":
            from sidecar.worker import run_worker

            run_worker(home, args.once)
            return
        # 网页服务负责收发请求；另起 worker，使浏览器断开后任务仍可继续。
        if args.command == "serve":
            import uvicorn

            from sidecar.displays import hidden_display
            from sidecar.jobs import JobQueue, Settings
            from sidecar.server import create_app, pairing_token, start_worker

            allowed = [name.strip() for name in args.allowed_packages.split(",") if name.strip()]
            aliases = {}
            for entry in args.app_alias:
                package, separator, name = entry.partition("=")
                if not separator or not package.strip() or not name.strip():
                    raise ValueError("--app-alias 格式为 包名=应用名称")
                aliases.setdefault(package.strip(), []).append(name.strip())
            config = Settings(
                mode="android" if args.android else "demo",
                serial=args.serial,
                package=args.package or (allowed[0] if allowed else ""),
                allowed_packages=allowed,
                app_aliases=aliases,
                model=os.environ.get("SIDECAR_MODEL", ""),
                base_url=os.environ.get("SIDECAR_BASE_URL", ""),
            )
            if args.android and not os.environ.get("SIDECAR_API_KEY"):
                raise ValueError("Android 模式需要显式配置 SIDECAR_API_KEY")
            if args.hidden_display and not args.android:
                raise ValueError("--hidden-display 需要 --android")
            if args.hidden_display and any(
                j["status"] not in {"completed", "failed", "cancelled"}
                for j in JobQueue(home).list()
            ):
                raise ValueError("创建新的隐藏执行屏前，请先结束当前任务")
            with hidden_display(args.serial) if args.hidden_display else nullcontext() as display:
                config.display = display
                JobQueue(home).configure(config)
                token = pairing_token(home)
                start_worker(home)
                print(f"Sidecar: http://{args.host}:{args.port}\n配对码: {token}", flush=True)
                uvicorn.run(
                    create_app(home, token), host=args.host, port=args.port, access_log=False
                )
            return
        # 下面的命令使用离线模拟设备；真实手机任务通过网页队列进入 worker。
        device = DemoDevice(home)
        if args.command in {"status", "events", "pause", "cancel", "demo-login"}:
            with Harness(home, device, DemoPolicy()) as agent:
                if args.command == "status":
                    emit(agent.status(args.task_id))
                elif args.command == "events":
                    emit(agent.events(args.task_id))
                elif args.command == "demo-login":
                    with agent.store.execution_lock():
                        state = device.load()
                        state["logged_in"] = True
                        device.save(state)
                    emit({"mode": "offline_demo", "logged_in": True})
                else:
                    agent.control(args.task_id, args.command)
                    emit({"task_id": args.task_id, "requested": args.command})
            return

        if args.command == "demo":
            if args.goal and not args.llm:
                raise ValueError("--goal requires --llm; offline policy is not a language model")
            items = args.item or ["复习项目架构", "整理演示视频"]
            task = Task(
                goal=args.goal or "创建以下待办：" + "、".join(items),
                criteria=[PREFIX + item for item in items],
            )
            task_id = uuid4().hex
            metadata = {
                "mode": "llm_demo" if args.llm else "offline_demo",
                "model": os.environ.get("SIDECAR_MODEL") if args.llm else None,
                "base_url": os.environ.get("SIDECAR_BASE_URL") if args.llm else None,
            }
        else:
            task_id = args.task_id
            if not task_id.isalnum():
                raise ValueError("invalid task id")
            metadata_path = home / "runs" / f"{task_id}.json"
            if not metadata_path.exists():
                raise ValueError("unknown demo task")
            metadata = json.loads(metadata_path.read_text())
            if metadata["mode"] == "llm_demo" and (
                metadata["model"] != os.environ.get("SIDECAR_MODEL")
                or metadata["base_url"] != os.environ.get("SIDECAR_BASE_URL")
            ):
                raise ValueError("resume requires the original model and endpoint configuration")

        model = LangChainModel.from_env() if metadata["mode"] == "llm_demo" else DemoPolicy()

        # 在任务获准启动后保存演示元信息，必要时模拟等待用户登录。
        def prepare_demo():
            # Runs after task admission, under the executor lock.
            runs = home / "runs"
            runs.mkdir(exist_ok=True)
            (runs / f"{task_id}.json").write_text(json.dumps(metadata))
            if args.require_login:
                state = device.load()
                state["logged_in"] = False
                device.save(state)

        with Harness(home, device, model) as agent:
            result = (
                agent.start(task, task_id, prepare=prepare_demo)
                if args.command == "demo"
                else agent.resume(task_id, args.note)
            )
            emit({**result, "mode": metadata["mode"]})
            if result["status"] == "failed":
                raise SystemExit(1)
    except (ValueError, RuntimeError, OSError) as exc:
        parser().exit(2, f"Sidecar: {exc}\n")


if __name__ == "__main__":
    main()
