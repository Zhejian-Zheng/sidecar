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
