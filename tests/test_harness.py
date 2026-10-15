import pytest

from sidecar.contracts import Action, Check, Observation, Task, Verification
from sidecar.harness import Harness


class Screen:
    """Controlled external GUI boundary; effects are observable through observe()."""

    device_id = "test-device"

    def __init__(self):
        self.items = []
        self.reads = 0
        self.timeout_after_write = False
        self.sensitive = False

    def observe(self):
        self.reads += 1
        return Observation(text="|".join(self.items), elements={"add": "Add item"})

    def assess(self, action, observation):
        return "handoff" if self.sensitive else "allow"

    def execute(self, action):
        if action.kind == "type":
            self.items.append(action.text)
            if self.timeout_after_write:
                raise TimeoutError("Response lost after commit")


class Policy:
    def __init__(self):
        self.observations = []
        self.on_decide = None

    def decide(self, task, observation, history):
        self.observations.append(observation.id)
        if self.on_decide:
            self.on_decide()
        if "read" not in observation.text:
            return Action(kind="type", target="add", text="read", reason="Create requested item")
        return Action(kind="finish", reason="Item is visible")

    def verify(self, task, observation):
        return Verification(
            checks=[
                Check(criterion=c, passed="read" in observation.text, evidence=observation.text)
                for c in task.criteria
            ]
        )


def task():
    return Task(goal="Add read", criteria=["read exists"])


def test_actions_observe_fresh_state_and_completion_has_new_evidence(tmp_path):
    screen, policy = Screen(), Policy()
    with Harness(tmp_path, screen, policy) as agent:
        result = agent.start(task())
        assert result["status"] == "completed"
        assert screen.items == ["read"]
        assert len(set(policy.observations)) == 2
        assert result["verification"]["observation_id"] not in policy.observations
        assert agent.events(result["task_id"])


def test_uncertain_write_waits_and_resume_does_not_duplicate(tmp_path):
    screen = Screen()
    screen.timeout_after_write = True
    with Harness(tmp_path, screen, Policy()) as agent:
        first = agent.start(task())
        assert first["status"] == "waiting_user"
        assert screen.items == ["read"]
    with Harness(tmp_path, screen, Policy()) as agent:
        result = agent.resume(first["task_id"], "Checked the item exists")
        assert result["status"] == "completed"
        assert screen.items == ["read"]


def test_sensitive_action_is_not_executed_and_resume_reobserves(tmp_path):
    screen = Screen()
    screen.sensitive = True
    with Harness(tmp_path, screen, Policy()) as agent:
        first = agent.start(task())
        assert first["status"] == "waiting_user"
        assert screen.items == []
        screen.items.append("read")  # User completes the action in the device.
        result = agent.resume(first["task_id"], "Done manually")
        assert result["status"] == "completed"
        assert screen.items == ["read"]


@pytest.mark.parametrize("signal,expected", [("cancel", "cancelled"), ("pause", "paused")])
def test_control_arriving_during_decision_prevents_action(tmp_path, signal, expected):
    screen, policy = Screen(), Policy()
    with Harness(tmp_path, screen, policy) as agent:
        policy.on_decide = lambda: agent.control("controlled", signal)
        result = agent.start(task(), task_id="controlled")
        assert result["status"] == expected
        assert screen.items == []


def test_finish_claim_without_all_checks_cannot_complete(tmp_path):
    class Liar(Policy):
        def decide(self, *args):
            return Action(kind="finish", reason="Done")

        def verify(self, *args):
            return Verification(checks=[])

    with Harness(tmp_path, Screen(), Liar(), max_decisions=3) as agent:
        result = agent.start(task())
        assert result["status"] == "failed"
        assert result["decisions"] <= 3


def test_unknown_target_never_reaches_device(tmp_path):
    class BadTarget(Policy):
        def decide(self, *args):
            return Action(kind="type", target="missing", text="read", reason="Wrong target")

    screen = Screen()
    with Harness(tmp_path, screen, BadTarget(), max_decisions=2) as agent:
        result = agent.start(task())
        assert result["status"] == "failed"
        assert screen.items == []


def test_unchanged_screen_stops_wait_loop(tmp_path):
    class Waiting(Policy):
        def decide(self, *args):
            return Action(kind="wait", reason="Loading")

    with Harness(tmp_path, Screen(), Waiting(), max_stalls=2) as agent:
        result = agent.start(task())
        assert result["status"] == "failed"
        assert result["decisions"] <= 3


