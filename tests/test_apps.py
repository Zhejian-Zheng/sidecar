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
