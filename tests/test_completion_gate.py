from pathlib import Path

import pytest

from sidecar.contracts import Action, Check, Observation, Task, Verification
from sidecar.harness import Harness


@pytest.mark.parametrize(
    "sample,completed",
    [
        ("target-enabled", True),
        ("list-disabled", False),
        ("time-picker", False),
        ("label-dialog", False),
    ],
)
def test_model_success_cannot_override_program(tmp_path, sample, completed):
    from sidecar.verifiers import extract_ui

    xml = (
        Path(__file__).parent / "fixtures/google_clock/7.5" / sample / "hierarchy.xml"
    ).read_text()
    nodes, truncated = extract_ui(xml)

    class Device:
        device_id = "test"

        def observe(self):
            return Observation(
                text="Clock",
                ui=nodes,
                ui_truncated=truncated,
                package="com.google.android.deskclock",
                app_version="7.5",
            )

    class Model:
        def decide(self, *args):
            return Action(kind="finish", reason="done")

        def verify(self, task, obs):
            return Verification(
                checks=[
                    Check(criterion=c, passed=True, evidence="model says yes")
                    for c in task.criteria
                ]
            )

    task = Task(
        goal="Enable alarm at 23:58",
        criteria=["alarm enabled"],
        requirement={"kind": "clock_alarm_enabled", "expected_time": "23:58"},
    )
    with Harness(tmp_path, Device(), Model(), max_decisions=3) as h:
        result = h.start(task)
        assert (result["status"] == "completed") == completed
        assert result["verification"]["conclusion"] == (
            "completed" if completed else "not_completed"
        )
        if not completed and result["status"] == "waiting_user":
            resumed = h.resume(result["task_id"], "try again")
            assert resumed["status"] != "completed"


@pytest.mark.parametrize(
    "change",
    [
        "missing_checked",
        "truncated",
        "wrong_version",
        "wrong_package",
        "conflict",
        "missing_target",
    ],
)
def test_incomplete_evidence_never_passes(change):
    from sidecar.verifiers import evaluate, extract_ui

    xml = (
        Path(__file__).parent / "fixtures/google_clock/7.5/target-enabled/hierarchy.xml"
    ).read_text()
    ui, _ = extract_ui(xml)
    obs = Observation(
        text="Clock", ui=ui, package="com.google.android.deskclock", app_version="7.5"
    )
    task = Task(
        goal="Enable 23:58 alarm",
        criteria=["enabled"],
        requirement={"kind": "clock_alarm_enabled", "expected_time": "23:58"},
    )
    if change == "missing_checked":
        for node in obs.ui:
            if node.resource_id.endswith("/onoff"):
                node.checked = None
    elif change == "truncated":
        obs.ui_truncated = True
    elif change == "wrong_version":
        obs.app_version = "unknown"
    elif change == "wrong_package":
        obs.package = "another.app"
    elif change == "conflict":
        task.goal = "Enable 08:00 alarm"
    else:
        task.requirement.expected_time = None
    assert evaluate(task, obs)["status"] == "uncertain"


def test_gmail_never_falls_back_to_model():
    from sidecar.verifiers import evaluate

    task = Task(goal="Save draft", criteria=["saved"], requirement={"kind": "gmail_draft_saved"})
    assert evaluate(task, Observation(text="Saved"))["status"] == "uncertain"


def test_time_boundaries_and_private_nodes():
    from sidecar.verifiers import extract_ui, times

    assert times("12:00 AM 12:00 PM") == {"00:00", "12:00"}
    nodes, truncated = extract_ui(
        '<hierarchy><node password="true" text="secret" '
        'content-desc="secret" checked="false"/></hierarchy>'
    )
    assert nodes[0].checked is None
    assert "secret" not in nodes[0].model_dump_json()
    assert not truncated


def test_requirement_survives_queue_storage(tmp_path):
    from sidecar.jobs import JobQueue, Settings

    q = JobQueue(tmp_path)
    q.configure(Settings(mode="android", package="com.google.android.deskclock", model="test"))
    job = q.submit(goal="Enable alarm at 23:58")
    assert q.get(job["id"])["request"]["requirement"]["expected_time"] == "23:58"


