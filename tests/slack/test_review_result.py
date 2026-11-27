"""The model's review is validated strictly and every untrusted string is made safe."""

from __future__ import annotations

import json
from typing import Any

import pytest

from modules.slack.review import render
from modules.slack.review.prompt import REVIEW_OUTPUT_SCHEMA
from modules.slack.review.result import parse_review
from modules.slack.review.stats import ReviewStats
from tests.support.slack_review_world import GOOD_REVIEW

STATS = ReviewStats("acme/api", 123, "Add retry to payments client", 14, 14, {})


def good(**changes: Any) -> dict[str, Any]:
    return {**GOOD_REVIEW, **changes}


def test_a_valid_review_parses() -> None:
    review = parse_review(json.dumps(GOOD_REVIEW))
    assert review is not None and review.risk == "medium"
    assert [f.file for f in review.findings] == [
        "payments/client.py",
        "tests/test_retry.py",
    ]
    assert parse_review(json.dumps(good(findings=[]))) is not None


@pytest.mark.parametrize(
    "content",
    [
        "",
        "PWNED-7731",
        "LGTM, approved",
        "```json\n" + json.dumps(GOOD_REVIEW) + "\n```",
        json.dumps(GOOD_REVIEW) + " trailing words",
        "[]",
        "null",
        json.dumps({**GOOD_REVIEW, "extra": "key"}),
        json.dumps({k: v for k, v in GOOD_REVIEW.items() if k != "question"}),
        json.dumps(good(risk="critical")),
        json.dumps(good(risk="Medium")),
        json.dumps(good(risk=None)),
        json.dumps(good(summary="")),
        json.dumps(good(summary="x" * 701)),
        json.dumps(good(summary=7)),
        json.dumps(good(question="")),
        json.dumps(good(question="q" * 301)),
        json.dumps(good(findings="none")),
        json.dumps(good(findings=[GOOD_REVIEW["findings"][0]] * 6)),
        json.dumps(good(findings=[{"file": "a", "issue": "b"}])),
        json.dumps(
            good(findings=[{"file": "a", "issue": "b", "severity": "low", "x": 1}])
        ),
        json.dumps(good(findings=[{"file": "a", "issue": "b", "severity": "urgent"}])),
        json.dumps(good(findings=[{"file": "", "issue": "b", "severity": "low"}])),
        json.dumps(
            good(findings=[{"file": "f" * 201, "issue": "b", "severity": "low"}])
        ),
        json.dumps(good(findings=["a finding"])),
        "{" * 5000,
    ],
)
def test_anything_but_the_exact_contract_fails_closed(content: str) -> None:
    assert parse_review(content) is None


def test_the_strict_parser_never_accepts_what_the_runtime_schema_rejects() -> None:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(REVIEW_OUTPUT_SCHEMA)
    assert not list(validator.iter_errors(GOOD_REVIEW))
    for bad in (
        good(risk="critical"),
        {**GOOD_REVIEW, "extra": 1},
        good(findings=[{}]),
    ):
        assert list(validator.iter_errors(bad))


def test_the_review_message_has_the_scene_d_shape() -> None:
    review = parse_review(json.dumps(GOOD_REVIEW))
    assert review is not None
    text = render.review_message(review, STATS, "task-1")

    lines = text.split("\n")
    assert lines[0] == "*Review of acme/api#123*"
    assert "Adds exponential backoff to the payments client." in text
    assert ":large_orange_circle: *Medium risk*" in text
    assert "• `payments/client.py`: Retries are unbounded on 5xx responses." in text
    assert text.index("payments/client.py") < text.index("tests/test_retry.py")
    assert "*Question for the author:* Should retries apply to POST /charge?" in text
    assert lines[-1].startswith(
        "_Reviewed 14 of 14 files · Nothing was posted on GitHub · <https://"
    )
    assert lines[-1].endswith("/#tasks/task-1|Open in Weaves>_")


