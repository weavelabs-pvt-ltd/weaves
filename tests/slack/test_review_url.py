"""The strict pull request URL parser: one exact shape, hostile look-alikes refused."""

from __future__ import annotations

import pytest

from clients.integrations.github_pr_url import parse_pull_request_url
from modules.slack.classify import IntentKind, classify

BOT = "U0BOT"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://github.com/acme/api/pull/42", "acme/api#42"),
        ("Review pull request https://github.com/Acme/API/pull/42", "acme/api#42"),
        ("https://github.com/acme/api/pull/42/", "acme/api#42"),
        ("https://github.com/acme/api/pull/42/files", "acme/api#42"),
        ("https://github.com/acme/api/pull/42/commits", "acme/api#42"),
        ("https://github.com/acme/api/pull/42?diff=split", "acme/api#42"),
        ("https://github.com/acme/api/pull/42#discussion_r1", "acme/api#42"),
        ("see https://github.com/acme/api/pull/42.", "acme/api#42"),
        ("(https://github.com/acme/api/pull/42)", "acme/api#42"),
        ("https://GitHub.com/acme/api/pull/42", "acme/api#42"),
        (
            "https://github.com/acme/my.repo_x-1/pull/9999999",
            "acme/my.repo_x-1#9999999",
        ),
        (
            "https://github.com/a/b/pull/1 and again https://github.com/a/b/pull/1",
            "a/b#1",
        ),
    ],
)
def test_valid_links_give_the_lower_case_reference(text: str, expected: str) -> None:
    found = parse_pull_request_url(text)
    assert found is not None and found.ref == expected
    assert found.url == f"https://github.com/{expected.replace('#', '/pull/')}"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "review the payments PR",
        "http://github.com/acme/api/pull/42",
        "github.com/acme/api/pull/42",
        "https://www.github.com/acme/api/pull/42",
        "https://github.com.evil.test/acme/api/pull/42",
        "https://evil.test/github.com/acme/api/pull/42",
        "https://evil.test/?u=https://github.com/acme/api/pull/42",
        "https://github.com@evil.test/acme/api/pull/42",
        "https://user@github.com/acme/api/pull/42",
        "https://github.com:443/acme/api/pull/42",
        "https://github.com:8443/acme/api/pull/42",
        "https://gitlab.com/acme/api/pull/42",
        "https://github.com/acme/api/issues/42",
        "https://github.com/acme/api/pull/0",
        "https://github.com/acme/api/pull/042",
        "https://github.com/acme/api/pull/12345678",
        "https://github.com/acme/api/pull/-1",
        "https://github.com/acme/api/pull/4\uff12",  # a full-width digit
        "https://github.com/acme/api/pull/42/../../../etc/passwd",
        "https://github.com/acme/../pull/42",
        "https://github.com/acme/./pull/1",
        "https://github.com/acme/api/pull/42/%2e%2e/x",
        "https://github.com/ac%6de/api/pull/42",
        "https://github.com/-acme/api/pull/42",
        "https://github.com/acme/api.git/pull/42",
        "https://github.com/acme/api/pull/42/other",
        "https://github.com/acme/ap\u0131/pull/42",  # a dotless i
        "https://github.com/acme/api/pull/42​",
        "https://github.com/acme/api/pull/42 https://github.com/acme/api/pull/43",
        "https://github.com/acme/api/pull/42 https://evil.test/x",
        "https://github.com//acme/api/pull/42",
        "https://github.com/a/" + "b" * 101 + "/pull/1",
        "https://github.com/" + "a" * 40 + "/b/pull/1",
        "https://github.com/acme/api/pull/42\nhttps://github.com/x/y/pull/1",
    ],
)
def test_everything_else_is_refused(text: str) -> None:
    assert parse_pull_request_url(text) is None


def test_the_classifier_sends_a_bad_link_to_the_missing_detail_reply() -> None:
    for text in (
        "review https://github.com.evil.test/acme/api/pull/42",
        "review https://gitlab.com/a/b/pull/1",
        "review https://github.com/acme/api/pull/42 https://github.com/x/y/pull/1",
    ):
        intent = classify(f"<@{BOT}> {text}", BOT)
        assert intent.kind is IntentKind.REVIEW and intent.arg == ""
    good = classify(f"<@{BOT}> review <https://github.com/Acme/Api/pull/42|PR>", BOT)
    assert good.kind is IntentKind.REVIEW and good.arg == "acme/api#42"
