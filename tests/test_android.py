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


def test_android_target_stays_valid_when_unrelated_nodes_are_inserted():
    d, t = device()
    before = d.observe()
    target = next(k for k, v in before.elements.items() if "保存" in v)
    t.xml = XML.replace("<hierarchy>", '<hierarchy><node text="status bar"/>')
    assert d.is_current(Action(kind="tap", target=target, reason="保存"), before, d.observe())


@pytest.mark.parametrize("attribute", ["checked", "selected"])
def test_android_selection_state_is_visible_and_invalidates_old_action(attribute):
    import json

    d, t = device()
    t.xml = XML.replace('text="保存"', f'text="保存" {attribute}="false"')
    before = d.observe()
    target = next(k for k, v in before.elements.items() if "保存" in v)
    assert json.loads(before.elements[target])[attribute] is False
    t.xml = t.xml.replace(f'{attribute}="false"', f'{attribute}="true"')
    after = d.observe()
    assert json.loads(after.elements[target])[attribute] is True
    assert not d.is_current(Action(kind="tap", target=target, reason="保存"), before, after)


def test_android_text_distinguishes_selected_radio_options():
    d, t = device()
    t.xml = """<hierarchy>
    <node package="com.example.todo" text="AM" checkable="true" checked="false"/>
    <node package="com.example.todo" text="PM" checkable="true" checked="true"/>
    </hierarchy>"""
    text = d.observe().text
    assert "AM [checked=false]" in text
    assert "PM [checked=true]" in text


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


def test_android_launch_is_restricted_to_configured_package():
    d, t = device()
    d.launch()
    assert t.actions == [("launch", {"package": "com.example.todo"})]


@pytest.mark.parametrize("label", ["发送消息", "转账", "购买", "Send message", "Transfer money"])
def test_android_blocks_high_risk_action_screens(label):
    d, t = device()
    t.xml = XML.replace('text="保存"', f'text="{label}"')
    obs = d.observe()
    assert obs.requires_human
    assert obs.image is None


def test_delete_button_does_not_block_reading_or_separate_safe_target():
    d, t = device()
    t.xml = XML.replace('text="保存"', 'text="Delete"')
    obs = d.observe()
    assert not obs.requires_human
    assert obs.image is not None
    safe = next(k for k, v in obs.elements.items() if "标题" in v)
    unsafe = next(k for k, v in obs.elements.items() if "Delete" in v)
    assert d.assess(Action(kind="tap", target=safe, reason="打开独立字段"), obs) == "allow"
    assert d.assess(Action(kind="tap", target=unsafe, reason="删除"), obs) == "handoff"
    assert d.assess(Action(kind="tap", x=0.5, y=0.2, reason="坐标"), obs) == "handoff"
    assert d.assess(Action(kind="swipe", direction="left", reason="滑动"), obs) == "handoff"


def test_delete_child_blocks_parent_click_and_unknown_delete_bounds_block_screen():
    d, t = device()
    t.xml = XML.replace('text="保存"', 'text="Delete"').replace(
        "</hierarchy>",
        '<node package="com.example.todo" class="Button" text="parent" '
        'clickable="true" bounds="[0,0][200,200]"/></hierarchy>',
    )
    obs = d.observe()
    parent = next(k for k, v in obs.elements.items() if "parent" in v)
    assert d.assess(Action(kind="tap", target=parent, reason="parent"), obs) == "handoff"
    t.xml = '<hierarchy><node text="Delete"/></hierarchy>'
    assert d.observe().requires_human


@pytest.mark.parametrize("op, expected_calls", [("observe", 2), ("execute", 1), ("launch", 1)])
def test_transport_recovers_only_read_operations(monkeypatch, op, expected_calls):
    from types import SimpleNamespace

    from sidecar.android import Transport

    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs["input"])
        return SimpleNamespace(returncode=1 if len(calls) == 1 else 0, stdout='{"ok": true}')

    monkeypatch.setattr("sidecar.android.subprocess.run", run)
    transport = Transport("emulator-test")
    if op == "observe":
        assert transport.call(op) == {"ok": True}
    else:
        with pytest.raises(RuntimeError):
            transport.call(op)
    assert len(calls) == expected_calls


def test_transport_read_recovery_is_bounded(monkeypatch):
    import subprocess

    from sidecar.android import Transport

    calls = []

    def timeout(*args, **kwargs):
        calls.append(1)
        raise subprocess.TimeoutExpired("device", 30)

    monkeypatch.setattr("sidecar.android.subprocess.run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        Transport("emulator-test").call("observe")
    assert len(calls) == 2


def test_hidden_device_rejects_unscoped_observation_and_input():
    from sidecar.android import AndroidDevice
    from sidecar.displays import DisplayBinding

    binding = DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-a")
    transport = Transport()
    d = AndroidDevice("emulator-test", "com.example.todo", transport=transport, display=binding)
    with pytest.raises(RuntimeError):
        d.observe()
    assert transport.actions == []


def test_display_binding_is_part_of_task_device_identity():
    from sidecar.android import AndroidDevice
    from sidecar.displays import DisplayBinding

    a = AndroidDevice("emulator-test", "com.example.todo")
    b = AndroidDevice(
        "emulator-test",
        "com.example.todo",
        display=DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-a"),
    )
    c = AndroidDevice(
        "emulator-test",
        "com.example.todo",
        display=DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-b"),
    )
    assert len({a.device_id, b.device_id, c.device_id}) == 3


def test_hidden_typing_requires_explicit_target_and_carries_node_identity():
    from sidecar.android import AndroidDevice
    from sidecar.displays import DisplayBinding

    binding = DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-a")

    class ScopedTransport(Transport):
        def call(self, op, payload=None):
            result = super().call(op, payload)
            if op == "observe":
                result["display"] = binding.model_dump()
                result["xml"] = self.xml.replace("<node ", '<node display-id="2" ')
            return result

    t = ScopedTransport()
    d = AndroidDevice("emulator-test", "com.example.todo", transport=t, display=binding)
    obs = d.observe()
    target = next(k for k, v in obs.elements.items() if "标题" in v)
    action = Action(kind="type", target=target, text="副屏中文 hello", reason="填写")
    assert d.assess(Action(kind="type", text="误入焦点", reason="test"), obs) == "handoff"
    assert d.assess(action, obs) == "allow"
    d.execute(action)
    payload = t.actions[-1][1]
    assert payload["selector"] == {
        "resource_id": "title",
        "class_name": "android.widget.EditText",
        "bounds": [10, 20, 190, 70],
        "old_text": "",
        "description": "标题",
    }
    assert payload["text"] == "副屏中文 hello"


def test_target_app_outside_allow_list_is_rejected():
    from sidecar.android import AndroidDevice

    with pytest.raises(ValueError, match="allowed"):
        AndroidDevice("emulator-test", "com.other", allowed=["com.a", "com.b"])


def test_foreground_outside_allow_list_requires_human():
    d, t = device()
    d.allowed = ["com.example.todo", "com.tencent.weread"]
    t.package = "com.evil.app"
    obs = d.observe()
    assert obs.requires_human
    assert obs.image is None


def test_allow_list_accepts_any_named_app_as_task_target():
    from sidecar.android import AndroidDevice

    transport = Transport()
    transport.package = "com.tencent.weread"
    transport.xml = XML.replace("com.example.todo", "com.tencent.weread")
    d = AndroidDevice(
        "emulator-test",
        "com.tencent.weread",
        transport=transport,
        allowed=["com.example.todo", "com.tencent.weread"],
    )
    assert not d.observe().requires_human
    assert d.allowed == ["com.example.todo", "com.tencent.weread"]