def test_verifier_exception_cannot_complete(tmp_path, monkeypatch):
    from sidecar import verifiers

    def broken(*args):
        raise RuntimeError("internal error")

    monkeypatch.setattr(verifiers, "evaluate", broken)

    class Device:
        device_id = "test"

        def observe(self):
            return Observation(text="saved")

    class Model:
        def decide(self, *args):
            return Action(kind="finish", reason="done")

        def verify(self, *args):
            return Verification(checks=[Check(criterion="saved", passed=True, evidence="saved")])

    task = Task(goal="alarm", criteria=["saved"], requirement={"kind": "clock_alarm_enabled"})
    with Harness(tmp_path, Device(), Model()) as h:
        result = h.start(task)
        assert result["status"] == "waiting_user"
        assert result["verification"]["conclusion"] == "not_completed"
        assert result["verification"]["checks"] == []
        assert result["verification"]["model_verification"]["checks"][0]["passed"]
        assert h.resume(result["task_id"], "continue")["status"] == "waiting_user"


def test_requirement_models_are_discriminated_and_round_trip():
    from sidecar.contracts import ClockAlarmRequirement, GmailDraftRequirement

    clock = Task(
        goal="alarm",
        criteria=["x"],
        requirement={"kind": "clock_alarm_enabled", "expected_time": "23:58"},
    ).requirement
    assert isinstance(clock, ClockAlarmRequirement)
    assert clock.expected_time == "23:58"

    gmail = Task(
        goal="draft",
        criteria=["x"],
        requirement={
            "kind": "gmail_draft_saved",
            "recipient": "a@b.c",
            "subject": "s",
            "body": "b",
        },
    ).requirement
    assert isinstance(gmail, GmailDraftRequirement)
    assert (gmail.recipient, gmail.subject, gmail.body) == ("a@b.c", "s", "b")

    dumped = Task(goal="draft", criteria=["x"], requirement=gmail).model_dump()["requirement"]
    assert dumped["kind"] == "gmail_draft_saved"
    assert dumped["subject"] == "s"


def test_unknown_requirement_kind_is_rejected():
    with pytest.raises(Exception):
        Task(goal="x", criteria=["y"], requirement={"kind": "unknown_kind"})


def test_chinese_meridiem_resolves_to_24h():
    from sidecar.verifiers import times

    assert times("晚上 11:58（23:58）") == {"23:58"}
    assert times("上午 11:58") == {"11:58"}
    assert times("下午 11:58") == {"23:58"}
    assert times("凌晨 12:30") == {"00:30"}
    assert times("中午 12:00") == {"12:00"}
    assert times("23:58") == {"23:58"}


def test_dual_notation_goal_is_not_a_conflict():
    from sidecar.verifiers import evaluate, extract_ui

    xml = (
        Path(__file__).parent / "fixtures/google_clock/7.5/target-enabled/hierarchy.xml"
    ).read_text()
    ui, truncated = extract_ui(xml)
    obs = Observation(
        text="Clock",
        ui=ui,
        ui_truncated=truncated,
        package="com.google.android.deskclock",
        app_version="7.5",
    )
    task = Task(
        goal="在 Google 时钟中设置一个晚上 11:58（23:58）的闹钟并开启",
        criteria=["enabled"],
        requirement={"kind": "clock_alarm_enabled", "expected_time": "23:58"},
    )
    assert evaluate(task, obs)["status"] == "passed"


@pytest.mark.parametrize(
    "goal,expected",
    [
        ("关闭 23:58 的闹钟", "disable"),
        ("删除 23:58 的闹钟", "unknown"),
        ("设置 23:58 提醒我", "enable"),
        ("开启 23:58 的闹钟", "enable"),
    ],
)
def test_clock_intent_registration(tmp_path, goal, expected):
    from sidecar.jobs import JobQueue, Settings

    q = JobQueue(tmp_path)
    q.configure(Settings(mode="android", package="com.google.android.deskclock", model="test"))
    req = q.submit(goal)["request"]["requirement"]
    assert req is not None
    assert req["intent"] == expected


