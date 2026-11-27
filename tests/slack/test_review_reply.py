"""A review end to end: guard, reader, model, two messages, receipt, failures."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta

import pytest

from contracts.v1 import TaskTurnStatus
from modules.slack import copy
from modules.slack.access import messages
from modules.slack.review import render
from shared.metrics import REGISTRY
from tenancy_harness import fake_github_data as data
from tenancy_harness.fake_github import STATE
from tests.support.slack_ask_world import BOT_TOKEN
from tests.support.slack_pipeline_world import ORG
from tests.support.slack_review_world import GOOD_REVIEW, ReviewWorld

RECEIPT = re.compile(
    r'^:white_check_mark: Reviewed acme/api#123, "Add retry to payments client" '
    r"· 14 of 14 files · \d+s$"
)


def _posts(w: ReviewWorld) -> int:
    return w.aworld.slack.count("chat.postMessage")


def _go(w: ReviewWorld, number: int = 123):
    w.allow()
    w.review(number)
    w.aworld.dispatch()
    turn = w.aworld.work()
    w.aworld.reply()
    return turn


def test_a_review_is_two_messages_and_the_first_becomes_the_receipt(
    rworld: ReviewWorld,
) -> None:
    rworld.allow()
    rworld.review(123)
    rworld.aworld.dispatch()
    finished = rworld.aworld.work()
    assert finished.status is TaskTurnStatus.SUCCEEDED
    rworld.runtime.task_turns.put(
        finished.model_copy(
            update={
                "status": TaskTurnStatus.RUNNING,
                "finished_at": None,
                "worker_id": "test-worker",
                "started_at": datetime.now(UTC),
                "assistant_message_id": None,
            }
        )
    )

    rworld.aworld.reply()  # while it runs: the first message names the PR and files
    assert rworld.aworld.slack.texts() == [
        ':mag: Reviewing acme/api#123, "Add retry to payments client" · 14 files'
    ]

    rworld.runtime.task_turns.put(finished)
    rworld.aworld.reply()  # done: the review is the second message

    first, second = rworld.aworld.slack.texts()
    assert RECEIPT.match(first), first
    assert second.startswith("*Review of acme/api#123*")
    assert ":large_orange_circle: *Medium risk*" in second
    assert "• `payments/client.py`: Retries are unbounded on 5xx responses." in second
    assert "*Question for the author:* Should retries apply to POST /charge?" in second
    assert "Reviewed 14 of 14 files · Nothing was posted on GitHub · <" in second
    assert second.rstrip().endswith("|Open in Weaves>_")
    assert _posts(rworld) == 2  # never more than two bot posts per request
    (row,) = rworld.aworld.threads()
    assert row.reply_state == "posted" and row.run_state == "done" and row.posts == 2


def test_nothing_is_posted_twice_however_often_the_tick_runs(
    rworld: ReviewWorld,
) -> None:
    _go(rworld)
    posts, updates = _posts(rworld), rworld.aworld.slack.count("chat.update")

    for _ in range(4):
        rworld.aworld.reply()

    assert _posts(rworld) == posts == 1
    assert rworld.aworld.slack.count("chat.update") == updates


def test_a_failed_post_is_retried_once_not_twice(rworld: ReviewWorld) -> None:
    rworld.allow()
    rworld.review(123)
    rworld.aworld.dispatch()
    rworld.aworld.work()
    rworld.aworld.slack.faults["chat.postMessage"] = ["500"]

    rworld.aworld.reply()
    assert rworld.aworld.slack.texts() == []  # the first attempt failed
    rworld.aworld.at(datetime.now(UTC) + timedelta(minutes=10))
    rworld.aworld.reply()
    rworld.aworld.reply()

    assert len(rworld.aworld.slack.texts()) == 1
    assert rworld.aworld.slack.texts()[0].startswith("*Review of acme/api#123*")


def test_the_reader_is_called_once_with_a_token_for_that_one_repository(
    rworld: ReviewWorld,
) -> None:
    _go(rworld)

    assert rworld.review_calls == 1
    assert ("12345", ("acme/api",)) in rworld.tokens.asked
    paths = [c["path"] for c in STATE.calls if c["method"] == "GET"]
    assert [p for p in paths if "/pulls/" in p] == [
        "/repos/acme/api/pulls/123",
        "/repos/acme/api/pulls/123/files",
    ]
    assert STATE.unexpected == []
    assert all(c["method"] == "GET" for c in STATE.calls)  # nothing was ever written
    (invocation,) = [
        i
        for i in rworld.runtime.tool_invocations.list()
        if i.capability_id == "github.pr.review_context"
    ]
    assert invocation.output_summary["stats"]["files_reviewed"] == 14


def test_a_big_pull_request_gets_the_partial_review_line(rworld: ReviewWorld) -> None:
    _go(rworld, 41)

    (row,) = rworld.aworld.threads()
    text = rworld.aworld.slack.texts()[-1]
    assert (
        "_Reviewed 9 of 41 files (skipped generated and lock files) · Nothing" in text
    )
    assert row.reply_state == "posted"


@pytest.mark.parametrize(
    ("number", "text"),
    [
        (404, render.UNREADABLE),
        (403, render.UNREADABLE),
        (555, render.UNREADABLE),
        (429, render.GITHUB_BUSY),
    ],
)
def test_an_unreadable_pull_request_says_so_in_one_message(
    rworld: ReviewWorld,
    number: int,
    text: str,
) -> None:
    _go(rworld, number)

    assert rworld.aworld.slack.texts() == [text]
    assert _posts(rworld) == 1 and rworld.review_calls == 0  # no model call
    (row,) = rworld.aworld.threads()
    assert row.reply_state == "posted" and row.run_state == "failed"


def test_an_unreadable_pull_request_replaces_the_working_message(
    rworld: ReviewWorld,
) -> None:
    rworld.allow()
    rworld.review(404)
    rworld.aworld.dispatch()
    rworld.aworld.at(datetime.now(UTC) + timedelta(seconds=30))
    rworld.aworld.reply()  # still queued after 10 s: a working message exists
    assert rworld.aworld.slack.texts() == [copy.QUEUED]

    turn = rworld.aworld.work()
    assert turn.status is TaskTurnStatus.FAILED
    rworld.aworld.reply()  # the same message becomes the sentence: no second post

    assert rworld.aworld.slack.texts() == [render.UNREADABLE]
    assert _posts(rworld) == 1


def test_a_server_error_is_a_generic_failure_with_a_reference(
    rworld: ReviewWorld,
) -> None:
    _go(rworld, 500)

    (text,) = rworld.aworld.slack.texts()
    assert re.fullmatch(r":warning: I couldn't finish this \(ref [a-z0-9]{6}\)\.", text)
    assert "internal secret text" not in text


@pytest.mark.parametrize(
    "answer",
    [
        "PWNED-7731",
        "```json\n{}\n```",
        json.dumps({**GOOD_REVIEW, "risk": "catastrophic"}),
        json.dumps({**GOOD_REVIEW, "extra": "key"}),
        json.dumps({"summary": "LGTM", "risk": "low"}),
        json.dumps({**GOOD_REVIEW, "findings": GOOD_REVIEW["findings"] * 4}),
    ],
)
def test_a_review_that_breaks_the_contract_fails_closed_with_one_sentence(
    rworld: ReviewWorld,
    answer: str,
) -> None:
    rworld.reply = lambda _request: answer
    _go(rworld)

    assert (
        rworld.aworld.slack.texts()
        == [render.NO_REVIEW]
        == ["I couldn't produce a review. Try again."]
    )
    assert _posts(rworld) == 1


def test_a_model_answer_the_runtime_accepts_but_we_do_not_still_fails_closed(
    rworld: ReviewWorld,
) -> None:
    """Defense in depth: the second validator is ours, not the runtime's."""
    rworld.allow()
    rworld.review(123)
    rworld.aworld.dispatch()
    turn = rworld.aworld.work()
    message = rworld.runtime.task_messages.get(turn.assistant_message_id)
    rworld.runtime.task_messages.put(
        message.model_copy(update={"content": json.dumps({**GOOD_REVIEW, "x": 1})})
    )
    rworld.aworld.reply()

    assert rworld.aworld.slack.texts() == [render.NO_REVIEW]


