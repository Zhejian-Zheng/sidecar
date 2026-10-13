"""Resolve explicit app names to the configured allow-list, before creating a task."""

import re

# Convenience names, not a support allow-list. Custom apps can supply their own aliases.
NAMES = {
    "notion.id": ["Notion"],
    "com.tencent.weread": ["微信读书", "微信阅读", "WeRead"],
    "com.google.android.deskclock": ["时钟", "闹钟", "Clock"],
    "com.microsoft.todos": ["Microsoft To Do", "微软待办"],
}


def route_app(goal, settings):
    aliases = {package: list(names) for package, names in NAMES.items()}
    for package in settings.allowed_packages:
        aliases.setdefault(package, []).append(package)
    for package, names in settings.app_aliases.items():
        aliases.setdefault(package, []).extend(names)
    mentions = []
    for package, names in aliases.items():
        for name in names:
            pattern = re.escape(name)
            if name.isascii():
                pattern = r"(?<![a-z0-9_.])" + pattern + r"(?![a-z0-9_.])"
            for match in re.finditer(pattern, goal, re.I):
                mentions.append((match.start(), match.end(), package))
    # A longer name owns its span; separate mentions still remain ambiguous.
    matches = {
        package
        for start, end, package in mentions
        if not any(
            left <= start and end <= right and right - left > end - start
            for left, right, _ in mentions
        )
    }
    if len(matches) > 1:
        raise ValueError("任务中出现多个 App，请拆成每次只操作一个 App 的任务")
    if not matches:
        if len(settings.allowed_packages) > 1:
            raise ValueError("请在任务中写明目标 App 名称，或通过控制台选择目标 App")
        return settings.package
    target = matches.pop()
    if target not in settings.allowed_packages:
        raise ValueError(f"目标 App 尚未加入允许列表：{target}")
    return target
