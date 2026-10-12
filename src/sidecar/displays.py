"""Experimental Android 15 display isolation. Never fall back to display zero."""

import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import Field

from sidecar.contracts import Message


class DisplayBinding(Message):
    id: int = Field(gt=0)
    unique_id: str = Field(min_length=1)
    boot_id: str = Field(min_length=1)


def run(args, timeout=12, input=None):
    result = subprocess.run(args, input=input, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Display operation failed; no primary-display fallback")
    return result.stdout


def shell(serial, args):
    # adb shell uses a remote shell even when the host subprocess does not.
    return run(["adb", "-s", serial, "shell", shlex.join(args)]).decode().strip()


def parse_displays(text):
    result = {}
    for line in text.splitlines():
        match = re.search(
            r'Display id (\d+): DisplayInfo\{"([^"]+)".*?real (\d+) x (\d+).*?'
            r'uniqueId "([^"]+)"',
            line,
        )
        if match and "isValid=true" in line:
            number, name, width, height, unique_id = match.groups()
            result[int(number)] = dict(
                name=name, unique_id=unique_id, width=int(width), height=int(height)
            )
    return result


def listing(serial):
    return parse_displays(shell(serial, ["cmd", "display", "get-displays"]))


def boot_id(serial):
    return shell(serial, ["cat", "/proc/sys/kernel/random/boot_id"])


def verify(serial, binding):
    binding = DisplayBinding.model_validate(binding)
    info = listing(serial).get(binding.id)
    if not info or info["unique_id"] != binding.unique_id or boot_id(serial) != binding.boot_id:
        raise RuntimeError("Hidden display lost or replaced; restart and review the task")
    return info


def foreground(text, display_id):
    blocks = re.split(r"(?m)^Display #(\d+) \(activities from top to bottom\):", text)
    for index in range(1, len(blocks), 2):
        if int(blocks[index]) == display_id:
            match = re.search(
                r"topResumedActivity=ActivityRecord\{\S+ u\d+ ([\w.]+)/([\w.$]+)",
                blocks[index + 1],
            )
            if match:
                return match.groups()
    raise RuntimeError("No resumed app on the selected display")


def current(serial, display_id):
    return foreground(shell(serial, ["dumpsys", "activity", "activities"]), display_id)


def filter_hierarchy(xml, display_id):
    source = ET.fromstring(xml)
    result = ET.Element("hierarchy")

    def copy(node):
        if node.get("display-id") != str(display_id):
            return None
        clean = ET.Element("node", node.attrib)
        for child in node:
            copied = copy(child)
            if copied is not None:
                clean.append(copied)
        return clean

    for root in source:
        copied = copy(root)
        if copied is not None:
            result.append(copied)
    if not len(result):
        raise RuntimeError("No display-scoped hierarchy; refusing an unscoped observation")
    return ET.tostring(result, encoding="unicode")


def screenshot(serial, binding):
    # A separate bounded, silent capture client attaches to this logical display.
    # Android's screencap -d uses physical IDs and cannot safely substitute here.
    from PIL import Image

    verify(serial, binding)
    with tempfile.TemporaryDirectory(prefix="sidecar-frame-") as temp:
        video = str(Path(temp) / "frame.mkv")
        run(
            [
                "scrcpy",
                "--serial",
                serial,
                f"--display-id={binding.id}",
                "--no-window",
                "--no-audio",
                "--no-control",
                "--no-clipboard-autosync",
                "--record=" + video,
                "--time-limit=1",
                "--max-fps=1",
            ],
            timeout=10,
        )
        data = run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                video,
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            timeout=5,
        )
        image = Image.open(io.BytesIO(data))
        image.load()
    verify(serial, binding)
    return image


def observe(serial, binding, device):
    verify(serial, binding)
    package, activity = current(serial, binding.id)
    xml = filter_hierarchy(device.dump_hierarchy(), binding.id)
    image = screenshot(serial, binding)
    if current(serial, binding.id) != (package, activity):
        raise RuntimeError("App changed during display capture; re-observe")
    return package, activity, xml, image