def test_resume_rejects_different_device(tmp_path):
    screen = Screen()
    screen.sensitive = True
    with Harness(tmp_path, screen, Policy()) as agent:
        first = agent.start(task())
    screen.device_id = "another-device"
    with Harness(tmp_path, screen, Policy()) as agent:
        with pytest.raises(ValueError, match="device"):
            agent.resume(first["task_id"], "Continue")


def test_duplicate_task_id_does_not_restart(tmp_path):
    with Harness(tmp_path, Screen(), Policy()) as agent:
        agent.start(task(), task_id="same")
        with pytest.raises(ValueError, match="exists"):
            agent.start(task(), task_id="same")


def test_invalid_task_and_action_are_rejected():
    with pytest.raises(ValueError):
        Task(goal="  ", criteria=[])
    with pytest.raises(ValueError):
        Action(kind="type", target="add", reason="Missing text")
    with pytest.raises(ValueError):
        Action(kind="shell", reason="Unsupported tool")


def test_pause_can_resume_and_cancel_is_terminal(tmp_path):
    screen, policy = Screen(), Policy()
    with Harness(tmp_path, screen, policy) as agent:
        policy.on_decide = lambda: agent.control("pause-me", "pause")
        result = agent.start(task(), task_id="pause-me")
        assert result["status"] == "paused"
        policy.on_decide = None
        assert agent.resume("pause-me", "Continue")["status"] == "completed"
        with pytest.raises(ValueError, match="terminal"):
            agent.resume("pause-me", "Again")


def test_runtime_failure_is_saved_without_raw_exception_secrets(tmp_path):
    class Broken(Policy):
        def decide(self, *args):
            raise RuntimeError("private-token-do-not-log")

    with Harness(tmp_path, Screen(), Broken()) as agent:
        result = agent.start(task())
        assert result["status"] == "failed"
        assert "private-token-do-not-log" not in str(agent.events(result["task_id"]))


def test_concurrent_execution_is_rejected(tmp_path):
    screen, policy = Screen(), Policy()
    with Harness(tmp_path, screen, policy) as first, Harness(tmp_path, screen, Policy()) as second:

        def contend():
            with pytest.raises(RuntimeError, match="busy"):
                second.start(task())

        policy.on_decide = contend
        assert first.start(task())["status"] == "completed"


def test_crash_after_side_effect_does_not_replay_action_on_recovery(tmp_path):
    class CrashingScreen(Screen):
        def execute(self, action):
            super().execute(action)
            raise SystemExit("simulated process death")

    screen = CrashingScreen()
    with Harness(tmp_path, screen, Policy()) as agent:
        with pytest.raises(SystemExit):
            agent.start(task(), task_id="crash")
    assert screen.items == ["read"]
    with Harness(tmp_path, screen, Policy()) as agent:
        recovered = agent.resume("crash", "Checked screen after crash")
        assert recovered["status"] == "completed"
        assert screen.items == ["read"]


def test_waiting_task_reserves_environment_until_cancelled(tmp_path):
    screen = Screen()
    screen.sensitive = True
    with Harness(tmp_path, screen, Policy()) as agent:
        first = agent.start(task())
        with pytest.raises(ValueError, match="unfinished"):
            agent.start(task())
        agent.control(first["task_id"], "cancel")
        screen.sensitive = False
        assert agent.start(task())["status"] == "completed"


def test_cancel_after_crash_is_not_erased_by_resume(tmp_path):
    class CrashingModel(Policy):
        def decide(self, *args):
            raise SystemExit("Worker died")

    screen = Screen()
    with Harness(tmp_path, screen, CrashingModel()) as agent:
        with pytest.raises(SystemExit):
            agent.start(task(), task_id="cancel-after-crash")
        agent.control("cancel-after-crash", "cancel")
    with Harness(tmp_path, screen, Policy()) as agent:
        assert agent.status("cancel-after-crash")["status"] == "cancelled"
        with pytest.raises(ValueError, match="terminal"):
            agent.resume("cancel-after-crash", "Try to continue")
        assert screen.items == []


def test_external_screen_change_discards_stale_action(tmp_path):
    screen, policy = Screen(), Policy()
    policy.on_decide = lambda: screen.items.append("user-edit")
    with Harness(tmp_path, screen, policy, max_decisions=2) as agent:
        result = agent.start(task())
        assert result["status"] == "failed"
        assert "read" not in screen.items


