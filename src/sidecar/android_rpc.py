# 设备子进程入口：从 stdin 读取一次请求，向 stdout 返回 JSON，诊断输出走 stderr。
"""Bounded subprocess bridge; stdout contains only the JSON response."""

import base64
import io
import json
import sys
import time
from contextlib import redirect_stdout


# 连接指定 Android；读取截图和控件，或把单步动作转换为自动化工具调用。
def main():
    request = json.load(sys.stdin)
    with redirect_stdout(sys.stderr):
        import uiautomator2 as u2

        d = u2.connect(request["serial"])
        d.settings["wait_timeout"] = 5
        # 观察包含当前 App、完整控件树和缩小后的截图，供主进程解析。
        if request["op"] == "observe":
            current = d.app_current()
            xml = d.dump_hierarchy()
            image = d.screenshot()
            image.thumbnail((960, 1600))
            out = io.BytesIO()
            image.save(out, format="PNG")
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
    print(json.dumps(response))


if __name__ == "__main__":
    main()
