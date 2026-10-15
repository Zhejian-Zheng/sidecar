"""Per-app completion actions registry. Apps without an entry stay model_only."""

from sidecar.app_actions.base import AppAction, program_verdict
from sidecar.app_actions.clock import ClockAction
from sidecar.app_actions.gmail import GmailAction

ACTIONS: tuple[AppAction, ...] = (ClockAction(), GmailAction())
BY_PACKAGE = {action.package: action for action in ACTIONS}
BY_KIND = {kind: action for action in ACTIONS for kind in action.kinds}


def by_package(package):
    return BY_PACKAGE.get(package) if package else None


def by_kind(kind):
    return BY_KIND.get(kind) if kind else None


def infer_requirement(package, goal):
    """Structured requirement from the app's own parser; None keeps the task model_only."""
    action = by_package(package)
    return action.infer_requirement(goal) if action else None


def evaluate(task, obs, context=None):
    """Deterministic completion verdict for the task's requirement kind."""
    action = by_kind(task.requirement.kind)
    if action is None:
        return program_verdict(
            "uncertain",
            "该任务类型没有可用的程序验证器",
            observation_id=obs.id,
            verifier_version="none",
        )
    return action.verify(task, obs, context or {})


def prompt_hint(kind):
    action = by_kind(kind)
    return action.prompt_hint() if action else ""
