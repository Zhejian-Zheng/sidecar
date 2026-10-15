"""Per-app completion actions: requirement inference, deterministic verification, evidence chains.

Apps without an entry in the registry stay model_only; the harness never knows a concrete app.
"""

from sidecar.contracts import Observation, Task


def program_verdict(
    status, reason, *, observation_id, verifier_version, evidence=None, details=None
):
    """Standard program-verdict shape consumed by the harness completion gate."""
    return {
        "status": status,  # "passed" | "failed" | "uncertain"
        "reason": reason,
        "observation_id": observation_id,
        "verifier_version": verifier_version,
        "evidence": evidence or [],
        "details": details or {},
    }


class AppAction:
    """One app's deterministic completion knowledge.

    Override infer_requirement / verify for a static gate; set evidence_chain=True and
    override advance for multi-page evidence (reopen-and-check flows). prompt_hint is
    appended to the decision prompt. The harness only ever sees this interface.
    """

    package: str = ""
    kinds: frozenset[str] = frozenset()
    evidence_chain: bool = False

    def infer_requirement(self, goal: str):
        """A structured VerificationRequirement parsed from the goal, or None."""
        return None

    def verify(self, task: Task, obs: Observation, context: dict) -> dict:
        """Deterministic check against the fresh observation. Never falls back to the model."""
        return program_verdict(
            "uncertain",
            "该任务类型没有可用的程序验证器",
            observation_id=obs.id,
            verifier_version="none",
        )

    def advance(
        self, task: Task, before: Observation, after: Observation, action, context: dict
    ) -> dict:
        """Evidence accumulation after a returned action; used only when evidence_chain."""
        return context

    def prompt_hint(self) -> str:
        """App-specific guidance appended to the decision prompt."""
        return ""