def launch(serial, binding, package):
    verify(serial, binding)
    if not re.fullmatch(r"[\w.]+", package):
        raise ValueError("Invalid package")
    # Do not take the app the human is currently using away from the primary screen.
    if current(serial, 0)[0] == package:
        raise RuntimeError("Target app is in use on the primary display")
    try:
        if current(serial, binding.id)[0] == package:
            return
    except RuntimeError:
        pass  # A new display has no resumed activity yet; launch only on the bound display.
    resolved = shell(serial, ["cmd", "package", "resolve-activity", "--brief", package])
    component = resolved.splitlines()[-1] if resolved else ""
    if not re.fullmatch(re.escape(package) + r"/[\w.$]+", component):
        raise RuntimeError("Cannot resolve target launcher")
    verify(serial, binding)
    shell(serial, ["am", "start", "--display", str(binding.id), "-n", component])
    for _ in range(10):
        try:
            if current(serial, binding.id)[0] == package:
                return
        except RuntimeError:
            pass  # Only repeat the read while the launch animation settles.
        time.sleep(0.2)
    raise RuntimeError("Target app did not open on hidden display")


def execute(serial, binding, payload):
    info = verify(serial, binding)
    kind = payload["kind"]
    prefix = ["input", "-d", str(binding.id)]
    if kind == "tap":
        if "bounds" in payload:
            left, top, right, bottom = payload["bounds"]
            x, y = (left + right) // 2, (top + bottom) // 2
        else:
            x = min(info["width"] - 1, int(payload["x"] * info["width"]))
            y = min(info["height"] - 1, int(payload["y"] * info["height"]))
        shell(serial, prefix + ["tap", str(x), str(y)])
    elif kind == "swipe":
        points = {
            "up": (0.5, 0.75, 0.5, 0.25),
            "down": (0.5, 0.25, 0.5, 0.75),
            "left": (0.8, 0.5, 0.2, 0.5),
            "right": (0.2, 0.5, 0.8, 0.5),
        }[payload["direction"]]
        coords = [
            str(int(p * info["width" if i % 2 == 0 else "height"])) for i, p in enumerate(points)
        ]
        shell(serial, prefix + ["swipe", *coords, "300"])
    elif kind == "back":
        shell(serial, prefix + ["keyevent", "KEYCODE_BACK"])
    elif kind == "wait":
        time.sleep(1)
    else:
        raise ValueError("Isolated text input is not yet supported; handoff required")


def set_text(serial, binding, package, payload):
    """One node-scoped write; never retry or fall back to focus/clipboard input."""
    verify(serial, binding)
    selector = payload.get("selector")
    if not isinstance(selector, dict) or not isinstance(payload.get("text"), str):
        raise ValueError("Explicit input node and text required")
    bridge = Path(__file__).with_name("assets") / "textbridge.jar"
    # Content-addressed name prevents a different local build replacing an in-flight bridge.
    import hashlib

    digest = hashlib.sha256(bridge.read_bytes()).hexdigest()[:20]
    remote = f"/data/local/tmp/sidecar-text-{digest}.jar"
    run(["adb", "-s", serial, "push", str(bridge), remote])
    request = dict(
        display=binding.model_dump(), package=package, selector=selector, text=payload["text"]
    )
    response = run(
        [
            "adb",
            "-s",
            serial,
            "shell",
            "CLASSPATH=" + remote,
            "app_process",
            "/",
            "dev.sidecar.textbridge.Main",
        ],
        input=(json.dumps(request, ensure_ascii=False) + "\n").encode(),
        timeout=15,
    )
    result = json.loads(response)
    if result.get("ok") is not True or result.get("verified") is not True:
        raise RuntimeError("Text input was rejected or could not be verified; do not retry blindly")
    verify(serial, binding)


@contextmanager
def hidden_display(serial):
    for binary in ("adb", "scrcpy", "ffmpeg"):
        if not shutil.which(binary):
            raise ValueError(f"Hidden display requires {binary} on PATH")
    if int(shell(serial, ["getprop", "ro.build.version.sdk"])) < 35:
        raise ValueError("Hidden display currently requires Android 15 or later")
    before = listing(serial)
    main = before[0]
    process = subprocess.Popen(
        [
            "scrcpy",
            "--serial",
            serial,
            f"--new-display={main['width']}x{main['height']}/160",
            "--no-window",
            "--no-audio",
            "--no-control",
            "--no-clipboard-autosync",
            "--no-vd-system-decorations",
            "--display-ime-policy=local",
            "--record=" + os.devnull,
            "--record-format=mkv",
            "--max-fps=5",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < deadline:
            displays = listing(serial)
            added = [
                number
                for number, info in displays.items()
                if number not in before and info["name"] == "scrcpy"
            ]
            if len(added) > 1:
                raise RuntimeError("Ambiguous hidden display creation")
            if added:
                number = added[0]
                yield DisplayBinding(
                    id=number, unique_id=displays[number]["unique_id"], boot_id=boot_id(serial)
                )
                return
            time.sleep(0.2)
        raise RuntimeError("Hidden display did not start")
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
