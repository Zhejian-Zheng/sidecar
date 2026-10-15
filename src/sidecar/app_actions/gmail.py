"""Evidence for a saved draft: reopen from Drafts, match fields, exit without editing."""

import json
import re

from sidecar.app_actions.base import AppAction, program_verdict
from sidecar.contracts import GmailDraftRequirement

PACKAGE = "com.google.android.gm"
PREFIX = PACKAGE + ":id/"


def usable(obs):
    return obs.package == PACKAGE and not obs.requires_human and not obs.ui_truncated


def draft_list(obs):
    return (
        usable(obs)
        and not any(n.resource_id == PREFIX + "send" for n in obs.ui)
        and any(
            n.package == PACKAGE and n.text in {"Drafts", "草稿"} and n.checked is True
            for n in obs.ui
        )
    )


def fields_match(task, obs):
    req = task.requirement
    if not usable(obs) or not all((req.recipient, req.subject, req.body)):
        return False
    by_id = {n.node_id: n for n in obs.ui}

    def inside(node, resource):
        seen = set()
        while node and node.node_id not in seen:
            seen.add(node.node_id)
            if node.resource_id == PREFIX + resource:
                return True
            node = by_id.get(node.parent_id)
        return False

    subjects = [
        n.text for n in obs.ui if n.package == PACKAGE and n.resource_id == PREFIX + "subject"
    ]
    bodies = [
        n.text
        for n in obs.ui
        if n.package == PACKAGE and "EditText" in n.klass and inside(n, "body_wrapper")
    ]
    recipients = set()
    for n in obs.ui:
        if n.package == PACKAGE and inside(n, "to_content"):
            recipients.update(
                re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", n.text + " " + n.description)
            )
        if n.resource_id in {PREFIX + "cc_content", PREFIX + "bcc_content"}:
            # Additional recipient fields need a separate, explicit verifier contract.
            return False
    return (
        subjects == [req.subject]
        and bodies == [req.body]
        and recipients == {req.recipient}
        and any(n.resource_id == PREFIX + "send" for n in obs.ui)
    )


def advance(task, before, after, action, proof):
    """Called only after a successfully returned device action and fresh observation."""
    if not usable(before) or not usable(after):
        return {}
    if action.kind in {"type", "swipe"}:
        return {}
    target = json.loads(before.elements.get(action.target, "{}"))
    if (
        action.kind == "tap"
        and draft_list(before)
        and target.get("selector", {}).get("resource_id")
        == PREFIX + "viewified_conversation_item_view"
    ):
        return (
            {"reopened": after.id, "session": after.display_session}
            if fields_match(task, after)
            else {}
        )
    if (
        proof.get("reopened")
        and proof.get("session") == after.display_session
        and fields_match(task, before)
        and draft_list(after)
        and action.kind == "tap"
        and target.get("label") in {"Navigate up", "向上导航"}
    ):
        return {**proof, "exited": after.id}
    return proof if action.kind == "wait" else {}


def verdict(task, obs, proof):
    passed = (
        bool(proof.get("reopened") and proof.get("exited"))
        and draft_list(obs)
        and proof.get("session") == obs.display_session
    )
    return program_verdict(
        "passed" if passed else "uncertain",
        (
            "已从草稿箱重开核对收件人、主题与正文，并退出撰写界面"
            if passed
            else "需要从草稿箱重新打开，核对收件人、主题和完整正文后退出；尚未确认保存"
        ),
        observation_id=obs.id,
        verifier_version="gmail.reopen.v1",
        evidence=[proof] if passed else [],
    )


_GMAIL_PROMPT = (
    "For Gmail draft-only tasks, fill the subject and body before the recipient when all "
    "fields are visible, to avoid recipient autocomplete hiding other fields. After filling, "
    "use Navigate up to save, open Drafts, reopen the saved draft and inspect all fields, "
    "then Navigate up again and finish. Do not claim saved from the compose screen alone."
)


class GmailAction(AppAction):
    package = PACKAGE
    kinds = frozenset({"gmail_draft_saved"})
    evidence_chain = True

    def infer_requirement(self, goal):
        if not re.search(r"草稿|\bdraft\b", goal, re.I):
            return None
        emails = set(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", goal))

        def quoted(label):
            values = re.findall(label + r'\s*(?:为|是|[:：])?\s*[“"]([^”"]+)[”"]', goal)
            return values[0] if len(values) == 1 else None

        return GmailDraftRequirement(
            recipient=next(iter(emails)) if len(emails) == 1 else None,
            subject=quoted("主题"),
            body=quoted("正文"),
        )

    def verify(self, task, obs, context):
        return verdict(task, obs, context)

    def advance(self, task, before, after, action, context):
        return advance(task, before, after, action, context)

    def prompt_hint(self):
        return _GMAIL_PROMPT
