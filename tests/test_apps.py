import pytest

from sidecar.apps import route_app
from sidecar.jobs import Settings


def config():
    return Settings(
        mode="android",
        model="test",
        allowed_packages=["com.video", "com.music"] + [f"com.app{i}" for i in range(30)],
        app_aliases={"com.video": ["YouTube"], "com.music": ["YouTube Music"]},
    )


def test_longer_app_name_wins_only_for_overlapping_mention():
    settings = config()
    assert route_app("在 YouTube Music 查找音乐", settings) == "com.music"
    assert route_app("在 YouTube 查找视频", settings) == "com.video"
    with pytest.raises(ValueError, match="多个 App"):
        route_app("在 YouTube Music 和 YouTube 查找", settings)


def test_large_allow_list_round_trip():
    settings = config()
    assert len(Settings.model_validate_json(settings.model_dump_json()).allowed_packages) == 32


def test_missing_app_with_multiple_allowed_is_rejected():
    with pytest.raises(ValueError, match="写明目标 App"):
        route_app("随便帮我处理一下", config())


def test_single_allowed_app_needs_no_mention():
    settings = Settings(mode="android", model="test", allowed_packages=["com.only"])
    assert route_app("随便帮我处理一下", settings) == "com.only"


def test_detected_app_outside_allow_list_is_rejected():
    # 内置别名里有 Notion，但 config() 的允许列表不含它。
    with pytest.raises(ValueError, match="尚未加入允许列表"):
        route_app("把这个加到 Notion", config())


def test_custom_and_builtin_aliases_both_resolve():
    settings = Settings(
        mode="android",
        model="test",
        allowed_packages=["com.tencent.weread", "com.other"],
        app_aliases={"com.tencent.weread": ["微信阅读器"]},
    )
    assert route_app("用微信阅读器打开", settings) == "com.tencent.weread"
    assert route_app("用微信读书打开", settings) == "com.tencent.weread"


def test_alias_bound_to_app_outside_allow_list_is_rejected():
    with pytest.raises(ValueError, match="别名"):
        Settings(
            mode="android",
            model="test",
            allowed_packages=["com.a"],
            app_aliases={"com.b": ["B"]},
        )


@pytest.mark.parametrize(
    "goal",
    [
        "不要打开时钟，创建一条待办",
        "别用时钟；创建一条待办",
        "时钟不要打开。创建一条待办",
        "Don't open Clock, create a todo",
    ],
)
def test_negated_app_does_not_block_single_default(goal):
    settings = Settings(mode="android", model="test", allowed_packages=["com.microsoft.todos"])
    assert route_app(goal, settings) == "com.microsoft.todos"


@pytest.mark.parametrize(
    "goal",
    [
        "不要打开时钟，在微软待办创建一条待办",
        "在微软待办创建一条待办，不要打开时钟",
        "不要打开时钟和 Notion，在微软待办创建一条待办",
    ],
)
def test_explicit_positive_app_wins_over_negated_apps(goal):
    settings = Settings(
        mode="android",
        model="test",
        allowed_packages=[
            "com.microsoft.todos",
            "com.google.android.deskclock",
            "notion.id",
        ],
    )
    assert route_app(goal, settings) == "com.microsoft.todos"


@pytest.mark.parametrize(
    "allowed, goal",
    [
        (["com.microsoft.todos", "com.google.android.deskclock"], "不要打开时钟，创建一条待办"),
        (["com.google.android.deskclock"], "不要打开时钟，创建一条待办"),
        (["com.microsoft.todos"], "不要打开时钟"),
        (["com.microsoft.todos"], "不要打开时钟而是在微软待办创建任务"),
        (["com.google.android.deskclock"], "不要打开时钟，打开时钟"),
        (["com.google.android.deskclock"], "不是不要打开时钟"),
    ],
)
def test_negation_never_falls_back_to_excluded_or_ambiguous_target(allowed, goal):
    settings = Settings(mode="android", model="test", allowed_packages=allowed)
    with pytest.raises(ValueError, match="写明目标 App"):
        route_app(goal, settings)


def test_negation_keeps_longest_alias_ownership():
    assert route_app("不要用 YouTube Music，在 YouTube 查找视频", config()) == "com.video"


def test_negation_does_not_bypass_allow_list_for_positive_app():
    settings = Settings(mode="android", model="test", allowed_packages=["com.microsoft.todos"])
    with pytest.raises(ValueError, match="尚未加入允许列表"):
        route_app("不要打开时钟，在 Notion 创建任务", settings)


def test_chat_submission_queues_only_positive_target(tmp_path):
    from sidecar.jobs import JobQueue
    from sidecar.openai_compat import submit_goal

    queue = JobQueue(tmp_path)
    queue.configure(
        Settings(
            mode="android",
            model="test",
            allowed_packages=[
                "com.microsoft.todos",
                "com.google.android.deskclock",
            ],
        )
    )
    job = submit_goal(queue, "不要打开时钟，在微软待办创建一条待办", "negated-app")
    assert job["request"]["app"] == "com.microsoft.todos"
    with pytest.raises(ValueError, match="写明目标 App"):
        submit_goal(queue, "不要打开时钟，创建一条待办", "ambiguous-app")
    assert [item["id"] for item in queue.list()] == [job["id"]]
