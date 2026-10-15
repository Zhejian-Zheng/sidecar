"""App-agnostic verification tools: UI tree extraction and time normalization.

Per-app deterministic verifiers live in sidecar.app_actions; this module only holds
shared helpers with no app-specific knowledge.
"""

import re
from xml.etree import ElementTree as ET

from sidecar.contracts import UINode


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