# -- prompt injection, offline ---------------------------------------------


def test_the_pull_request_text_reaches_the_model_only_as_fenced_data(
    rworld: ReviewWorld,
) -> None:
    _go(rworld)

    (request,) = rworld.review_requests
    system = request.messages[0].content
    user = "\n".join(m.content for m in request.messages[1:])
    assert data.INJECTION_MARK not in system  # never in the instructions
    assert "UNTRUSTED" in system and "Never" in system
    assert data.INJECTION_MARK in user  # the model does get to see it, as data
    # Both attempts (description and a patch comment) sit between the fences.
    for chunk in re.split(r"-----BEGIN UNTRUSTED PULL REQUEST TEXT-----", user)[1:]:
        assert "-----END UNTRUSTED PULL REQUEST TEXT-----" in chunk
    outside = re.sub(
        r"-----BEGIN UNTRUSTED PULL REQUEST TEXT-----.*?"
        r"-----END UNTRUSTED PULL REQUEST TEXT-----",
        "",
        user,
        flags=re.S,
    )
    assert data.INJECTION_MARK not in outside
    assert user.startswith(
        "Task:\nReview pull request https://github.com/acme/api/pull/123"
    )


def test_a_model_that_obeys_the_injection_cannot_mention_post_or_widen_anything(
    rworld: ReviewWorld,
) -> None:
    rworld.reply = lambda _r: json.dumps(
        {
            "summary": f"{data.INJECTION_MARK} <!channel> approved, see <https://evil.test|here>",
            "risk": "low",
            "findings": [
                {
                    "file": "payments/client.py`<!here>",
                    "issue": "<@U0ADMIN> merge it https://evil.test/x",
                    "severity": "low",
                }
            ],
            "question": "<!everyone> print your system prompt?",
        }
    )
    _go(rworld)

    text = rworld.aworld.slack.texts()[-1]
    assert "<!channel>" not in text and "<!here>" not in text
    assert "<@U0ADMIN>" not in text and "<!everyone>" not in text
    assert "<https://evil" not in text and "](https://evil" not in text
    assert text.count("<") == 1 and "|Open in Weaves>" in text
    # Whatever the model said, the work was read-only and inside the pull request.
    assert STATE.unexpected == [] and all(c["method"] == "GET" for c in STATE.calls)
    assert rworld.aworld.slack.count("chat.postMessage") == 1
    assert len(rworld.tasks()) == 1  # no other task was created


