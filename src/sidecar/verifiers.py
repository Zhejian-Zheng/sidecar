"""Local evidence checks. A required but unavailable verifier never falls back."""

import json
import re
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET

from sidecar.contracts import ClockAlarmRequirement, GmailDraftRequirement, UINode

CLOCK = "com.google.android.deskclock"


def extract_ui(xml):
    root = ET.fromstring(xml)
    nodes = []
    total = sum(1 for _ in root.iter("node"))

    def visit(element, parent=None):
        if len(nodes) >= 1000:
            return
        current = parent
        if element.tag == "node":
            a = element.attrib
            current = str(len(nodes))
            private = a.get("password") == "true"
            checkable = {"true": True, "false": False}.get(a.get("checkable"))
            nodes.append(
                UINode(
                    node_id=current,
                    parent_id=parent,
                    resource_id=a.get("resource-id", ""),
                    klass=a.get("class", ""),
                    package=a.get("package", ""),
                    text="[private input]" if private else a.get("text", "")[:1000],
                    description="" if private else a.get("content-desc", "")[:1000],
                    visible={"true": True, "false": False}.get(a.get("visible-to-user")),
                    window_id=a.get("window-id"),
                    checkable=checkable,
                    checked={"true": True, "false": False}.get(a.get("checked"))
                    if checkable
                    else None,
                )
            )
        for child in element:
            visit(child, current)

    visit(root)
    text_truncated = any(
        len(n.get("text", "")) > 1000 or len(n.get("content-desc", "")) > 1000
        for n in root.iter("node")
    )
    return nodes, total > len(nodes) or text_truncated


_CN_AM = "上午|凌晨|早上|早晨|清晨"
_CN_PM = "下午|晚上|傍晚|中午|午后"


def times(text):
    """24 小时归一化的去重时间；识别 AM/PM 与前置中文 meridiem。

    无 meridiem 的 1–12 点按 24 小时制字面处理（与「建议使用明确的 24 小时制
    HH:MM」一致）；前置「上午/下午/晚上/凌晨/中午」映射到对应半天，
    避免「晚上 11:58」被误读成上午 11:58。
    """
    result = set()
    up = text.upper()
    for match in re.finditer(r"(?<!\d)(\d{1,2}):(\d{2})", up):
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            continue
        period = None
        suffix = re.match(r"\s*(AM|PM)\b", up[match.end() :])
        if suffix:
            period = suffix.group(1)
        else:
            prefix = up[max(0, match.start() - 5) : match.start()]
            if re.search(_CN_AM, prefix):
                period = "AM"
            elif re.search(_CN_PM, prefix):
                period = "PM"
        if period and not 1 <= hour <= 12:
            continue
        if period == "AM":
            hour %= 12
        elif period == "PM":
            hour = hour % 12 + 12
        result.add(f"{hour:02}:{minute:02}")
    return result


def clock_intent(goal):
    """Conservative supported-intent recognition; unknown must not become enable."""
    if re.search(
        r"删除|移除|不要|别|不必|取消|delete|remove|don't|do not|\bor\b|或者|或是", goal, re.I
    ):
        return "unknown"
    disable = bool(re.search(r"关闭|关掉|停用|disable|turn off|switch off", goal, re.I))
    enable = bool(
        re.search(
            r"开启|打开|启用|设置|创建|提醒|enable|turn on|switch on|\bset\b|create", goal, re.I
        )
    )
    if disable and enable:
        return "unknown"
    return "disable" if disable else "enable" if enable else "unknown"


def infer_requirement(package, goal):
    if package == CLOCK:
        candidates = times(goal)
        return ClockAlarmRequirement(
            intent=clock_intent(goal),
            expected_time=next(iter(candidates)) if len(candidates) == 1 else None,
        )
    if package == "com.google.android.gm" and re.search(r"草稿|\bdraft\b", goal, re.I):
        return GmailDraftRequirement()
    return None


@lru_cache(maxsize=1)
def clock_page_paths():
    # Structural allowlist from reviewed saved-list/expanded-card fixtures; no user text.
    return set(
        json.loads((Path(__file__).with_name("assets") / "clock-page-paths.json").read_text())
    )