def test_verification_needs_evidence_for_every_unique_criterion(tmp_path):
    class Incomplete(Policy):
        def decide(self, *args):
            return Action(kind="finish", reason="Verify")

        def verify(self, task, observation):
            return Verification(
                checks=[
                    Check(criterion="first", passed=True, evidence="seen"),
                    Check(criterion="first", passed=True, evidence="seen"),
                ]
            )

    with Harness(tmp_path, Screen(), Incomplete(), max_decisions=1) as agent:
        result = agent.start(Task(goal="Both", criteria=["first", "second"]))
        assert result["status"] == "failed"


def test_time_budget_exceeded_during_model_call_prevents_action(tmp_path):
    import time

    screen, policy = Screen(), Policy()
    policy.on_decide = lambda: time.sleep(0.03)
    with Harness(tmp_path, screen, policy, max_seconds=0.02) as agent:
        assert agent.start(task())["status"] == "failed"
        assert screen.items == []


def test_status_shows_progress_while_task_is_running(tmp_path):
    screen, policy = Screen(), Policy()
    with Harness(tmp_path, screen, policy) as agent:

        def check():
            if screen.items:
                assert agent.status("progress")["decisions"] == 1

        policy.on_decide = check
        assert agent.start(task(), task_id="progress")["status"] == "completed"


def test_resume_after_crash_before_first_checkpoint(tmp_path):
    def crash():
        raise SystemExit("Before first checkpoint")

    screen = Screen()
    with Harness(tmp_path, screen, Policy()) as agent:
        with pytest.raises(SystemExit):
            agent.start(task(), task_id="early-crash", prepare=crash)
    with Harness(tmp_path, screen, Policy()) as agent:
        result = agent.resume("early-crash", "Checked setup and device")
        assert result["status"] == "completed"
        assert screen.items == ["read"]


def test_cancel_is_not_downgraded_by_later_pause(tmp_path):
    screen, policy = Screen(), Policy()
    with Harness(tmp_path, screen, policy) as agent:

        def control():
            agent.control("cancel-first", "cancel")
            agent.control("cancel-first", "pause")

        policy.on_decide = control
        result = agent.start(task(), task_id="cancel-first")
        assert result["status"] == "cancelled"
        assert screen.items == []


def test_cancel_arriving_during_resume_is_sticky(tmp_path, monkeypatch):
    screen = Screen()
    screen.sensitive = True
    with Harness(tmp_path, screen, Policy()) as agent:
        first = agent.start(task())
        screen.sensitive = False
        original = agent.store.control

        def cancel_before_clear(task_id, signal):
            if signal == "":
                from sidecar.store import Store

                controller = Store(tmp_path)
                try:
                    controller.control(task_id, "cancel")
                finally:
                    controller.close()
            return original(task_id, signal)

        monkeypatch.setattr(agent.store, "control", cancel_before_clear)
        result = agent.resume(first["task_id"], "Continue")
        assert result["status"] == "cancelled"
        assert screen.items == []


def test_handoff_cannot_overwrite_acknowledged_cancel(tmp_path, monkeypatch):
    screen = Screen()
    screen.sensitive = True
    with Harness(tmp_path, screen, Policy()) as agent:
        original = agent.store.save
        waits = 0

        def cancel_before_second_wait_save(state):
            nonlocal waits
            if state["status"] == "waiting_user":
                waits += 1
                if waits == 2:
                    with Harness(tmp_path, screen, Policy()) as controller:
                        controller.control(state["task_id"], "cancel")
            return original(state)

        monkeypatch.setattr(agent.store, "save", cancel_before_second_wait_save)
        result = agent.start(task())
        assert result["status"] == "cancelled"
        assert agent.status(result["task_id"])["status"] == "cancelled"
        screen.sensitive = False
        assert agent.start(task())["status"] == "completed"


def test_cancel_orphan_releases_workspace(tmp_path):
    class Crash(Policy):
        def decide(self, *args):
            raise SystemExit()

    screen = Screen()
    with Harness(tmp_path, screen, Crash()) as h:
        with pytest.raises(SystemExit):
            h.start(task(), task_id="orphan")
    with Harness(tmp_path, screen, Policy()) as h:
        h.control("orphan", "cancel")
        assert h.status("orphan")["status"] == "cancelled"
        assert h.start(task())["status"] == "completed"