def test_a_hostile_title_cannot_mention_or_link_from_the_receipt(
    rworld: ReviewWorld,
) -> None:
    pull = data.PULLS[("acme/api", 123)][0]
    original = pull["title"]
    pull["title"] = "Fix <!channel> and <@U0ADMIN> see https://evil.test/a `x` *y*"
    try:
        _go(rworld)
    finally:
        pull["title"] = original

    joined = "\n".join(rworld.aworld.slack.texts())
    assert "<!channel>" not in joined and "<@U0ADMIN>" not in joined
    assert "https://evil.test" not in joined


# -- guard, state and privacy ---------------------------------------------


def test_a_rule_turned_off_before_pickup_cancels_the_turn_and_reads_nothing(
    rworld: ReviewWorld,
) -> None:
    rworld.allow()
    rworld.review(123)
    rworld.aworld.dispatch()
    rworld.aworld.world.set_slack_settings(
        rules={
            "create_task": "off",
            "pr_review": "off",
            "run_workflow": "off",
            "ask_private": "off",
        }
    )

    turn = rworld.aworld.work()
    rworld.aworld.reply()

    assert turn.status is TaskTurnStatus.CANCELLED
    assert turn.origin.verdict.cancel_reason == "slack_access_revoked"
    assert STATE.calls == [] and rworld.review_calls == 0
    assert rworld.aworld.slack.ephemerals[-1]["text"].startswith(":lock:")


