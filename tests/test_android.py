import base64

import pytest

from sidecar.contracts import Action

XML = """<hierarchy><node package="com.example.todo" class="android.widget.EditText"
resource-id="title" text="" content-desc="标题" bounds="[10,20][190,70]"
clickable="true" enabled="true" focused="true" password="false"/>
<node package="com.example.todo" class="android.widget.Button" resource-id="save"
text="保存" bounds="[10,80][190,120]" clickable="true" enabled="true"/></hierarchy>"""


class Transport:
    def __init__(self):
        self.xml = XML
        self.package = "com.example.todo"
        self.actions = []

    def call(self, op, payload=None):
        if op == "observe":
            return dict(
                xml=self.xml,
                package=self.package,
                activity="Main",
                image=base64.b64encode(b"fixture-png").decode(),
                width=200,
                height=400,
            )
        self.actions.append((op, payload))
        return {}


def device():
    from sidecar.android import AndroidDevice

    transport = Transport()
    return AndroidDevice("emulator-test", "com.example.todo", transport=transport), transport


def test_android_observation_and_unicode_target_input():
    d, t = device()
    obs = d.observe()
    assert obs.image.startswith("data:image/png;base64,")
    target = next(k for k, v in obs.elements.items() if "标题" in v)
    action = Action(kind="type", target=target, text="准备面试", reason="填写")
    assert d.assess(action, obs) == "allow"
    d.execute(action)
    assert t.actions[-1][1]["text"] == "准备面试"
    assert t.actions[-1][1]["bounds"] == [10, 20, 190, 70]


def test_android_blocks_wrong_app_and_password_screens():
    d, t = device()
    t.package = "com.other.app"
    assert d.observe().requires_human
    t.package = "com.example.todo"
    t.xml = XML.replace('password="false"', 'password="true"')
    assert d.observe().requires_human


def test_android_target_freshness_ignores_image_but_not_changed_button():
    d, t = device()
    before = d.observe()
    target = next(k for k, v in before.elements.items() if "保存" in v)
    action = Action(kind="tap", target=target, reason="保存")
    after = before.model_copy(update={"image": "data:image/png;base64,changed"})
    assert d.is_current(action, before, after)
    t.xml = XML.replace('text="保存"', 'text="删除"')
    after = d.observe()
    assert not d.is_current(action, before, after)
    assert d.assess(action, after) == "handoff"


def test_android_focused_input_requires_editable_focus():
    d, t = device()
    action = Action(kind="type", text="中文", reason="填写")
    obs = d.observe()
    assert d.assess(action, obs) == "allow"
    t.xml = XML.replace('focused="true"', 'focused="false"')
    assert d.assess(action, d.observe()) == "handoff"


def test_android_coordinate_click_does_not_ignore_changed_image():
    d, _ = device()
    before = d.observe()
    after = before.model_copy(update={"image": "data:image/png;base64,changed"})
    action = Action(kind="tap", x=0.2, y=0.3, reason="点击")
    assert not d.is_current(action, before, after)


def test_android_locks_same_serial_across_workspaces(tmp_path):
    from sidecar.android import AndroidDevice

    a = AndroidDevice(
        "emulator-lock-test", "com.example.todo", transport=Transport(), lock_root=tmp_path
    )
    b = AndroidDevice(
        "emulator-lock-test", "com.example.todo", transport=Transport(), lock_root=tmp_path
    )
    with a.execution_lock():
        with pytest.raises(RuntimeError, match="busy"):
            with b.execution_lock():
                pass


@pytest.mark.parametrize(
    "tail",
    [
        '<node password="true" text="secret"/>',
        '<node text="确认支付" enabled="false"/>',
    ],
)
def test_sensitive_field_after_element_limit_still_blocks_screen(tail):
    d, t = device()
    ordinary = (
        '<node package="com.example.todo" class="Button" text="普通" '
        'clickable="true" bounds="[1,1][2,2]"/>'
    )
    t.xml = "<hierarchy>" + ordinary * 201 + tail + "</hierarchy>"
    obs = d.observe()
    assert obs.requires_human
    assert obs.image is None
    assert "secret" not in obs.text
    assert len(obs.elements) == 200
