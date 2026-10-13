"""Opt-in Android 15 acceptance; opens Clock then Settings on a temporary display."""

import os

import pytest

SERIAL = os.environ.get("SIDECAR_LIFECYCLE_SERIAL")
pytestmark = pytest.mark.skipif(not SERIAL, reason="requires Android emulator acceptance")


def test_two_tasks_open_their_own_app_and_leave_hidden_display_empty(tmp_path, monkeypatch):
    from sidecar import displays
    from sidecar.contracts import Action, Check, Verification
    from sidecar.jobs import JobQueue, Settings
    from sidecar.worker import run_next

    class ReadOnlyModel:
        def decide(self, task, observation, history):
            return Action(kind="finish", reason="App opened; read-only lifecycle test")

        def verify(self, task, observation):
            return Verification(
                checks=[
                    Check(
                        criterion=task.criteria[0],
                        passed=True,
                        evidence=observation.text.splitlines()[0],
                    )
                ]
            )

    primary = displays.current(SERIAL, 0)
    packages = ["com.google.android.deskclock", "com.android.settings"]
    assert primary[0] not in packages, "Keep a different app on the primary display for this test"
    monkeypatch.setenv("SIDECAR_MODEL", "lifecycle-test")
    monkeypatch.delenv("SIDECAR_BASE_URL", raising=False)
    monkeypatch.setattr("sidecar.worker.LangChainModel.from_env", lambda: ReadOnlyModel())
    with displays.hidden_display(SERIAL) as binding:
        q = JobQueue(tmp_path)
        q.configure(
            Settings(
                mode="android",
                serial=SERIAL,
                package=packages[0],
                allowed_packages=packages,
                display=binding,
                model="lifecycle-test",
            )
        )
        jobs = [
            q.submit("Observe " + package, app=package, criteria=["App open"])
            for package in packages
        ]
        for job, package in zip(jobs, packages):
            assert run_next(tmp_path)
            result = q.get(job["id"])
            assert result["status"] == "completed", result["reason"]
            assert result["state"]["verification"]["checks"][0]["evidence"] == "App: " + package
            assert not any(e["kind"] == "cleanup_error" for e in q.events(job["id"]))
            stacks = displays.app_stacks(displays.shell(SERIAL, ["am", "stack", "list"]))
            assert not any(item[0] == binding.id for item in stacks.values())
            assert displays.current(SERIAL, 0) == primary
