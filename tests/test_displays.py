import pytest

DISPLAYS = (
    'Displays:\nDisplay id 0: DisplayInfo{"Built-in Screen", displayId 0, '
    'real 320 x 640, type INTERNAL, uniqueId "local:123"}, isValid=true\n'
    'Display id 2: DisplayInfo{"scrcpy", displayId 2, real 320 x 640, '
    'type VIRTUAL, uniqueId "virtual:shell,scrcpy,0"}, isValid=true\n'
)


def test_binding_never_accepts_primary_display():
    from sidecar.displays import DisplayBinding

    with pytest.raises(ValueError):
        DisplayBinding(id=0, unique_id="local:123", boot_id="boot-a")


def test_display_parser_keeps_logical_identity_and_dimensions():
    from sidecar.displays import parse_displays

    assert parse_displays(DISPLAYS)[2] == {
        "name": "scrcpy",
        "unique_id": "virtual:shell,scrcpy,0",
        "width": 320,
        "height": 640,
    }


@pytest.mark.parametrize(
    "boot, listing",
    [
        ("boot-b", DISPLAYS),
        ("boot-a", DISPLAYS.split("Display id 2:")[0]),
        ("boot-a", DISPLAYS.replace("virtual:shell,scrcpy,0", "virtual:other")),
    ],
)
def test_lost_or_replaced_display_fails_before_any_input(monkeypatch, boot, listing):
    from sidecar import displays

    writes = []

    def shell(serial, args):
        if args[0] == "cat":
            return boot
        if args[:3] == ["cmd", "display", "get-displays"]:
            return listing
        writes.append(args)
        return ""

    monkeypatch.setattr(displays, "shell", shell)
    binding = displays.DisplayBinding(id=2, unique_id="virtual:shell,scrcpy,0", boot_id="boot-a")
    with pytest.raises(RuntimeError):
        displays.execute("emulator-test", binding, {"kind": "tap", "bounds": [10, 20, 30, 40]})
    assert writes == []


def test_touch_and_back_are_explicitly_routed_to_secondary_display(monkeypatch):
    from sidecar import displays

    calls = []
    monkeypatch.setattr(displays, "verify", lambda *args: {"width": 320, "height": 640})
    monkeypatch.setattr(displays, "shell", lambda serial, args: calls.append(args) or "")
    binding = displays.DisplayBinding(id=2, unique_id="virtual:shell,scrcpy,0", boot_id="boot-a")
    displays.execute("emulator-test", binding, {"kind": "tap", "bounds": [10, 20, 30, 40]})
    displays.execute("emulator-test", binding, {"kind": "back"})
    assert calls == [
        ["input", "-d", "2", "tap", "20", "30"],
        ["input", "-d", "2", "keyevent", "KEYCODE_BACK"],
    ]


def test_unscoped_text_input_is_rejected_without_writes(monkeypatch):
    from sidecar import displays

    monkeypatch.setattr(displays, "verify", lambda *args: {})
    calls = []
    monkeypatch.setattr(displays, "shell", lambda *args: calls.append(args))
    binding = displays.DisplayBinding(id=2, unique_id="virtual:shell,scrcpy,0", boot_id="boot-a")
    with pytest.raises(ValueError):
        displays.execute("emulator-test", binding, {"kind": "type", "text": "secret"})
    assert calls == []


def test_hierarchy_excludes_main_screen_and_missing_display_tags():
    from sidecar.displays import filter_hierarchy

    xml = """<hierarchy><node display-id="0" text="private video"/>
    <node text="unknown"/><node display-id="2" package="clock">
    <node display-id="2" text="Alarm"/><node display-id="0" text="private"/>
    </node></hierarchy>"""
    result = filter_hierarchy(xml, 2)
    assert "Alarm" in result
    assert "private" not in result
    assert "unknown" not in result
    with pytest.raises(RuntimeError):
        filter_hierarchy(xml, 3)