@pytest.mark.parametrize(
    "goal,is_draft", [("搜索邮件", False), ("阅读最新邮件", False), ("保存一封草稿", True)]
)
def test_gmail_gate_is_scoped_to_drafts(tmp_path, goal, is_draft):
    from sidecar.jobs import JobQueue, Settings

    q = JobQueue(tmp_path)
    q.configure(Settings(mode="android", package="com.google.android.gm", model="test"))
    assert (q.submit(goal)["request"]["requirement"] is not None) == is_draft


@pytest.mark.parametrize(
    "goal,sample,status",
    [
        ("关闭 23:58 的闹钟", "target-enabled", "uncertain"),
        ("创建23:58或08:00的闹钟", "target-enabled", "uncertain"),
    ],
)
def test_explicit_requirement_cannot_override_conflicting_goal(goal, sample, status):
    from sidecar.verifiers import evaluate, extract_ui

    xml = (
        Path(__file__).parent / "fixtures/google_clock/7.5" / sample / "hierarchy.xml"
    ).read_text()
    nodes, _ = extract_ui(xml)
    obs = Observation(
        text="Clock", ui=nodes, package="com.google.android.deskclock", app_version="7.5"
    )
    task = Task(
        goal=goal,
        criteria=["done"],
        requirement={"kind": "clock_alarm_enabled", "expected_time": "23:58"},
    )
    assert evaluate(task, obs)["status"] == status


@pytest.mark.parametrize("mutation", ["extra_root", "nested_overlay", "hidden"])
def test_unrecognized_or_hidden_page_is_not_completion_evidence(mutation):
    from xml.etree import ElementTree as ET

    from sidecar.verifiers import evaluate, extract_ui

    root = ET.parse(
        Path(__file__).parent / "fixtures/google_clock/7.5/target-enabled/hierarchy.xml"
    ).getroot()
    if mutation == "hidden":
        next(n for n in root.iter("node") if n.get("resource-id", "").endswith("/onoff")).set(
            "visible-to-user", "false"
        )
    else:
        parent = root if mutation == "extra_root" else root[0]
        ET.SubElement(
            parent,
            "node",
            {
                "package": "com.google.android.deskclock",
                "class": "android.widget.FrameLayout",
                "resource-id": "new_overlay",
                "visible-to-user": "true",
            },
        )
    nodes, _ = extract_ui(ET.tostring(root, encoding="unicode"))
    obs = Observation(
        text="Clock", ui=nodes, package="com.google.android.deskclock", app_version="7.5"
    )
    task = Task(
        goal="Enable 23:58 alarm",
        criteria=["done"],
        requirement={"kind": "clock_alarm_enabled", "expected_time": "23:58"},
    )
    assert evaluate(task, obs)["status"] == "uncertain"


@pytest.mark.parametrize(
    "sample,expected", [("list-disabled", "passed"), ("target-enabled", "failed")]
)
def test_disable_checks_disabled_state(sample, expected):
    from sidecar.verifiers import evaluate, extract_ui, infer_requirement

    nodes, _ = extract_ui(
        (Path(__file__).parent / "fixtures/google_clock/7.5" / sample / "hierarchy.xml").read_text()
    )
    goal = "关闭 23:58 的闹钟"
    task = Task(
        goal=goal,
        criteria=["disabled"],
        requirement=infer_requirement("com.google.android.deskclock", goal),
    )
    obs = Observation(
        text="Clock", ui=nodes, package="com.google.android.deskclock", app_version="7.5"
    )
    assert evaluate(task, obs)["status"] == expected


def test_direct_harness_start_cannot_omit_clock_gate(tmp_path):
    class Device:
        device_id = "test"
        package = "com.google.android.deskclock"

        def observe(self):
            return Observation(text="model says saved")

    class Model:
        def decide(self, *args):
            return Action(kind="finish", reason="done")

        def verify(self, *args):
            return Verification(checks=[Check(criterion="saved", passed=True, evidence="saved")])

    with Harness(tmp_path, Device(), Model()) as h:
        result = h.start(Task(goal="设置 23:58 提醒我", criteria=["saved"]))
        assert result["status"] == "waiting_user"
        assert result["task"]["requirement"]["expected_time"] == "23:58"
        assert h.resume(result["task_id"], "continue")["status"] == "waiting_user"
