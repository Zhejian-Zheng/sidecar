# Android 适配层：把真实界面转换为观察，并将结构化动作交给设备子进程。
"""Android GUI adapter. Each device RPC runs in a bounded child process."""

import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

from sidecar.contracts import Action, DeviceSetupError, Observation
from sidecar.displays import DisplayBinding
from sidecar.errors import ObservationError, TextInputError

SENSITIVE = re.compile(
    r"password|sign in|log in|sign up|\bregister\b|create account|create an account|"
    r"new account|verification code|remove account|"
    r"密码|登录|注册|验证码|注销|创建账号|创建账户|新用户",
    re.I,
)
CONFIRMATION = re.compile(
    r"pay now|place order|checkout|submit order|\bbuy\b|\bpay\b|\bpurchase\b|"
    r"\bsend\b|\btransfer\b|\bpublish\b|"
    r"支付|付款|购买|转账|发送|发布|提交订单|结算|下单|提现",
    re.I,
)
DESTRUCTIVE = re.compile(r"delete|remove|clear|erase|reset|删除|清除|清空|移除|重置", re.I)


def overlaps(bounds, blocked):
    left, top, right, bottom = bounds
    return any(
        left < blocked_right
        and right > blocked_left
        and top < blocked_bottom
        and bottom > blocked_top
        for blocked_left, blocked_top, blocked_right, blocked_bottom in blocked
    )


# Device RPC needs runtime/ADB configuration, never provider credentials.
DEVICE_ENV_KEYS = {
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP",
    "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "WINDIR",
    "PYTHONPATH", "VIRTUAL_ENV", "ANDROID_HOME", "ANDROID_SDK_ROOT",
    "ANDROID_USER_HOME", "ANDROID_ADB_SERVER_PORT", "ADB_SERVER_SOCKET",
    "ADB_VENDOR_KEYS",
}


# 封装设备 RPC 子进程，避免设备调用无限阻塞主执行流程。
class Transport:
    # 固定目标设备编号，防止多设备连接时误选手机。
    def __init__(self, serial, display=None, package=None, stream=None):
        self.serial = serial
        self.display = display
        self.package = package
        self.stream = str(stream) if stream else None

    # 通过标准输入传 JSON，限制调用时长；设备写入超时须按结果不确定处理。
    def call(self, op, payload=None):
        # Never use shell interpolation. Timeouts of writes remain uncertain.
        # 读页面在切换动画/重连时可能失败，最多重新采集一次，绝不重放写入。
        attempts = 2 if op == "observe" else 1
        for attempt in range(attempts):
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "sidecar.android_rpc"],
                    input=json.dumps(
                        {
                            "serial": self.serial,
                            "op": op,
                            "payload": payload or {},
                            "display": self.display.model_dump() if self.display else None,
                            "package": self.package,
                            "stream": self.stream,
                        }
                    ),
                    env={key: value for key, value in os.environ.items()
                         if key in DEVICE_ENV_KEYS},
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode:
                    raise RuntimeError(
                        "Android connection failed; check ADB and device permissions"
                    )
                data = json.loads(result.stdout)
                # 子进程把受控错误放在 stdout 的 error 字段，保留可读原因。
                if isinstance(data, dict) and "error" in data:
                    error = data["error"] or {}
                    if "text_input_stage" in error:
                        raise TextInputError(error["text_input_stage"])
                    if "observation_stage" in error:
                        raise ObservationError(error["observation_stage"])
                    message = error.get("message") or "Device operation failed"
                    if error.get("setup"):
                        raise DeviceSetupError(message)
                    raise RuntimeError(message)
                return data
            except (RuntimeError, subprocess.TimeoutExpired, ValueError, OSError) as exc:
                if attempt == attempts - 1:
                    if (
                        op == "execute"
                        and (payload or {}).get("kind") == "type"
                        and not isinstance(exc, TextInputError)
                    ):
                        raise TextInputError("transport") from exc
                    if op == "observe" and not isinstance(exc, ObservationError):
                        raise ObservationError("transport") from exc
                    raise


