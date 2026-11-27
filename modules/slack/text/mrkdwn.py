"""Deterministic markdown to Slack mrkdwn, with the sanitizing of design 3.7.

Model output is markdown; Slack wants mrkdwn. The conversion is plain code (no
model): bold, italics, strikethrough, headings, bullets, tables (monospace),
code fences and links. The same pass makes the text safe to post:

* ``<!channel>``, ``<!here>``, ``<@U...>``, ``<#C...>`` and every other Slack
  angle-bracket form in the input are neutralized (the sigil keeps a zero-width
  space so it cannot notify anyone), and ``&``, ``<``, ``>`` are escaped;
* a link survives only when its host is on the allow-list; any other link keeps
  its label and loses its URL (the host stays visible as plain text).

``fit`` caps a message at a limit without cutting inside a code fence and adds
the "read the full answer" note; ``split_fenced`` splits long text on line
boundaries and keeps every fence balanced.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

ZWSP = "​"
MAX_MESSAGE_CHARS = 3800
CAP_NOTE = "_The answer is longer than Slack allows. Read the full answer in Weaves._"

_MARK_OPEN, _MARK_CLOSE = "", ""
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANGLE = re.compile(r"<([!@#][^>\n]{0,200})>")
_FENCE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)(?:^[ \t]*```[ \t]*$|\Z)", re.S | re.M)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]{1,300})\]\((https?://[^\s)]{1,2000})\)")
_BARE_URL = re.compile(r"https?://[^\s<>()\[\]]{1,2000}")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$")
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+(.*)$")
_RULE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TABLE_RULE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$"
)


def escape(text: str) -> str:
    """Slack's three required escapes."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def neutralize_mentions(text: str) -> str:
    """Turn ``<!here>``, ``<@U1>``, ``<#C1|x>`` and friends into inert plain text."""

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        label = inner.split("|", 1)[-1] if "|" in inner else inner[1:]
        sigil = "@" if inner[0] == "!" else inner[0]
        return f"{sigil}{ZWSP}{label.lstrip('!@#^')}"

    return _ANGLE.sub(replace, text)


class _Slots:
    """Placeholders that protect finished fragments from later rewrites."""

    def __init__(self) -> None:
        self._items: list[str] = []

    def hold(self, text: str) -> str:
        self._items.append(text)
        return f"{_MARK_OPEN}{len(self._items) - 1}{_MARK_CLOSE}"

    def restore(self, text: str) -> str:
        pattern = re.compile(f"{_MARK_OPEN}(\\d+){_MARK_CLOSE}")
        for _ in range(4):  # slots may hold slots (bold around a link)
            text, hits = pattern.subn(lambda m: self._items[int(m.group(1))], text)
            if not hits:
                break
        return text


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _link(url: str, label: str | None, allowed: frozenset[str]) -> str:
    """A kept link as ``<url|label>``, or the label (and bare host) for the rest."""
    host = _host(url)
    if host in allowed:
        safe = escape(url)
        return f"<{safe}|{label.replace('|', '¦')}>" if label else f"<{safe}>"
    shown = escape(host + (urlsplit(url).path.rstrip("/") if not label else ""))
    return f"{label} ({host})" if label else shown


def _inline(text: str, allowed: frozenset[str]) -> str:
    """Convert one prose line; code spans and links are protected while rewriting."""
    slots = _Slots()
    text = _INLINE_CODE.sub(lambda m: slots.hold(f"`{escape(m.group(1))}`"), text)
    text = escape(neutralize_mentions(text))
    text = _LINK.sub(
        lambda m: slots.hold(_link(_unescape(m.group(2)), m.group(1), allowed)), text
    )
    text = _BARE_URL.sub(
        lambda m: slots.hold(_link(_unescape(m.group(0)), None, allowed)), text
    )
    text = _BOLD.sub(lambda m: slots.hold(f"*{m.group(1) or m.group(2)}*"), text)
    text = _STRIKE.sub(lambda m: f"~{m.group(1)}~", text)
    text = _ITALIC.sub(lambda m: f"_{m.group(1)}_", text)
    return slots.restore(text)


