"""Google Clock 7.5 deterministic gate: alarm records, page structure, switch state."""

import json
import re
from functools import lru_cache
from pathlib import Path

from sidecar.app_actions.base import AppAction, program_verdict
from sidecar.contracts import ClockAlarmRequirement
from sidecar.verifiers import times

CLOCK = "com.google.android.deskclock"


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


@lru_cache(maxsize=1)
def clock_page_paths():
    # Structural allowlist from reviewed saved-list/expanded-card fixtures; no user text.
    return set(json.loads((Path(__file__).with_name("clock-page-paths.json")).read_text()))


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


class ClockAction(AppAction):
    package = CLOCK
    kinds = frozenset({"clock_alarm_enabled"})

    def infer_requirement(self, goal):
        candidates = times(goal)
        return ClockAlarmRequirement(
            intent=clock_intent(goal),
            expected_time=next(iter(candidates)) if len(candidates) == 1 else None,
        )

    def verify(self, task, obs, context):
        req = task.requirement

        def result(status, reason, evidence=None, **details):
            return program_verdict(
                status,
                reason,
                observation_id=obs.id,
                verifier_version="clock.v1",
                evidence=evidence,
                details=details,
            )

        base_details = {
            "visible_match_count": 0,
            "list_complete": None,
            "truncated": obs.ui_truncated,
        }
        if req.intent == "unknown" or clock_intent(task.goal) != req.intent:
            return result("uncertain", "任务意图未知或与结构化要求冲突，请澄清", **base_details)
        if not req.expected_time or (times(task.goal) and times(task.goal) != {req.expected_time}):
            return result("uncertain", "目标时间缺失或与用户目标冲突，请澄清", **base_details)
        if obs.requires_human or obs.package != CLOCK or obs.app_version != "7.5":
            return result("uncertain", "需要人工处理，或 App/版本不在已验证范围", **base_details)
        if obs.ui_truncated or not obs.ui:
            return result("uncertain", "节点缺失或截断", **base_details)
        if any(
            n.package not in {"", CLOCK}
            or n.resource_id.endswith(("material_timepicker_ok_button", "label_input_field"))
            for n in obs.ui
        ):
            return result("uncertain", "检测到弹窗或其他包的节点", **base_details)
        if not recognized_clock_page(obs.ui):
            return result("uncertain", "页面结构、可见性或窗口归属不符合已验证样本", **base_details)
        by_id = {n.node_id: n for n in obs.ui}
        records = {}
        for node in obs.ui:
            if node.resource_id != CLOCK + ":id/onoff":
                continue
            parent = by_id.get(node.parent_id)
            seen = set()
            while parent and parent.klass != "androidx.cardview.widget.CardView":
                if parent.node_id in seen:
                    return result("uncertain", "节点父子关系异常", **base_details)
                seen.add(parent.node_id)
                parent = by_id.get(parent.parent_id)
            if (
                not parent
                or not node.checkable
                or node.checked is None
                or node.klass != "android.widget.Switch"
                or node.package != CLOCK
            ):
                return result("uncertain", "无法关联保存记录或开关状态未知", **base_details)
            parsed = times(node.description)
            if len(parsed) != 1 or parent.node_id in records:
                return result("uncertain", "时间或记录关联不唯一", **base_details)
            records[parent.node_id] = (next(iter(parsed)), node.checked, node.description)
        matches = [r for r in records.values() if r[0] == req.expected_time]
        evidence = [f"{r[2]} [checked={str(r[1]).lower()}]" for r in matches]
        details = {**base_details, "visible_match_count": len(matches)}
        if not matches:
            return result(
                "uncertain",
                "当前可见范围没有目标记录，不能推断全局不存在",
                evidence=[],
                **details,
            )
        expected_enabled = req.intent == "enable"
        if all(r[1] == expected_enabled for r in matches):
            return result(
                "passed",
                "目标时间可见记录的开关状态符合要求；不证明全局唯一性",
                evidence=evidence,
                **details,
            )
        return result(
            "failed",
            "目标时间的可见记录开关状态与要求不符",
            evidence=evidence,
            **details,
        )