# 将允许的 App、控件定位和设备锁接入通用 Device 接口。
class AndroidDevice:
    # 校验设备、任务目标 App 与允许列表，并初始化最近一次观察的控件缓存。
    def __init__(self, serial, package, transport=None, lock_root=None, display=None, allowed=None):
        if not re.fullmatch(r"[\w.:-]+", serial) or not re.fullmatch(r"[\w.]+", package):
            raise ValueError("Invalid device serial or app package")
        self.serial, self.package = serial, package
        # 允许列表是执行侧的安全边界：任务目标必须落在其中，且不能为空。
        self.allowed = list(dict.fromkeys(allowed)) if allowed else [package]
        if any(not re.fullmatch(r"[\w.]+", name) for name in self.allowed):
            raise ValueError("Invalid allowed package name")
        if package not in self.allowed:
            raise ValueError("Target app is outside the allowed package list")
        self.device_id = f"android:{serial}:{package}"
        self.display = DisplayBinding.model_validate(display) if display is not None else None
        if self.display:
            self.device_id += (
                ":display:"
                + hashlib.sha256(self.display.model_dump_json().encode()).hexdigest()[:20]
            )
        self.transport = transport or Transport(serial, self.display, package)
        self.lock_root = Path(lock_root or Path.home() / ".cache" / "sidecar" / "devices")
        self.nodes = {}
        self.latest = None
        self.blocked_bounds = []
        self.confirmation_bounds = []
        self.protected_click_bounds = []
        self.width = None
        self.height = None

    # 仅打开配置中批准的目标 App，不接受模型指定任意包名。
    def launch(self):
        self.transport.call("launch", {"package": self.package})

    def close_app(self):
        if self.display:
            self.transport.call("close_app")

    # 按设备编号加文件锁，限制同一用户下同时操作该设备的执行者。
    @contextmanager
    def execution_lock(self):
        import fcntl

        self.lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(self.serial.encode()).hexdigest()
        with (self.lock_root / f"{key}.lock").open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "device busy: another executor owns this Android device"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    # 解析控件树和截图，识别交接条件，并缓存本次可操作控件。
    def observe_fresh(self):
        return self.observe(fresh=True)

    def observe(self, fresh=False):
        data = (self.transport.call("observe", {"fresh": True}) if fresh
                else self.transport.call("observe"))
        if self.display and data.get("display") != self.display.model_dump():
            raise RuntimeError("Observation does not belong to the configured hidden display")
        root = ET.fromstring(data["xml"])
        nodes, lines, occurrences = {}, [], {}
        self.blocked_bounds = []
        self.confirmation_bounds = []
        clickable_bounds = []
        sensitive = False
        for node in root.iter("node"):
            attrs = node.attrib
            label = (attrs.get("text", "") + " " + attrs.get("content-desc", "")).strip()
            password = attrs.get("password") == "true"
            # resource-id 常带 submit_order / delete_btn 等语义词，一并纳入检测。
            resource = re.sub(r"([a-z])([A-Z])", r"\1 \2", attrs.get("resource-id", ""))
            haystack = label + " " + re.sub(r"[_/-]", " ", resource)
            sensitive |= password or bool(SENSITIVE.search(haystack))
            bounds = [int(n) for n in re.findall(r"-?\d+", attrs.get("bounds", ""))]
            # 负坐标意味着节点部分离开显示区域，点击中心会落到别的控件上，直接丢弃。
            valid_bounds = (
                len(bounds) == 4
                and bounds[2] > bounds[0]
                and bounds[3] > bounds[1]
                and bounds[0] >= 0
                and bounds[1] >= 0
            )
            # Scan all clickable parents, including those outside the model's element limit.
            if valid_bounds and attrs.get("clickable") == "true":
                clickable_bounds.append(bounds)
            # Gboard/AOSP/etc. expose the text backspace key as "Delete". It is not an
            # App delete action; its IME-window bounds can overlap App controls.
            package_name = attrs.get("package", "")
            ime_backspace = (
                package_name != self.package
                and package_name == data.get("active_ime_package")
                and attrs.get("class") == "android.widget.FrameLayout"
                and not attrs.get("text")
                and not attrs.get("resource-id")
                and attrs.get("content-desc", "").strip().casefold()
                in {"delete", "backspace", "删除", "退格"}
            )
            if DESTRUCTIVE.search(haystack) and not ime_backspace:
                if valid_bounds:
                    self.blocked_bounds.append(bounds)
                else:
                    sensitive = True
            # Editable content may mention sending/payment without being a submit control.
            if "EditText" not in attrs.get("class", "") and CONFIRMATION.search(haystack):
                if valid_bounds:
                    self.confirmation_bounds.append(bounds)
                else:
                    sensitive = True  # Cannot safely isolate an unlocated submit control.
            # Do not expose credentials, including the screenshot, to the model.
            if password:
                label = "[private input]"
            # Keep IME key labels out of the model text; they are not App controls.
            if label and not ime_backspace:
                state = ""
                if attrs.get("checkable") == "true":
                    state = f" [checked={str(attrs.get('checked') == 'true').lower()}]"
                lines.append(label[:300] + state)
            # 只限制交给模型的可操作控件；敏感检测仍遍历整棵树。
            if len(nodes) >= 200:
                continue
            if attrs.get("enabled") == "false":
                continue
            if attrs.get("clickable") != "true" and "EditText" not in attrs.get("class", ""):
                continue
            if attrs.get("package") != self.package:
                continue
            if not valid_bounds:
                continue
            # 状态栏等无关节点增减不应改变目标编号；位置或目标状态改变仍会失效。
            identity = json.dumps([attrs.get("resource-id"), attrs.get("class"), bounds])
            occurrence = occurrences.get(identity, 0)
            occurrences[identity] = occurrence + 1
            key = hashlib.sha256(f"{identity}:{occurrence}".encode()).hexdigest()[:12]
            nodes[key] = dict(
                label=label[:300],
                bounds=bounds,
                editable="EditText" in attrs.get("class", ""),
                focused=attrs.get("focused") == "true",
                checked=(attrs.get("checked") == "true"
                         if attrs.get("checkable") == "true"
                         and attrs.get("checked") in {"true", "false"} else None),
                selected=attrs.get("selected") == "true",
            )
            if not label:
                # Preserve the distinction between a container and a labelled button.
                child_labels = []
                for child in node.iter("node"):
                    if child is node or child.get("password") == "true":
                        continue
                    if child.get("package") != self.package:
                        continue
                    text = (child.get("text", "") + " " + child.get("content-desc", "")).strip()
                    if text and text[:100] not in child_labels:
                        child_labels.append(text[:100])
                    if len(child_labels) == 8:
                        break
                nodes[key]["container"] = True
                nodes[key]["child_labels"] = child_labels
            if self.display:
                nodes[key]["selector"] = dict(
                    resource_id=attrs.get("resource-id", ""),
                    class_name=attrs.get("class", ""),
                    bounds=bounds,
                    old_text=attrs.get("text", ""),
                    description=attrs.get("content-desc", ""),
                )
        self.nodes = nodes
        sensitive_bounds = self.blocked_bounds + self.confirmation_bounds
        self.protected_click_bounds = sensitive_bounds + [
            bounds for bounds in clickable_bounds if overlaps(bounds, sensitive_bounds)
        ]
        confirmation_targets = {}
        for key, node in nodes.items():
            if overlaps(node["bounds"], self.confirmation_bounds):
                node["requires_confirmation"] = True
                confirmation_targets[key] = node["label"] or "敏感提交控件"
        context = f"App: {data['package']}\nActivity: {data.get('activity', '')}\n"
        if self.display:
            context += (
                "Hidden display: type requires an explicit editable target; replaces its text.\n"
            )
        # 每个任务只操作一个目标 App：前台不是它就交接。
        # 允许列表在构造时已校验，这里不放宽为「列表内其它 App 也可操作」。
        # 记录屏幕尺寸，供 assess 把归一化坐标换算成像素落点。
        self.width = data.get("width")
        self.height = data.get("height")
        foreground = data["package"]
        out_of_scope = foreground != self.package
        requires_human = sensitive or out_of_scope
        from sidecar.verifiers import extract_ui

        ui, truncated = extract_ui(data["xml"]) if not requires_human else ([], True)
        obs = Observation(
            ui=ui, ui_truncated=truncated, package=foreground,
            app_version=data.get("app_version"),
            text=(context + "\n".join(lines))[:16000],
            elements={k: json.dumps(v, ensure_ascii=False) for k, v in nodes.items()},
            image=None if requires_human else "data:image/png;base64," + data["image"],
            requires_human=requires_human,
            confirmation_targets=confirmation_targets,
            viewport=(self.width, self.height) if self.width and self.height else None,
            display_session=self.display.model_dump_json() if self.display else None,
        )
        self.latest = obs
        return obs

    # 把 0–1 的归一化坐标换算为像素落点；缺少屏幕尺寸时返回 None。
    def pixel_point(self, x, y):
        if x is None or y is None or not self.width or not self.height:
            return None
        return int(x * self.width), int(y * self.height)

    # 检查交接标记、目标控件和输入焦点，决定是否允许执行。
    def assess(self, action, observation):
        if observation.requires_human:
            return "handoff"
        if self.display and action.kind == "type" and not action.target:
            return "handoff"
        if action.target and action.target not in self.nodes:
            return "deny"
        if action.target and overlaps(
            self.nodes[action.target]["bounds"], self.blocked_bounds + self.confirmation_bounds
        ):
            return "handoff"
        # 目标控件按区域重叠检查；坐标点击还要保护敏感标签所属的可点击父控件。
        # wait 和 back 沿用导航策略，不作为控件点击处理。
        if (self.blocked_bounds or self.confirmation_bounds) and action.kind not in {
            "wait",
            "back",
        }:
            sensitive = self.blocked_bounds + self.confirmation_bounds
            if action.kind == "tap" and action.target is None:
                # A child label can belong to a larger clickable submission control.
                sensitive = self.protected_click_bounds
                point = self.pixel_point(action.x, action.y)
            else:
                node = self.nodes.get(action.target)
                if not node or action.kind not in {"tap", "type"}:
                    return "handoff"
                left, top, right, bottom = node["bounds"]
                point = ((left + right) // 2, (top + bottom) // 2)
            if point is None or any(
                left <= point[0] <= right and top <= point[1] <= bottom
                for left, top, right, bottom in sensitive
            ):
                return "handoff"
        if action.kind == "type":
            node = (
                self.nodes.get(action.target)
                if action.target
                else next((v for v in self.nodes.values() if v["editable"] and v["focused"]), None)
            )
            if not node or not node["editable"]:
                return "handoff"
        return "allow"

    # 检查目标控件是否仍有效；坐标动作使用更严格的全界面指纹比较。
    def is_current(self, action, before, after):
        if before.viewport != after.viewport or before.display_session != after.display_session:
            return False
        if before.requires_human or after.requires_human:
            return False
        # App and activity must match even if unrelated text or pixels move.
        if before.text.splitlines()[:2] != after.text.splitlines()[:2]:
            return False
        if action.target:
            return action.target in before.elements and before.elements[
                action.target
            ] == after.elements.get(action.target)
        if action.kind == "wait":
            return True
        return before.fingerprint() == after.fingerprint()

    # 再次检查本地策略，将控件位置与动作一起交给 RPC 子进程。
    def execute(self, action: Action):
        if self.latest is None or self.assess(action, self.latest) != "allow":
            raise ValueError("Observe an allowed app before executing")
        node = self.nodes.get(action.target)
        if action.kind == "type" and node is None:
            node = next(v for v in self.nodes.values() if v["editable"] and v["focused"])
        payload = action.model_dump()
        if self.display:
            payload["expected_size"] = [self.width, self.height]
            payload["observation_id"] = self.latest.id
        if node:
            payload["bounds"] = node["bounds"]
            if self.display and action.kind == "type":
                payload["selector"] = node["selector"]
        self.transport.call("execute", payload)