def _unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _prose_line(line: str, allowed: frozenset[str]) -> str:
    if _RULE.match(line):
        return "———"
    heading = _HEADING.match(line)
    if heading:
        body = re.sub(r"\*\*|__", "", heading.group(1))
        return f"*{_inline(body, allowed)}*"
    bullet = _BULLET.match(line)
    if bullet and not _RULE.match(line):
        indent = bullet.group(1).replace("\t", "    ")
        return f"{indent}• {_inline(bullet.group(2), allowed)}"
    if line.lstrip().startswith(">"):
        return "> " + _inline(line.lstrip()[1:].lstrip(), allowed)
    return _inline(line, allowed)


def _code(content: str) -> str:
    body = escape(neutralize_mentions(content)).replace("```", "'''").rstrip("\n")
    return f"```\n{body}\n```"


def _prose(text: str, allowed: frozenset[str]) -> str:
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        if _TABLE_ROW.match(line) and _TABLE_RULE.match(nxt):
            end = index
            while end < len(lines) and _TABLE_ROW.match(lines[end]):
                end += 1
            out.append(_code("\n".join(lines[index:end])))
            index = end
            continue
        out.append(_prose_line(line, allowed))
        index += 1
    return "\n".join(out)


def to_mrkdwn(markdown: str, *, allowed_hosts: Iterable[str] = ()) -> str:
    """Convert and sanitize ``markdown`` (deterministic, no I/O)."""
    allowed = frozenset(h.lower() for h in allowed_hosts)
    text = _CONTROL.sub("", markdown.replace("\r\n", "\n").replace("\r", "\n"))
    out: list[str] = []
    position = 0
    for match in _FENCE.finditer(text):
        out.append(_prose(text[position : match.start()], allowed))
        out.append(_code(match.group(1)))
        position = match.end()
    out.append(_prose(text[position:], allowed))
    joined = "".join(_join_blocks(out))
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


def _join_blocks(parts: list[str]) -> list[str]:
    """Keep a newline between prose and a code block on either side."""
    result: list[str] = []
    for part in parts:
        if not part:
            continue
        if result and not result[-1].endswith("\n") and not part.startswith("\n"):
            result.append("\n")
        result.append(part)
    return result


@dataclass(frozen=True)
class Fitted:
    """Text within the limit and whether it was cut."""

    text: str
    truncated: bool = False


def _open_fence(text: str) -> bool:
    return text.count("```") % 2 == 1


def fit(text: str, limit: int = MAX_MESSAGE_CHARS, note: str = CAP_NOTE) -> Fitted:
    """Cut at a line boundary (never inside a fence) and add the cap note."""
    if len(text) <= limit:
        return Fitted(text)
    budget = max(0, limit - len(note) - 2 - 4)  # room for the note and a fence
    head = text[:budget]
    cut = max(head.rfind("\n\n"), head.rfind("\n"), head.rfind(" "))
    head = (head[:cut] if cut > budget // 2 else head).rstrip()
    if _open_fence(head):
        head = f"{head}\n```"
    return Fitted(f"{head}\n\n{note}" if head else note, True)


def split_fenced(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split on line boundaries into chunks within ``limit``, each fence balanced."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    in_fence = False
    for line in text.split("\n"):
        pieces = [line[i : i + limit - 8] for i in range(0, len(line), limit - 8)] or [
            ""
        ]
        for piece in pieces:
            extra = len(piece) + 1
            if current and size + extra > limit - 4:
                chunks.append("\n".join(current) + ("\n```" if in_fence else ""))
                current = ["```"] if in_fence else []
                size = 4 if in_fence else 0
            current.append(piece)
            size += extra
            if piece.strip().startswith("```"):
                in_fence = not in_fence
    if current:
        chunks.append("\n".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


def has_unsafe_angle(text: str) -> bool:
    """True if ``text`` holds an angle bracket that is not one of our own links."""
    return re.search(r"<(?!https?://)", text) is not None
