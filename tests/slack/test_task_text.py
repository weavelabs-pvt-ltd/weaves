"""The pure pieces of the task flow: titles, results, limits, links, buttons."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from contracts.v1.slack_access import ExecuteLimits
from modules.slack import copy
from modules.slack.access.execute_limits import check_execute_limits
from modules.slack.access.models import ExecuteUsage, Reason
from modules.slack.reply.poster import LinkButton, PostError, message_blocks
from modules.slack.tasks.links import permalink, workspace_origin
from modules.slack.tasks.text import (
    BULLET_CHARS,
    MAX_BULLETS,
    RESULT_LIMIT,
    SUMMARY_CHARS,
    digest,
    result_text,
    title_of,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("prepare the weekly KPI summary", "prepare the weekly KPI summary"),
        ("  *bold* `code` title  \nsecond line", "bold code title"),
        ("ping @U0ABC123 about [the doc](https://x.test/y)", "ping about the doc"),
        ("<!channel> <@U0VICTIM> hello", "!channel hello"),
        ("", copy.TASK_DEFAULT_TITLE),
        ("   \n  ", copy.TASK_DEFAULT_TITLE),
        ("***", copy.TASK_DEFAULT_TITLE),
    ],
)
def test_the_title_is_plain_words_from_the_first_line(text: str, title: str) -> None:
    assert title_of(text) == title


def test_a_long_title_is_cut_at_a_word_with_an_ellipsis() -> None:
    title = title_of("word " * 60)

    assert len(title) <= 80 and title.endswith("…") and " …" not in title


def test_the_digest_takes_the_first_prose_paragraph_and_five_bullets() -> None:
    content = "# Report\n\nActive teams grew.\n\nMore detail follows.\n\n" + "\n".join(
        f"{n}. Item {n}" for n in range(1, 9)
    )

    shown = digest(content)

    assert shown.summary == "Active teams grew."
    assert shown.bullets == tuple(f"Item {n}" for n in range(1, MAX_BULLETS + 1))
    assert shown.cut


def test_a_short_result_is_not_cut() -> None:
    shown = digest("Done.\n\n- one\n- two")

    assert (shown.summary, shown.bullets, shown.cut) == ("Done.", ("one", "two"), False)


def test_code_fences_and_tables_are_not_summaries() -> None:
    shown = digest("```\nsecret = 1\n```\n| a | b |\n|---|---|\nThe answer.")

    assert shown.summary == "The answer."


def test_long_lines_are_cut_with_an_ellipsis() -> None:
    shown = digest("x" * 900 + "\n- " + "y" * 900)

    assert len(shown.summary) <= SUMMARY_CHARS and shown.summary.endswith("…")
    assert len(shown.bullets[0]) <= BULLET_CHARS and shown.cut


def test_the_result_message_always_ends_with_the_pointer_to_weaves() -> None:
    assert result_text("Fine.").endswith(copy.TASK_FULL_RESULT)
    empty = result_text("")
    assert copy.TASK_NO_TEXT in empty and empty.endswith(copy.TASK_FULL_RESULT)
    assert len(result_text("word " * 5000)) < RESULT_LIMIT


def test_the_result_message_is_sanitized() -> None:
    text = result_text("Hello <!channel> and <@U0X1> & friends\n- a <b> c")

    assert "<!channel>" not in text and "<@U0X1>" not in text
    assert "&amp;" in text and "&lt;b&gt;" in text


# -- Execute limits ----------------------------------------------------------


def _stamps(count: int, minutes_ago: float = 1.0) -> tuple[datetime, ...]:
    return tuple(NOW - timedelta(minutes=minutes_ago, seconds=n) for n in range(count))


def test_within_the_limits_nothing_is_refused() -> None:
    usage = ExecuteUsage(user=_stamps(9), channel=_stamps(29))

    assert not check_execute_limits(ExecuteLimits(), usage, NOW).limited


def test_the_tenth_request_in_ten_minutes_is_refused_with_the_wait() -> None:
    verdict = check_execute_limits(ExecuteLimits(), ExecuteUsage(user=_stamps(10)), NOW)

    assert verdict.reason is Reason.RATE_LIMIT_USER
    assert 0 < verdict.retry_after_seconds <= 600


def test_the_channel_limit_is_thirty() -> None:
    verdict = check_execute_limits(
        ExecuteLimits(), ExecuteUsage(channel=_stamps(30)), NOW
    )

    assert verdict.reason is Reason.RATE_LIMIT_CHANNEL


def test_old_requests_fall_out_of_the_window() -> None:
    usage = ExecuteUsage(user=_stamps(10, minutes_ago=11))

    assert not check_execute_limits(ExecuteLimits(), usage, NOW).limited


def test_the_longer_wait_wins() -> None:
    usage = ExecuteUsage(user=_stamps(10, minutes_ago=9), channel=_stamps(30, 1))

    verdict = check_execute_limits(ExecuteLimits(), usage, NOW)

    assert verdict.reason is Reason.RATE_LIMIT_CHANNEL


def test_the_limits_have_bounds() -> None:
    with pytest.raises(ValueError):
        ExecuteLimits(user_per_10_min=0)
    with pytest.raises(ValueError):
        ExecuteLimits(channel_per_10_min=10_001)


# -- links -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://acme.slack.com/", "https://acme.slack.com/"),
        ("https://Acme-Test.slack.com/foo?x=1", "https://acme-test.slack.com/"),
        ("https://acme.enterprise.slack.com/", "https://acme.enterprise.slack.com/"),
        ("http://acme.slack.com/", None),
        ("https://slack.com/", None),
        ("https://acme.slack.com.evil.io/", None),
        ("https://evil.io/acme.slack.com/", None),
        ("https://user@acme.slack.com/", None),
        ("https://acme.slack.com:8443/", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_only_a_slack_workspace_address_is_accepted(url, origin) -> None:
    assert workspace_origin(url) == origin


def test_the_permalink_opens_the_thread() -> None:
    link = permalink("https://acme.slack.com/", "C0CHAN001", "1699999001.000100")

    assert link == (
        "https://acme.slack.com/archives/C0CHAN001/p1699999001000100"
        "?thread_ts=1699999001.000100&cid=C0CHAN001"
    )


@pytest.mark.parametrize(
    ("channel", "ts"),
    [("c0chan", "1.2"), ("C0CHAN001", "not-a-ts"), ("C0/../X", "1699999001.000100")],
)
def test_a_malformed_thread_gets_no_permalink(channel: str, ts: str) -> None:
    assert permalink("https://acme.slack.com/", channel, ts) is None


# -- the link button ---------------------------------------------------------


def test_a_message_with_a_button_has_a_section_and_an_actions_block() -> None:
    blocks = message_blocks("hello", LinkButton("View task", "https://app.test/#t/1"))

    assert blocks[0]["text"] == {"type": "mrkdwn", "text": "hello"}
    (element,) = blocks[1]["elements"]
    assert element["url"] == "https://app.test/#t/1"
    assert element["text"]["text"] == "View task"


def test_a_message_without_a_button_replaces_old_blocks_with_one_section() -> None:
    assert message_blocks("plain", None) == [
        {"type": "section", "text": {"type": "mrkdwn", "text": "plain"}}
    ]


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "ftp://x.test/", "https://u:p@x.test/", "https://"]
)
def test_a_button_url_must_be_a_plain_http_link(url: str) -> None:
    with pytest.raises(PostError):
        message_blocks("x", LinkButton("Open", url))