def test_transient_model_timeout_is_retried(tmp_path):
    class Flaky(Policy):
        calls = 0

        def decide(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError()
            return super().decide(*args)

    with Harness(tmp_path, Screen(), Flaky()) as h:
        assert h.start(task())["status"] == "completed"


def test_cancel_during_timeout_prevents_retry(tmp_path):
    class Flaky(Policy):
        calls = 0

        def decide(self, *args):
            self.calls += 1
            h.control("retry-cancel", "cancel")
            raise TimeoutError()

    model = Flaky()
    with Harness(tmp_path, Screen(), model) as h:
        assert h.start(task(), task_id="retry-cancel")["status"] == "cancelled"
        assert model.calls == 1


def test_focused_input_does_not_need_invented_target():
    assert Action(kind="type", text="中文内容", reason="填写已聚焦输入框").target is None


def test_adapter_can_validate_target_despite_unrelated_changes(tmp_path):
    class Moving(Screen):
        def observe(self):
            self.reads += 1
            return Observation(text=f"clock={self.reads}; {self.items}", elements={"add": "Add"})

        def is_current(self, action, before, after):
            return before.elements.get(action.target) == after.elements.get(action.target)

    screen = Moving()
    with Harness(tmp_path, screen, Policy(), max_decisions=3) as h:
        assert h.start(task())["status"] == "completed"
        assert screen.items == ["read"]


def test_prepare_setup_error_keeps_readable_reason(tmp_path):
    from sidecar.contracts import DeviceSetupError

    def prepare():
        raise DeviceSetupError("目标 App 正在主屏使用；请先切换")

    with Harness(tmp_path, Screen(), Policy()) as agent:
        result = agent.start(task(), prepare=prepare)
        assert result["status"] == "failed"
        assert "目标 App 正在主屏使用" in result["reason"]


def test_prepare_uncontrolled_error_hides_message(tmp_path):
    def prepare():
        raise RuntimeError("private-token-do-not-log")

    with Harness(tmp_path, Screen(), Policy()) as agent:
        result = agent.start(task(), prepare=prepare)
        assert result["status"] == "failed"
        assert "private-token-do-not-log" not in result["reason"]


@pytest.mark.parametrize("stage", ["decide", "verify"])
def test_model_api_rejection_keeps_safe_status_message(tmp_path, stage):
    import httpx
    from openai import RateLimitError

    from sidecar.model import LangChainModel

    class Reject:
        def invoke(self, messages):
            raise RateLimitError(
                "private-provider-body",
                response=httpx.Response(
                    429, request=httpx.Request("POST", "https://example.invalid")
                ),
                body={"secret": "private-provider-body"},
            )

    class Policy:
        def decide(self, *args):
            if stage == "decide":
                return LangChainModel._invoke(Reject(), [])
            return Action(kind="finish", reason="check")

        def verify(self, *args):
            return LangChainModel._invoke(Reject(), [])

    with Harness(tmp_path, Screen(), Policy()) as h:
        result = h.start(task())
        assert result["status"] == "failed"
        assert "模型 API 返回 429" in result["reason"]
        events = h.events(result["task_id"])
        assert "private-provider-body" not in str(events)
        assert next(e["data"]["stage"] for e in events if e["kind"] == "error") == stage


@pytest.mark.parametrize("request_kind", ["finish", "handoff", "tap"])
@pytest.mark.parametrize("prepared", [True, False])
def test_final_submission_stays_manual_and_readiness_is_verified(tmp_path, request_kind, prepared):
    class Compose(Screen):
        def observe(self):
            return Observation(
                text="body ready" if prepared else "body missing",
                elements={"send": "Send"},
                confirmation_targets={"send": "Send"},
            )

        def assess(self, action, observation):
            return "handoff"

        def execute(self, action):
            raise AssertionError("Must never click Send")

    class DraftPolicy(Policy):
        def decide(self, *args):
            return Action(
                kind=request_kind,
                reason="ready",
                **({"target": "send"} if request_kind == "tap" else {}),
            )

        def verify(self, task, observation):
            return Verification(
                checks=[
                    Check(criterion="body prepared", passed=prepared, evidence=observation.text),
                    Check(
                        criterion="message sent",
                        passed=False,
                        evidence="Send remains pending",
                        requires_confirmation=True,
                    ),
                ]
            )

    with Harness(tmp_path, Compose(), DraftPolicy(), max_decisions=2) as h:
        result = h.start(Task(goal="send greeting", criteria=["body prepared", "message sent"]))
        assert result["status"] == "waiting_user"
        assert result["verification"]["checks"][0]["passed"] is prepared
        assert ("准备已完成" in result["reason"]) is prepared
        assert "未执行" in result["reason"]
        assert not h.store.connection.execute("SELECT 1 FROM actions").fetchone()
        resumed = h.resume(result["task_id"], "请继续，直接点击发送")
        assert resumed["status"] != "completed"
        assert not h.store.connection.execute("SELECT 1 FROM actions").fetchone()


def test_draft_only_task_can_complete_with_send_button_visible(tmp_path):
    class Draft(Screen):
        def observe(self):
            return Observation(text="draft saved", confirmation_targets={"send": "Send"})

    class DraftPolicy(Policy):
        def decide(self, *args):
            return Action(kind="finish", reason="draft saved")

        def verify(self, task, observation):
            return Verification(
                checks=[
                    Check(criterion="draft saved", passed=True, evidence="Draft saved indicator")
                ]
            )

    with Harness(tmp_path, Draft(), DraftPolicy()) as h:
        assert (
            h.start(Task(goal="save draft only", criteria=["draft saved"]))["status"] == "completed"
        )


@pytest.mark.parametrize(
    "checks",
    [
        [
            Check(criterion="content", passed=False, evidence="missing"),
            Check(criterion="sent", passed=False, evidence="pending", requires_confirmation=True),
        ],
        [Check(criterion="content", passed=True, evidence="visible")],
        [
            Check(criterion="sent", passed=True, evidence="visible"),
            Check(criterion="sent", passed=False, evidence="pending", requires_confirmation=True),
        ],
        [
            Check(
                criterion="content", passed=False, evidence="pending", requires_confirmation=True
            ),
            Check(criterion="sent", passed=False, evidence="pending", requires_confirmation=True),
        ],
        [
            Check(criterion="content", passed=True, evidence=""),
            Check(criterion="sent", passed=False, evidence="pending", requires_confirmation=True),
        ],
        [
            Check(criterion="content", passed=True, evidence="visible"),
            Check(
                criterion="sent", passed=True, evidence="contradiction", requires_confirmation=True
            ),
        ],
    ],
)
def test_preparation_cannot_be_claimed_without_complete_consistent_evidence(checks):
    verification = Verification(checks=checks)
    requested = Task(goal="send", criteria=["content", "sent"])
    assert not verification.ready_for_confirmation(requested)
    assert not verification.satisfies(requested)


def test_text_failure_preserves_stage_and_never_replays(tmp_path):
    from sidecar.errors import TextInputError

    class FailingScreen(Screen):
        def execute(self, action):
            self.items.append(action.text)
            raise TextInputError("verification")

    screen = FailingScreen()
    with Harness(tmp_path, screen, Policy()) as agent:
        result = agent.start(task())
        assert result["status"] == "waiting_user"
        assert "核验" in result["reason"]
        assert screen.items == ["read"]
        event = next(e for e in agent.events(result["task_id"]) if e["kind"] == "uncertain_action")
        assert event["data"]["stage"] == "verification"
        assert event["data"]["operation"] == "text_input"


def test_final_verification_uses_independent_capture_and_records_provenance(tmp_path):
    from sidecar.contracts import Action, Check, Observation, Task, Verification
    from sidecar.harness import Harness
    from sidecar.offline import OfflineDevice
    from sidecar.verification_policy import POLICY_SHA256

    class Device(OfflineDevice):
        fresh_calls = 0

        def observe(self):
            return Observation(text="preview says saved")

        def observe_fresh(self):
            self.fresh_calls += 1
            return Observation(text="fresh saved record")

    class Model:
        def decide(self, task, observation, history):
            return Action(kind="finish", reason="verify result")

        def verify(self, task, observation):
            assert observation.text == "fresh saved record"
            return Verification(checks=[Check(criterion="saved", passed=True,
                                              evidence=observation.text)])

    device = Device(tmp_path)
    with Harness(tmp_path, device, Model()) as h:
        result = h.start(Task(goal="save", criteria=["saved"]))
    assert result["status"] == "completed"
    assert device.fresh_calls >= 1
    evidence = result["verification"]
    assert evidence["observation_id"] == result["observation"]["id"]
    assert evidence["policy_sha256"] == POLICY_SHA256
    assert evidence["method"] == "model_only"
    assert evidence["device_id"] == device.device_id
    assert evidence["verified_at"]