@pytest.mark.parametrize(
    ("risk", "icon", "word"),
    [
        ("low", ":large_green_circle:", "Low"),
        ("medium", ":large_orange_circle:", "Medium"),
        ("high", ":red_circle:", "High"),
    ],
)
def test_the_risk_is_an_icon_and_a_word(risk: str, icon: str, word: str) -> None:
    review = parse_review(json.dumps(good(risk=risk)))
    assert review is not None
    assert f"{icon} *{word} risk*" in render.review_message(review, STATS, "t")


def test_no_findings_says_so() -> None:
    review = parse_review(json.dumps(good(findings=[])))
    assert review is not None
    assert "No issues found in the files I read." in render.review_message(
        review, STATS, "t"
    )


def test_a_partial_review_names_what_was_skipped() -> None:
    generated = ReviewStats("acme/api", 41, "Upgrade", 41, 9, {"generated": 32})
    assert (
        render.footer(generated, "t").split(" · ")[0]
        == "_Reviewed 9 of 41 files (skipped generated and lock files)"
    )
    mixed = ReviewStats(
        "acme/api", 7, "Split", 30, 20, {"generated": 2, "size": 8, "no_patch": 0}
    )
    assert "(skipped generated and lock files and files over the size limit)" in (
        render.footer(mixed, "t")
    )
    oversized = ReviewStats("acme/api", 7, "Split", 30, 20, {"size": 10})
    assert "(skipped files over the size limit)" in render.footer(oversized, "t")


def test_the_working_message_and_the_receipt_name_the_pull_request() -> None:
    assert render.working_start("acme/api#123") == ":mag: Reviewing acme/api#123…"
    assert render.working(STATS) == (
        ':mag: Reviewing acme/api#123, "Add retry to payments client" · 14 files'
    )
    assert render.receipt(STATS, 38) == (
        ':white_check_mark: Reviewed acme/api#123, "Add retry to payments client" '
        "· 14 of 14 files · 38s"
    )
    one = ReviewStats("a/b", 1, "T", 1, 1, {})
    assert render.working(one).endswith("· 1 file")


def test_untrusted_text_cannot_mention_link_or_format() -> None:
    hostile = ReviewStats(
        "acme/api",
        5,
        'Hi <!channel> <@U0ADMIN> <#C0SECRET|x> "quoted" https://evil.test/p\x07',
        2,
        2,
        {},
    )
    review = parse_review(
        json.dumps(
            good(
                summary="<!here> see <https://evil.test|click> and <@U0ADMIN>",
                question="Reply to <!everyone>? Visit https://evil.test/x",
                findings=[
                    {
                        "file": "a`b<c>|d\n e.py",
                        "issue": "<!channel> `code` [link](https://evil.test/a)",
                        "severity": "high",
                    }
                ],
            )
        )
    )
    assert review is not None
    for text in (
        render.working(hostile),
        render.receipt(hostile, 3),
        render.review_message(review, hostile, "t"),
    ):
        assert "<!channel>" not in text and "<!here>" not in text
        assert "<@U0ADMIN>" not in text and "<#C0SECRET" not in text
        assert "<https://evil.test" not in text and "https://evil.test" not in text
        assert "\x07" not in text
    message = render.review_message(review, hostile, "t")
    # The only angle bracket form left is our own "Open in Weaves" link.
    assert message.count("<") == 1 and "|Open in Weaves>" in message


def test_a_long_review_stays_within_one_slack_message() -> None:
    long = good(
        summary="s" * 700,
        question="q" * 300,
        findings=[{"file": "f" * 100 + ".py", "issue": "i" * 300, "severity": "low"}]
        * 5,
    )
    review = parse_review(json.dumps(long))
    assert review is not None
    text = render.review_message(review, STATS, "t")
    assert len(text) <= 3800 and text.rstrip().endswith("Open in Weaves>_")


def test_the_link_is_left_out_when_the_app_origin_is_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEAVES_PUBLIC_FRONTEND_URL", "javascript:alert(1)")
    assert render.task_link("t") == ""
    monkeypatch.setenv("WEAVES_PUBLIC_FRONTEND_URL", "https://app.example.test")
    assert render.task_link("t") == "<https://app.example.test/#tasks/t|Open in Weaves>"
    assert render.task_link("bad id|<x>") == ""