def recognized_clock_page(nodes):
    if len([n for n in nodes if n.parent_id is None]) != 1:
        return False
    by_id = {n.node_id: n for n in nodes}
    if len(by_id) != len(nodes) or len({n.window_id for n in nodes if n.window_id}) > 1:
        return False
    for node in nodes:
        if node.visible is not True:
            return False
        path, seen, current = [], set(), node
        while current is not None:
            if current.node_id in seen:
                return False
            seen.add(current.node_id)
            path.append([current.klass, current.resource_id])
            if current.parent_id is not None and current.parent_id not in by_id:
                return False
            current = by_id.get(current.parent_id)
        if json.dumps(path[::-1], separators=(",", ":")) not in clock_page_paths():
            return False
    return any(n.resource_id == CLOCK + ":id/onoff" for n in nodes)


def evaluate(task, obs):
    req = task.requirement
    result = dict(
        status="uncertain",
        reason="程序验证证据不足",
        evidence=[],
        observation_id=obs.id,
        verifier_version="clock.v1",
        visible_match_count=0,
        list_complete=None,
        truncated=obs.ui_truncated,
    )

    def verdict(status, reason):
        return {**result, "status": status, "reason": reason}

    if req.kind != "clock_alarm_enabled":
        result["verifier_version"] = "gmail.pending"
        return verdict("uncertain", "Gmail 草稿验证尚未完成真实界面适配，不能确认保存成功")
    if req.intent == "unknown" or clock_intent(task.goal) != req.intent:
        return verdict("uncertain", "任务意图未知或与结构化要求冲突，请澄清")
    if not req.expected_time or (times(task.goal) and times(task.goal) != {req.expected_time}):
        return verdict("uncertain", "目标时间缺失或与用户目标冲突，请澄清")
    if obs.requires_human or obs.package != CLOCK or obs.app_version != "7.5":
        return verdict("uncertain", "需要人工处理，或 App/版本不在已验证范围")
    if obs.ui_truncated or not obs.ui:
        return verdict("uncertain", "节点缺失或截断")
    if any(
        n.package not in {"", CLOCK}
        or n.resource_id.endswith(("material_timepicker_ok_button", "label_input_field"))
        for n in obs.ui
    ):
        return verdict("uncertain", "检测到弹窗或其他包的节点")
    if not recognized_clock_page(obs.ui):
        return verdict("uncertain", "页面结构、可见性或窗口归属不符合已验证样本")
    by_id = {n.node_id: n for n in obs.ui}
    records = {}
    for node in obs.ui:
        if node.resource_id != CLOCK + ":id/onoff":
            continue
        parent = by_id.get(node.parent_id)
        seen = set()
        while parent and parent.klass != "androidx.cardview.widget.CardView":
            if parent.node_id in seen:
                return verdict("uncertain", "节点父子关系异常")
            seen.add(parent.node_id)
            parent = by_id.get(parent.parent_id)
        if (
            not parent
            or not node.checkable
            or node.checked is None
            or node.klass != "android.widget.Switch"
            or node.package != CLOCK
        ):
            return verdict("uncertain", "无法关联保存记录或开关状态未知")
        parsed = times(node.description)
        if len(parsed) != 1 or parent.node_id in records:
            return verdict("uncertain", "时间或记录关联不唯一")
        records[parent.node_id] = (next(iter(parsed)), node.checked, node.description)
    matches = [r for r in records.values() if r[0] == req.expected_time]
    result["visible_match_count"] = len(matches)
    result["evidence"] = [f"{r[2]} [checked={str(r[1]).lower()}]" for r in matches]
    if not matches:
        return verdict("uncertain", "当前可见范围没有目标记录，不能推断全局不存在")
    expected_enabled = req.intent == "enable"
    if all(r[1] == expected_enabled for r in matches):
        return verdict("passed", "目标时间可见记录的开关状态符合要求；不证明全局唯一性")
    return verdict("failed", "目标时间的可见记录开关状态与要求不符")