def test_app_focus_is_resolved_per_display():
    from sidecar.displays import foreground

    dump = """Display #2 (activities from top to bottom):
    topResumedActivity=ActivityRecord{ab u0 com.clock/.Clock t35}
Display #0 (activities from top to bottom):
    topResumedActivity=ActivityRecord{cd u0 com.youtube/.Main t33}
"""
    assert foreground(dump, 2) == ("com.clock", ".Clock")
    with pytest.raises(RuntimeError):
        foreground(dump, 4)


def test_hidden_cli_flag_is_explicit_and_settings_reject_demo_display():
    from sidecar.cli import parser
    from sidecar.displays import DisplayBinding
    from sidecar.jobs import Settings

    assert parser().parse_args(["serve", "--android", "--hidden-display"]).hidden_display
    with pytest.raises(ValueError):
        Settings(display=DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-a"))


def test_launcher_never_interrupts_the_app_the_human_is_using(monkeypatch):
    from sidecar import displays
    from sidecar.contracts import DeviceSetupError

    monkeypatch.setattr(displays, "verify", lambda *args: {})
    # 该 App 就是主屏前台：绝不打断用户，也不清理。
    monkeypatch.setattr(displays, "current", lambda serial, number: ("com.clock", ".Clock"))
    calls = []
    monkeypatch.setattr(displays, "shell", lambda *args: calls.append(args) or "")
    binding = displays.DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-a")
    with pytest.raises(DeviceSetupError, match="主屏"):
        displays.launch("emulator-test", binding, "com.clock")
    assert calls == []


def test_launcher_stops_a_background_task_before_starting(monkeypatch):
    from sidecar import displays

    monkeypatch.setattr(displays, "verify", lambda *args: {})
    # 主屏前台是别的 App，但主屏仍留着该 App 的后台 task。
    monkeypatch.setattr(
        displays,
        "current",
        lambda serial, number: ("com.other", ".Main") if number == 0 else ("com.clock", ".Clock"),
    )
    inventory = (
        "RootTask id=1 bounds=[0,0][320,640] displayId=0 userId=0\n"
        "  taskId=57: com.clock/.Clock bounds=[0,0][320,640]\n"
    )
    calls = []
    monkeypatch.setattr(displays, "shell", lambda *args: calls.append(args) or inventory)
    binding = displays.DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-a")
    displays.launch("emulator-test", binding, "com.clock")
    # 先 force-stop 清掉用户的后台任务（副屏已有该 App，因此不需要 am start）。
    assert any("force-stop" in str(entry) for entry in calls)
    assert all("start" not in str(entry) for entry in calls)


def test_close_app_is_quiet_when_app_is_only_on_another_display(monkeypatch):
    from sidecar import displays

    monkeypatch.setattr(displays, "verify", lambda *args: {})
    # notion.id 只在主屏（displayId=0）；绑定的副屏没有任何它的 root task。
    inventory = (
        "RootTask id=1 bounds=[0,0][320,640] displayId=0 userId=0\n"
        "  taskId=57: notion.id/notion.local.id.MainActivity\n"
    )
    calls = []
    monkeypatch.setattr(displays, "shell", lambda *args: calls.append(args) or inventory)
    binding = displays.DisplayBinding(id=95, unique_id="virtual:test", boot_id="boot-a")
    displays.close_app("emulator-test", binding, "notion.id")  # 不应抛错
    # 只读了一次清单，没有发出任何 stack remove。
    assert len(calls) == 1
    assert all("remove" not in str(entry) for entry in calls)


def test_launcher_reuses_app_already_resumed_on_hidden_screen(monkeypatch):
    from sidecar import displays

    monkeypatch.setattr(displays, "verify", lambda *args: {})
    monkeypatch.setattr(
        displays,
        "current",
        lambda serial, number: ("com.clock" if number == 2 else "com.youtube", ".Main"),
    )
    # 该 App 只存在于绑定的副屏（displayId=2），主屏没有它的任务。
    inventory = (
        "RootTask id=1 bounds=[0,0][320,640] displayId=2 userId=0\n"
        "  taskId=57: com.clock/.Clock bounds=[0,0][320,640]\n"
    )
    calls = []
    monkeypatch.setattr(displays, "shell", lambda *args: calls.append(args) or inventory)
    binding = displays.DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-a")
    displays.launch("emulator-test", binding, "com.clock")
    assert all("start" not in str(entry) for entry in calls)


def test_managed_display_is_hidden_and_cleaned_up_on_caller_failure(monkeypatch):
    import signal

    from sidecar import displays

    class Process:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def send_signal(self, value):
            assert value == signal.SIGINT
            self.stopped = True

        def wait(self, **kwargs):
            return 0

    process = Process()
    calls = []

    def spawn(args, **kwargs):
        calls.append(args)
        return process

    monkeypatch.setattr(displays.shutil, "which", lambda binary: "/bin/" + binary)
    monkeypatch.setattr(displays, "shell", lambda *args: "35")
    monkeypatch.setattr(displays.subprocess, "Popen", spawn)
    snapshots = iter(
        [{0: {"width": 320, "height": 640}}, {2: {"name": "scrcpy", "unique_id": "virtual:test"}}]
    )
    monkeypatch.setattr(displays, "listing", lambda serial: next(snapshots))
    with pytest.raises(ValueError, match="caller stopped"):
        with displays.hidden_display("emulator-test") as binding:
            assert binding.id == 2
            assert "--no-window" in calls[0] and "--no-audio" in calls[0]
            raise ValueError("caller stopped")
    assert process.stopped


def test_offline_cancel_releases_queue_after_hidden_server_dies(tmp_path, monkeypatch, capsys):
    from sidecar.cli import main
    from sidecar.jobs import JobQueue

    q = JobQueue(tmp_path)
    job = q.submit("task", items=["item"])
    q.claim()  # Worker died before creating a Harness checkpoint.
    monkeypatch.setattr("sys.argv", ["sidecar", "--home", str(tmp_path), "cancel", job["id"]])
    main()
    assert q.raw(job["id"])["status"] == "cancelled"
    assert "cancel" in capsys.readouterr().out


@pytest.mark.parametrize(
    "response,stage",
    [
        (b'{"ok":false}', "unknown"),
        (b'{"ok":true,"verified":false}', "verification"),
        (b'{"ok":false,"stage":"write_rejected"}', "write_rejected"),
        (b'{"ok":false,"stage":"target"}', "target"),
        (b'{"ok":false,"stage":"private-secret"}', "unknown"),
        (b"private-secret", "protocol"),
        (b"[]", "protocol"),
    ],
)
def test_text_bridge_rejects_unverified_result_without_replay(monkeypatch, response, stage):
    from sidecar import displays

    monkeypatch.setattr(displays, "verify", lambda *args: {})
    writes = []

    def run(args, **kwargs):
        if "push" in args:
            return b""
        writes.append(kwargs.get("input"))
        return response

    monkeypatch.setattr(displays.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(displays, "run", run)
    binding = displays.DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-a")
    with pytest.raises(RuntimeError) as caught:
        displays.set_text(
            "emulator-test",
            binding,
            "com.example.todo",
            {
                "text": "中文",
                "selector": {
                    "resource_id": "title",
                    "class_name": "android.widget.EditText",
                    "bounds": [10, 20, 190, 70],
                    "old_text": "",
                    "description": "标题",
                },
            },
        )
    assert len(writes) == 1
    assert getattr(caught.value, "stage", None) == stage
    assert "private-secret" not in str(caught.value)


def test_close_app_removes_only_its_secondary_stack(monkeypatch):
    from sidecar import displays

    snapshot = (
        "RootTask id=10 bounds=[0,0][320,640] displayId=0 userId=0\n"
        "  taskId=10: com.youtube/.Main topActivity=ComponentInfo{com.youtube/.Main}\n"
        "RootTask id=20 bounds=[0,0][320,640] displayId=2 userId=0\n"
        "  taskId=20: com.clock/.Main topActivity=ComponentInfo{com.clock/.Main}\n"
        "RootTask id=30 bounds=[0,0][320,640] displayId=2 userId=0\n"
        "  taskId=30: com.reader/.Main topActivity=ComponentInfo{com.reader/.Main}\n"
    )
    writes = []
    closing = [True]

    def shell(serial, args):
        if args == ["am", "stack", "list"]:
            if writes and closing:
                closing.pop()
                return snapshot
            return (
                snapshot
                if not writes
                else (
                    "RootTask id=10 bounds=[0,0][320,640] displayId=0 userId=0\n"
                    "  taskId=10: com.youtube/.Main topActivity=ComponentInfo{com.youtube/.Main}\n"
                    "RootTask id=30 bounds=[0,0][320,640] displayId=2 userId=0\n"
                    "  taskId=30: com.reader/.Main topActivity=ComponentInfo{com.reader/.Main}\n"
                )
            )
        writes.append(args)
        return ""

    monkeypatch.setattr(displays, "verify", lambda *args: {})
    monkeypatch.setattr(displays, "shell", shell)
    binding = displays.DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-a")
    displays.close_app("emulator-test", binding, "com.clock")
    assert writes == [["am", "stack", "remove", "20"]]


def test_close_app_refuses_stack_shared_with_another_app(monkeypatch):
    from sidecar import displays

    monkeypatch.setattr(displays, "verify", lambda *args: {})
    reads = (
        "RootTask id=20 bounds=[0,0][320,640] displayId=2 userId=0\n"
        "  taskId=20: com.clock/.Main topActivity=ComponentInfo{com.other/.Popup}\n"
    )
    writes = []
    monkeypatch.setattr(
        displays,
        "shell",
        lambda serial, args: reads if args == ["am", "stack", "list"] else writes.append(args),
    )
    binding = displays.DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-a")
    with pytest.raises(RuntimeError):
        displays.close_app("emulator-test", binding, "com.clock")
    assert not writes


def test_close_app_refuses_when_same_package_is_on_main_display(monkeypatch):
    from sidecar import displays

    monkeypatch.setattr(displays, "verify", lambda *args: {})
    snapshot = (
        "RootTask id=10 bounds=[0,0][320,640] displayId=0 userId=0\n"
        "  taskId=10: com.clock/.Main\n"
        "RootTask id=20 bounds=[0,0][320,640] displayId=2 userId=0\n"
        "  taskId=20: com.clock/.Main\n"
    )
    writes = []
    monkeypatch.setattr(
        displays,
        "shell",
        lambda serial, args: snapshot if args == ["am", "stack", "list"] else writes.append(args),
    )
    binding = displays.DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-a")
    with pytest.raises(RuntimeError, match="another display"):
        displays.close_app("emulator-test", binding, "com.clock")
    assert not writes


def test_close_app_does_not_follow_a_task_moved_to_main_display(monkeypatch):
    from sidecar import displays

    monkeypatch.setattr(displays, "verify", lambda *args: {})
    snapshot = (
        "RootTask id=20 bounds=[0,0][320,640] displayId=2 userId=0\n  taskId=20: com.clock/.Main\n"
    )
    reads = iter([snapshot, snapshot.replace("displayId=2", "displayId=0")])
    writes = []
    monkeypatch.setattr(
        displays,
        "shell",
        lambda serial, args: (
            next(reads) if args == ["am", "stack", "list"] else writes.append(args)
        ),
    )
    binding = displays.DisplayBinding(id=2, unique_id="virtual:test", boot_id="boot-a")
    with pytest.raises(RuntimeError, match="moved"):
        displays.close_app("emulator-test", binding, "com.clock")
    assert not writes
