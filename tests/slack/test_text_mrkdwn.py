"""Markdown to mrkdwn and the sanitizer of design 3.7 (deterministic, no model)."""

from __future__ import annotations

import random

import pytest

from modules.slack.text.mrkdwn import (
    CAP_NOTE,
    MAX_MESSAGE_CHARS,
    ZWSP,
    fit,
    has_unsafe_angle,
    neutralize_mentions,
    split_fenced,
    to_mrkdwn,
)

HOSTILE = [
    "<!channel> <!here> <!everyone> <!subteam^S0123|@oncall>",
    "<@U0ABC123> <@U0ABC123|bob> <#C0ABC123|general>",
    "[click](https://evil.example/phish) and https://evil.example/x?a=1&b=2",
    "<https://evil.example|totally safe> <mailto:a@b.c|mail>",
    "```\n<!channel>\n```",
    "`<!here>` inline",
    "&amp; <b>bold</b> &lt;script&gt;",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_input_never_produces_a_live_slack_construct(text: str) -> None:
    out = to_mrkdwn(text)
    assert not has_unsafe_angle(out)
    assert "<!" not in out and "<@" not in out and "<#" not in out
    assert "<http" not in out  # no live link with a disguised label


def test_mentions_are_neutralized_with_a_zero_width_space() -> None:
    assert neutralize_mentions("hi <!channel>") == f"hi @{ZWSP}channel"
    assert neutralize_mentions("<@U1|bob>") == f"@{ZWSP}bob"


def test_unlisted_links_lose_their_url_and_listed_hosts_are_kept() -> None:
    assert to_mrkdwn("see [docs](https://evil.example/a)") == "see docs (evil.example)"
    kept = to_mrkdwn(
        "see [docs](https://w.example/a?x=1&y=2)", allowed_hosts=["w.example"]
    )
    assert kept == "see <https://w.example/a?x=1&amp;y=2|docs>"
    assert to_mrkdwn("go https://evil.example/x") == "go evil.example/x"


def test_markdown_becomes_mrkdwn() -> None:
    text = "# Title\n**bold** and *it* and ~~old~~\n- one\n  - two\n---\n> quote"
    assert to_mrkdwn(text) == (
        "*Title*\n*bold* and _it_ and ~old~\n• one\n  • two\n———\n> quote"
    )


def test_tables_become_monospace_blocks_and_code_is_escaped() -> None:
    table = "| a | b |\n|---|---|\n| 1 | 2 |"
    assert to_mrkdwn(table) == f"```\n{table}\n```"
    assert (
        to_mrkdwn("```py\nx = a < b && c\n```") == "```\nx = a &lt; b &amp;&amp; c\n```"
    )


def test_conversion_is_deterministic() -> None:
    text = "**a** [b](https://x.example) `c` <!here>"
    assert to_mrkdwn(text) == to_mrkdwn(text)


def test_control_characters_are_dropped() -> None:
    assert to_mrkdwn("a\x00b\x07cd") == "abcd"


def test_fuzz_never_leaves_an_unsafe_angle() -> None:
    rng = random.Random(7)
    pieces = [
        "<",
        ">",
        "!",
        "@U1",
        "|",
        "[x](",
        ")",
        "```",
        "`",
        "*",
        "_",
        "&",
        "\n",
        "http://a.b/c",
        "<!here>",
    ]
    for _ in range(400):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 30)))
        assert not has_unsafe_angle(to_mrkdwn(text)), text


def test_fit_leaves_short_text_alone_and_caps_long_text_with_the_note() -> None:
    assert fit("short").text == "short" and not fit("short").truncated
    capped = fit("word " * 2000)
    assert capped.truncated and len(capped.text) <= MAX_MESSAGE_CHARS
    assert capped.text.endswith(CAP_NOTE)


def test_fit_never_cuts_inside_a_code_fence() -> None:
    capped = fit("```\n" + "line\n" * 2000 + "```")
    body = capped.text.removesuffix(CAP_NOTE)
    assert body.count("```") % 2 == 0 and len(capped.text) <= MAX_MESSAGE_CHARS


def test_split_keeps_every_chunk_within_the_limit_and_fences_balanced() -> None:
    chunks = split_fenced("intro\n```\n" + "code line\n" * 300 + "```\nafter", 500)
    assert len(chunks) > 3
    assert all(len(c) <= 500 for c in chunks)
    assert all(c.count("```") % 2 == 0 for c in chunks)


def test_split_breaks_a_single_oversized_line() -> None:
    chunks = split_fenced("x" * 1500, 500)
    assert all(len(c) <= 500 for c in chunks) and "".join(chunks) == "x" * 1500
