"""The fixed grammar: code classifies a mention, never a model."""

from __future__ import annotations

import pytest

from modules.slack.classify import IntentKind, classify, clean_text, parse_pr_url

BOT = "U0BOT"


def kind(text: str) -> IntentKind:
    return classify(f"<@{BOT}> {text}".strip(), BOT).kind


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", IntentKind.HELP),
        ("help", IntentKind.HELP),
        ("?", IntentKind.HELP),
        ("hi", IntentKind.GREETING),
        ("Hello there!", IntentKind.GREETING),
        ("good morning", IntentKind.GREETING),
        ("thanks", IntentKind.GREETING),
        ("task: draft the release notes", IntentKind.TASK),
        ("Task : x", IntentKind.TASK),
        ("review https://github.com/acme/api/pull/42", IntentKind.REVIEW),
        ("review", IntentKind.REVIEW),
        ("run weekly-report", IntentKind.RUN),
        ("run", IntentKind.RUN),
        ("status", IntentKind.STATUS),
        ("stop", IntentKind.STOP),
        ("retry", IntentKind.RETRY),
        ("what is the status of WEAVES-12?", IntentKind.ASK),
        ("review the postmortem from last week", IntentKind.ASK),
        ("run the migration again please", IntentKind.ASK),
        ("create a task for me", IntentKind.ASK),
        ("hi, what is WEAVES-1?", IntentKind.ASK),
    ],
)
def test_classification(text: str, expected: IntentKind) -> None:
    assert kind(text) is expected


def test_the_task_text_and_pr_reference_are_extracted() -> None:
    assert classify(f"<@{BOT}> task: fix it", BOT).arg == "fix it"
    pr = f"<@{BOT}> review <https://github.com/acme/api/pull/42|PR>"
    assert classify(pr, BOT).arg == "acme/api#42"
    assert classify(f"<@{BOT}> review https://gitlab.com/a/b/pull/1", BOT).arg == ""


def test_only_our_own_mention_is_removed() -> None:
    cleaned = clean_text(f"<@{BOT}>  what  is <@U0OTHER> doing", BOT)
    assert cleaned == "what is @U0OTHER doing"


def test_text_is_not_in_the_repr() -> None:
    assert "secret question" not in repr(classify(f"<@{BOT}> secret question", BOT))


def test_a_pull_request_url_is_parsed_strictly() -> None:
    assert parse_pr_url("see https://github.com/acme/api/pull/42/files") == (
        "acme/api#42"
    )
    assert parse_pr_url("https://github.com/acme/api/issues/42") is None
