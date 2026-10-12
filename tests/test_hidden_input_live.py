"""Opt-in emulator acceptance: build/install android/input-fixture first.

SIDECAR_INPUT_TEST_SERIAL=emulator-5554 pytest -q tests/test_hidden_input_live.py
Uses a temporary hidden display; the primary fixture must be opened by the tester.
"""

import os

import pytest

SERIAL = os.environ.get("SIDECAR_INPUT_TEST_SERIAL")
pytestmark = pytest.mark.skipif(not SERIAL, reason="requires prepared Android emulator fixture")


def test_secondary_unicode_input_preserves_primary_and_rejects_stale_target():
    from xml.etree import ElementTree as ET

    from sidecar import displays
    from sidecar.android_rpc import connect_device

    package = "dev.sidecar.inputfixture"

    def nodes():
        d = connect_device(SERIAL)
        try:
            return list(ET.fromstring(d.dump_hierarchy()).iter("node"))
        finally:
            d.stop_uiautomator()

    with displays.hidden_display(SERIAL) as binding:
        displays.shell(
            SERIAL,
            ["am", "start", "--display", str(binding.id), "-n", package + "/.SecondaryActivity"],
        )
        before = nodes()
        target = next(
            n
            for n in before
            if n.get("display-id") == str(binding.id)
            and n.get("class") == "android.widget.EditText"
        )
        primary = [
            n.attrib
            for n in before
            if n.get("display-id") == "0" and n.get("class") == "android.widget.EditText"
        ]
        assert primary and primary[0]["text"] == "MAIN stays here"
        import re

        selector = dict(
            resource_id=target.get("resource-id", ""),
            class_name=target.get("class"),
            bounds=[int(v) for v in re.findall(r"\d+", target.get("bounds"))],
            old_text=target.get("text", ""),
            description=target.get("content-desc", ""),
        )
        displays.set_text(
            SERIAL, binding, package, {"text": "副屏中文 hello", "selector": selector}
        )
        after = nodes()
        assert any(
            n.get("display-id") == str(binding.id) and n.get("text") == "副屏中文 hello"
            for n in after
        )
        assert [
            n.attrib
            for n in after
            if n.get("display-id") == "0" and n.get("class") == "android.widget.EditText"
        ] == primary
        # Identical resource IDs on the main display must never satisfy a stale selector.
        with pytest.raises(RuntimeError):
            displays.set_text(
                SERIAL, binding, package, {"text": "must not write", "selector": selector}
            )
        assert any(
            n.get("display-id") == "0" and n.get("text") == "MAIN stays here" for n in nodes()
        )

        # Exercise the actual adapter + subprocess RPC, not only the bridge function.
        from sidecar.android import AndroidDevice
        from sidecar.contracts import Action

        device = AndroidDevice(SERIAL, package, display=binding)
        observation = device.observe()
        target_id = next(
            k for k, value in observation.elements.items() if "副屏中文 hello" in value
        )
        action = Action(kind="type", target=target_id, text="后台输入完成 123", reason="隔离验收")
        assert device.assess(action, observation) == "allow"
        device.execute(action)
        assert "后台输入完成 123" in device.observe().text
        # The user types on the primary display while a secondary write is in flight.
        import concurrent.futures
        import time

        observation = device.observe()
        target_id = next(
            k for k, value in observation.elements.items() if "后台输入完成 123" in value
        )
        bounds = [int(v) for v in re.findall(r"\d+", primary[0]["bounds"])]
        displays.shell(
            SERIAL,
            [
                "input",
                "-d",
                "0",
                "tap",
                str((bounds[0] + bounds[2]) // 2),
                str((bounds[1] + bounds[3]) // 2),
            ],
        )
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(
                device.execute,
                Action(kind="type", target=target_id, text="并行写入成功", reason="隔离验收"),
            )
            time.sleep(0.15)
            displays.shell(SERIAL, ["input", "-d", "0", "keyevent", "KEYCODE_MOVE_END"])
            displays.shell(SERIAL, ["input", "-d", "0", "text", "_user"])
            future.result()
        after = nodes()
        assert any(
            n.get("display-id") == "0"
            and n.get("text") == "MAIN stays here_user"
            and n.get("focused") == "true"
            for n in after
        )
        assert any(
            n.get("display-id") == str(binding.id) and n.get("text") == "并行写入成功"
            for n in after
        )
