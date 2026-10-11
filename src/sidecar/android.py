# Android 适配层：把真实界面转换为观察，并将结构化动作交给设备子进程。
"""Android GUI adapter. Each device RPC runs in a bounded child process."""

import hashlib
import json
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

from sidecar.contracts import Action, Observation

SENSITIVE = re.compile(
    r"password|sign in|log in|verification code|pay now|purchase|delete|remove account|"
    r"密码|登录|验证码|立即支付|确认支付|确认付款|删除|注销",
    re.I,
)


# 封装设备 RPC 子进程，避免设备调用无限阻塞主执行流程。
class Transport:
    # 固定目标设备编号，防止多设备连接时误选手机。
    def __init__(self, serial):
        self.serial = serial

    # 通过标准输入传 JSON，限制调用时长；设备写入超时须按结果不确定处理。
    def call(self, op, payload=None):
        # Never use shell interpolation. Timeouts of writes remain uncertain.
        result = subprocess.run(
            [sys.executable, "-m", "sidecar.android_rpc"],
            input=json.dumps({"serial": self.serial, "op": op, "payload": payload or {}}),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError("Android connection failed; check ADB and device permissions")
        return json.loads(result.stdout)


# 将允许的 App、控件定位和设备锁接入通用 Device 接口。
class AndroidDevice:
    # 校验设备及包名格式，并初始化最近一次观察的控件缓存。
    def __init__(self, serial, package, transport=None, lock_root=None):
        if not re.fullmatch(r"[\w.:-]+", serial) or not re.fullmatch(r"[\w.]+", package):
            raise ValueError("Invalid device serial or app package")
        self.serial, self.package = serial, package
        self.device_id = f"android:{serial}:{package}"
        self.transport = transport or Transport(serial)
        self.lock_root = Path(lock_root or Path.home() / ".cache" / "sidecar" / "devices")
        self.nodes = {}
        self.latest = None

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
    def observe(self):
        data = self.transport.call("observe")
        root = ET.fromstring(data["xml"])
        nodes, lines = {}, []
        sensitive = False
        for index, node in enumerate(root.iter("node")):
            attrs = node.attrib
            label = (attrs.get("text", "") + " " + attrs.get("content-desc", "")).strip()
            password = attrs.get("password") == "true"
            sensitive |= password or bool(SENSITIVE.search(label))
            # Do not expose credentials, including the screenshot, to the model.
            if password:
                label = "[private input]"
            if label:
                lines.append(label[:300])
            # 只限制交给模型的可操作控件；敏感检测仍遍历整棵树。
            if len(nodes) >= 200:
                continue
            if attrs.get("enabled") == "false":
                continue
            if attrs.get("clickable") != "true" and "EditText" not in attrs.get("class", ""):
                continue
            if attrs.get("package") != self.package:
                continue
            bounds = [int(n) for n in re.findall(r"\d+", attrs.get("bounds", ""))]
            if len(bounds) != 4 or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                continue
            identity = f"{index}:{attrs.get('resource-id')}:{attrs.get('class')}"
            key = hashlib.sha256(identity.encode()).hexdigest()[:12]
            nodes[key] = dict(
                label=label[:300],
                bounds=bounds,
                editable="EditText" in attrs.get("class", ""),
                focused=attrs.get("focused") == "true",
            )
        self.nodes = nodes
        context = f"App: {data['package']}\nActivity: {data.get('activity', '')}\n"
        requires_human = sensitive or data["package"] != self.package
        obs = Observation(
            text=(context + "\n".join(lines))[:16000],
            elements={k: json.dumps(v, ensure_ascii=False) for k, v in nodes.items()},
            image=None if requires_human else "data:image/png;base64," + data["image"],
            requires_human=requires_human,
        )
        self.latest = obs
        return obs

    # 检查交接标记、目标控件和输入焦点，决定是否允许执行。
    def assess(self, action, observation):
        if observation.requires_human:
            return "handoff"
        if action.target and action.target not in self.nodes:
            return "deny"
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
        if node:
            payload["bounds"] = node["bounds"]
        self.transport.call("execute", payload)
