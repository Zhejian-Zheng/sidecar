# 设备子进程入口：从 stdin 读取一次请求，向 stdout 返回 JSON，诊断输出走 stderr。
"""Bounded subprocess bridge; stdout contains only the JSON response."""

import base64
import io
import json
import sys
import time
from contextlib import nullcontext, redirect_stdout, suppress

from sidecar.errors import ObservationError, observation_stage


def connect_device(serial):
    import uiautomator2 as u2
    from uiautomator2 import core

    device = u2.connect(serial)
    recoverable_call = device.jsonrpc_call

    # 底层默认会重启服务并重发失败 RPC。写操作结果不确定时必须交接，不能重发。
    # 仅覆盖这个子进程中的设备实例；依赖内部单次调用，库不兼容时直接失败。
    def call_once(method, params=None, timeout=10):
        if method in {"dumpWindowHierarchy", "takeScreenshot", "deviceInfo"}:
            return recoverable_call(method, params, timeout)
        return core._jsonrpc_call(
            device._dev, device._device_server_port, method, params, timeout, False
        )

    device.jsonrpc_call = call_once
    return device


def launch_app(device, package):
    resolved = device.shell(["cmd", "package", "resolve-activity", "--brief", package])
    components = [line.strip() for line in resolved.output.splitlines() if "/" in line]
    if resolved.exit_code or not components or not components[-1].startswith(package + "/"):
        raise RuntimeError("Cannot resolve the configured app launcher")
    activity = components[-1].split("/", 1)[1]
    device.app_start(package, activity=activity, wait=False, stop=False)
    if not device.app_wait(package, timeout=5, front=True):
        raise RuntimeError("Configured app did not reach the foreground")


# 连接指定 Android；读取截图和控件，或把单步动作转换为自动化工具调用。
def run():
    request = json.load(sys.stdin)
    if request.get("display") is not None:
        return display_request(request)
    with redirect_stdout(sys.stderr):
        with observation_stage("connection") if request["op"] == "observe" else nullcontext():
            d = connect_device(request["serial"])
        d.settings["wait_timeout"] = 5
        # 观察包含当前 App、完整控件树和缩小后的截图，供主进程解析。
        if request["op"] == "launch":
            launch_app(d, request["payload"]["package"])
            response = {}
        elif request["op"] == "observe":
            with observation_stage("foreground"):
                current = d.app_current()
            with observation_stage("hierarchy"):
                xml = d.dump_hierarchy()
            with observation_stage("screenshot"):
                image = d.screenshot()
                image.thumbnail((960, 1600))
                out = io.BytesIO()
                image.save(out, format="PNG")
            with observation_stage("page_changed"):
                if d.app_current() != current:
                    raise ObservationError("page_changed")
            response = dict(
                xml=xml,
                package=current["package"],
                activity=current.get("activity", ""),
                image=base64.b64encode(out.getvalue()).decode(),
            )
        elif request["op"] == "execute":
            # 控件动作使用边界中心；视觉坐标按设备尺寸转换，输入替换当前字段。
            p = request["payload"]
            kind = p["kind"]
            if kind in {"tap", "type"} and "bounds" in p:
                left, top, right, bottom = p["bounds"]
                d.click((left + right) // 2, (top + bottom) // 2)
            if kind == "type":
                # Replaces the focused field; Unicode supported by UiAutomator.
                d(focused=True).set_text(p["text"], timeout=5)
            elif kind == "tap" and "bounds" not in p:
                width, height = d.window_size()
                d.click(min(width - 1, int(p["x"] * width)), min(height - 1, int(p["y"] * height)))
            elif kind == "swipe":
                points = {
                    "up": (0.5, 0.75, 0.5, 0.25),
                    "down": (0.5, 0.25, 0.5, 0.75),
                    "left": (0.8, 0.5, 0.2, 0.5),
                    "right": (0.2, 0.5, 0.8, 0.5),
                }
                d.swipe(*points[p["direction"]], duration=0.3)
            elif kind == "back":
                d.press("back")
            elif kind == "wait":
                time.sleep(1)
            response = {}
        else:
            raise ValueError("Unsupported operation")
    return response


# 只让受控消息跨进程边界：其它异常降级为类型名，避免泄露 traceback 或敏感内容。
def main():
    from sidecar.contracts import DeviceSetupError

    try:
        print(json.dumps(run()))
    except ObservationError as exc:
        print(json.dumps({"error": {"observation_stage": exc.stage}}))
    except Exception as exc:
        setup = isinstance(exc, DeviceSetupError)
        message = str(exc) if setup else type(exc).__name__
        print(json.dumps({"error": {"setup": setup, "message": message}}))


def display_request(request):
    from sidecar import displays

    serial = request["serial"]
    binding = displays.DisplayBinding.model_validate(request["display"])
    with redirect_stdout(sys.stderr):
        if request["op"] == "launch":
            if request["package"] != request["payload"]["package"]:
                raise ValueError("Configured package mismatch")
            displays.launch(serial, binding, request["package"])
        elif request["op"] == "close_app":
            displays.close_app(serial, binding, request["package"])
        elif request["op"] == "observe":
            with observation_stage("connection"):
                device = connect_device(serial)
            try:
                package, activity, xml, image = displays.observe(serial, binding, device)
            except Exception:
                # A release failure must not overwrite the original capture failure.
                with suppress(Exception):
                    device.stop_uiautomator()
                raise
            else:
                with observation_stage("release"):
                    device.stop_uiautomator()
            with observation_stage("screenshot"):
                image.thumbnail((960, 1600))
                out = io.BytesIO()
                image.save(out, format="PNG")
            return dict(
                package=package,
                activity=activity,
                xml=xml,
                image=base64.b64encode(out.getvalue()).decode(),
                display=binding.model_dump(),
            )
        elif request["op"] == "execute":
            displays.verify(serial, binding)
            if displays.current(serial, binding.id)[0] != request["package"]:
                raise RuntimeError("Target app left hidden display; refusing input")
            if request["payload"]["kind"] == "type":
                displays.set_text(serial, binding, request["package"], request["payload"])
            else:
                displays.execute(serial, binding, request["payload"])
        else:
            raise ValueError("Unsupported display operation")
    return {}


if __name__ == "__main__":
    main()
