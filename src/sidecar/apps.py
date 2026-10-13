"""Resolve explicit app names to the configured allow-list, before creating a task."""

import re

# Convenience names, not a support allow-list. Custom apps can supply their own aliases.
NAMES = {
    "notion.id": ["Notion"],
    "com.tencent.weread": ["微信读书", "微信阅读", "WeRead"],
    "com.google.android.deskclock": ["时钟", "闹钟", "Clock"],
    "com.microsoft.todos": ["Microsoft To Do", "微软待办"],
}

# Conservative clause handling, not a natural-language parser. Keep ASCII dots
# inside package names; clauses without an explicit separator remain ambiguous.
CLAUSE = re.compile(r"[^，,。；;！？!?\n]+")
NEGATION = re.compile(
    r"不要|不用|无需|不必|禁止|不想|不能|不可以|不是|不使用|不打开|不在|"
    r"别(?:再|去|用|打开|启动|操作|在|把|给|碰)|"
    r"\b(?:not|never|without|don['’]t|do\s+not)\b",
    re.I,
)


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
    # 长名占有重叠区间；被更长名称覆盖的短名不计入。
    mentions = [
        (start, end, package)
        for start, end, package in mentions
        if not any(
            left <= start and end <= right and right - left > end - start
            for left, right, _ in mentions
        )
    ]
    by_name, excluded = set(), set()
    positive_clause = False
    for clause in CLAUSE.finditer(goal):
        packages = {
            package
            for start, end, package in mentions
            if clause.start() <= start and end <= clause.end()
        }
        if NEGATION.search(clause.group()):
            excluded.update(packages)
        elif clause.group().strip():
            positive_clause = True
            by_name.update(packages)
    clarification = "请在任务中写明目标 App 名称，或通过控制台选择目标 App"
    # Conflicting mentions cannot be resolved by assuming the last one wins.
    if by_name & excluded:
        raise ValueError(clarification)
    allowed = set(settings.allowed_packages)
    inside = by_name & allowed
    outside = by_name - allowed
    # 歧义只在允许列表内判定：白名单外的同名匹配不应让合法任务被误拒。
    if len(inside) > 1:
        raise ValueError("任务中出现多个 App，请拆成每次只操作一个 App 的任务")
    if inside:
        return inside.pop()
    if outside:
        raise ValueError(f"目标 App 尚未加入允许列表：{sorted(outside)[0]}")
    if len(settings.allowed_packages) > 1 or settings.package in excluded or not positive_clause:
        raise ValueError(clarification)
    return settings.package
