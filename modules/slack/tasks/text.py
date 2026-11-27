"""Pure text helpers of the task flow: the title and the short result (no I/O).

The task title is the first line of the request, reduced to plain words. The
result message is a short summary, up to five bullets and a fixed pointer to the
full result in Weaves: it never tries to carry the whole result, so a long
result is always cut and the link says where the rest is. Everything goes
through the same markdown sanitizer as an Ask answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from modules.slack import copy
from modules.slack.text.mrkdwn import fit, to_mrkdwn

TITLE_CHARS = 80
SUMMARY_CHARS = 280
BULLET_CHARS = 140
MAX_BULLETS = 5
RESULT_LIMIT = 2900  # a Slack section block holds 3,000 characters
_MENTION_LEFTOVER = re.compile(r"@[UW][A-Z0-9]{2,31}\b")
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d{1,2}[.)])\s+(.+)$")
_HEADING = re.compile(r"^\s*#{1,6}\s+")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,}|\|.*\|)\s*$")
_INLINE_MARKS = re.compile(r"[*_~`]+")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def title_of(text: str) -> str:
    """A plain title from the first line of a request (never empty)."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    plain = _LINK.sub(r"\1", first)
    plain = _MENTION_LEFTOVER.sub("", plain)
    plain = _INLINE_MARKS.sub("", plain)
    plain = re.sub(r"[<>|]", "", plain)
    plain = re.sub(r"\s+", " ", plain).strip(" -:.\t")
    if not plain:
        return copy.TASK_DEFAULT_TITLE
    return _cut(plain, TITLE_CHARS)


def _cut(text: str, limit: int) -> str:
    """Cut at a word boundary with an ellipsis when over ``limit``."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    space = head.rfind(" ")
    return (head[:space] if space > limit // 2 else head).rstrip(" ,.;:") + "…"


@dataclass(frozen=True)
class Digest:
    """The short form of a result: one summary line and a few bullets."""

    summary: str
    bullets: tuple[str, ...]
    cut: bool  # something of the result is not shown here


def _plain(line: str) -> str:
    line = _LINK.sub(r"\1", line)
    return re.sub(r"\s+", " ", _INLINE_MARKS.sub("", line)).strip()


def digest(content: str) -> Digest:
    """Summary (first prose paragraph) and up to five bullets of ``content``."""
    summary = ""
    bullets: list[str] = []
    other_prose = 0
    in_fence = False
    for raw in content.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip() or _RULE.match(line):
            continue
        item = _BULLET.match(line)
        if item:
            bullets.append(_plain(item.group(1)))
            continue
        text = _plain(_HEADING.sub("", line))
        if not text:
            continue
        if not summary and not _HEADING.match(line):
            summary = text
        elif not _HEADING.match(line):
            other_prose += 1
    shown = [b for b in bullets if b][:MAX_BULLETS]
    cut = (
        len(bullets) > len(shown)
        or len(summary) > SUMMARY_CHARS
        or any(len(b) > BULLET_CHARS for b in shown)
        or (other_prose > 0 and not shown)
    )
    return Digest(
        _cut(summary, SUMMARY_CHARS),
        tuple(_cut(b, BULLET_CHARS) for b in shown),
        cut,
    )


def result_text(content: str) -> str:
    """The result message body: summary, bullets and the pointer to Weaves."""
    shown = digest(content)
    parts: list[str] = []
    if shown.summary:
        parts.append(shown.summary)
    if shown.bullets:
        parts.append("\n".join(f"- {bullet}" for bullet in shown.bullets))
    body = to_mrkdwn("\n\n".join(parts)) or copy.TASK_NO_TEXT
    capped = fit(body, RESULT_LIMIT - len(copy.TASK_FULL_RESULT) - 4, note="")
    return f"{capped.text.rstrip()}\n\n{copy.TASK_FULL_RESULT}"