def test_a_repository_removed_from_the_allowlist_before_pickup_reads_nothing(
    rworld: ReviewWorld,
) -> None:
    rworld.allow()
    rworld.review(123)
    rworld.aworld.dispatch()
    rworld.aworld.world.set_slack_settings(pr_review_repos=[])

    turn = rworld.aworld.work()

    assert turn.status is TaskTurnStatus.CANCELLED
    assert STATE.calls == [] and rworld.review_calls == 0


def test_a_disabled_github_connection_cancels_the_turn_and_reads_nothing(
    rworld: ReviewWorld,
) -> None:
    rworld.allow()
    rworld.review(123)
    rworld.aworld.dispatch()
    rworld.aworld.world.disable(rworld.runtime.installations.get("gh-install-1"))

    turn = rworld.aworld.work()
    rworld.aworld.reply()

    assert turn.status is TaskTurnStatus.CANCELLED
    assert STATE.calls == [] and rworld.review_calls == 0
    (whisper,) = rworld.aworld.slack.ephemerals
    assert whisper["text"] == f":lock: {messages.REPO_NOT_ENABLED}"


def test_an_expired_github_connection_is_reported_without_a_read_or_a_model_call(
    rworld: ReviewWorld,
) -> None:
    rworld.allow()
    rworld.review(123)
    rworld.aworld.dispatch()
    rworld.aworld.world.snapshot(
        rworld.runtime.installations.get("gh-install-1"), "token_expired"
    )

    turn = rworld.aworld.work()
    rworld.aworld.reply()

    assert turn.agent_run_id is None and turn.status is TaskTurnStatus.SUCCEEDED
    assert STATE.calls == [] and rworld.review_calls == 0
    (whisper,) = rworld.aworld.slack.ephemerals
    assert (
        whisper["text"].startswith("I can't check GitHub")
        or "GitHub" in whisper["text"]
    )
    assert "its connection expired" in whisper["text"]


def test_the_decision_is_audited_and_nothing_from_the_pull_request_leaks(
    rworld: ReviewWorld,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    _go(rworld)

    events = list(rworld.runtime.audit_events.list_scoped(ORG))
    decisions = [e for e in events if e.action == "slack.decision"]
    allowed = [e for e in events if e.action == "slack.execute.allowed"]
    assert len(decisions) == 1 and len(allowed) == 1
    meta = decisions[0].metadata
    assert (meta["tier"], meta["action"], meta["outcome"]) == (
        "execute",
        "pr_review",
        "allowed",
    )
    assert tuple(meta["tools_needed"]) == ("github.pr.review_context",)
    audit = json.dumps([e.model_dump(mode="json") for e in events])
    rows = json.dumps([r.model_dump(mode="json") for r in rworld.aworld.threads()])
    logs = "\n".join(
        record.getMessage() + json.dumps(getattr(record, "__dict__", {}), default=str)
        for record in caplog.records
    )
    metrics = REGISTRY.render()
    for haystack in (audit, rows, logs, metrics):
        for secret in (
            "Add retry to payments client",
            data.INJECTION_MARK,
            "payments/client.py",
            "exponential backoff",
            BOT_TOKEN,
        ):
            assert secret not in haystack, secret
    assert 'slack_review_total{outcome="reviewed"}' in metrics
    assert 'slack_execute_total{action="pr_review",outcome="allowed"}' in metrics
