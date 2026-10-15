from sidecar.contracts import Action, Observation, Task
from sidecar.verifiers import extract_ui


def task():
    return Task(
        goal="收件人 a@example.com，主题为“测试”，正文为“测试11111”，保存草稿",
        criteria=["saved"],
        requirement={
            "kind": "gmail_draft_saved",
            "recipient": "a@example.com",
            "subject": "测试",
            "body": "测试11111",
        },
    )


def observation(compose=False, body="测试11111"):
    if compose:
        xml = (
            "<hierarchy>"
            '<node package="com.google.android.gm" '
            'resource-id="com.google.android.gm:id/to_content">'
            '<node package="com.google.android.gm" class="android.widget.EditText" '
            'text="a@example.com"/></node>'
            '<node package="com.google.android.gm" resource-id="com.google.android.gm:id/subject" '
            'text="测试"/>'
            '<node package="com.google.android.gm" '
            'resource-id="com.google.android.gm:id/body_wrapper">'
            '<node package="com.google.android.gm" class="android.widget.EditText" '
            f'text="{body}"/></node>'
            '<node package="com.google.android.gm" resource-id="com.google.android.gm:id/send"/>'
            "</hierarchy>"
        )
    else:
        xml = (
            "<hierarchy>"
            '<node package="com.google.android.gm" class="android.widget.Button" text="Drafts" '
            'checked="true" checkable="true"/>'
            '<node package="com.google.android.gm" '
            'resource-id="com.google.android.gm:id/viewified_conversation_item_view" '
            'text=", , Draft, , , 测试, 测试11111"/>'
            "</hierarchy>"
        )
    ui, _ = extract_ui(xml)
    return Observation(
        text="Gmail",
        package="com.google.android.gm",
        ui=ui,
        elements={
            "row": (
                '{"selector":{"resource_id":'
                '"com.google.android.gm:id/viewified_conversation_item_view"}}'
            ),
            "up": '{"label":"Navigate up"}',
        },
    )


def test_gmail_requires_reopen_exact_content_and_exit():
    from sidecar.app_actions.gmail import advance, verdict

    t = task()
    listing = observation()
    form = observation(True)
    assert verdict(t, form, {})["status"] != "passed"
    proof = advance(t, listing, form, Action(kind="tap", target="row", reason="reopen"), {})
    assert verdict(t, form, proof)["status"] != "passed"  # Must exit as requested.
    proof = advance(t, form, listing, Action(kind="tap", target="up", reason="save"), proof)
    assert verdict(t, listing, proof)["status"] == "passed"


def test_wrong_body_or_post_reopen_edit_cannot_pass():
    from sidecar.app_actions.gmail import advance, verdict

    t = task()
    listing = observation()
    wrong = observation(True, "wrong")
    proof = advance(t, listing, wrong, Action(kind="tap", target="row", reason="reopen"), {})
    proof = advance(t, wrong, listing, Action(kind="tap", target="up", reason="save"), proof)
    assert verdict(t, listing, proof)["status"] != "passed"
    form = observation(True)
    proof = advance(t, listing, form, Action(kind="tap", target="row", reason="reopen"), {})
    proof = advance(
        t, form, form, Action(kind="type", target="body", text="x", reason="edit"), proof
    )
    proof = advance(t, form, listing, Action(kind="tap", target="up", reason="save"), proof)
    assert verdict(t, listing, proof)["status"] != "passed"


def test_requirement_extracts_only_unambiguous_literal_fields():
    from sidecar.app_actions import infer_requirement

    req = infer_requirement("com.google.android.gm", task().goal)
    assert (req.recipient, req.subject, req.body) == ("a@example.com", "测试", "测试11111")
