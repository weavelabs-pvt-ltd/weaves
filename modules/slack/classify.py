"""Classify an ``app_mention`` by fixed grammar (design 3.5): code, never a model.

Order matters and an unclear message is always **Ask**, never Execute:

* empty after removing our own mention: ``HELP``;
* a bare greeting or thanks: ``GREETING``;
* ``task: <text>``, ``review <PR url>``, ``run <workflow>``, ``status``,
  ``stop``, ``retry``: the text commands; this slice recognizes them and the
  dispatcher replies politely (they run in later slices);
* anything else: ``ASK`` with the cleaned question.

The question text is excluded from ``repr`` so a stray log line cannot leak it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from clients.integrations.github_pr_url import parse_pull_request_url


class IntentKind(StrEnum):
    """What the mention is, as far as the fixed grammar can tell."""

    HELP = "help"
    GREETING = "greeting"
    ASK = "ask"
    TASK = "task"
    REVIEW = "review"
    RUN = "run"
    STATUS = "status"
    STOP = "stop"
    RETRY = "retry"


@dataclass(frozen=True)
class Intent:
    """The kind, plus the text the kind needs (never logged)."""

    kind: IntentKind
    text: str = field(default="", repr=False)
    # ``task``: the task text; ``review``: the PR URL; ``run``: the workflow name.
    arg: str = field(default="", repr=False)
    thanks: bool = False

    @property
    def is_execute(self) -> bool:
        """The tier-2 commands that persist work (they need an admin's rule)."""
        return self.kind in (IntentKind.TASK, IntentKind.REVIEW, IntentKind.RUN)


_SLACK_LINK = re.compile(r"<(https?://[^|>\s]+)(?:\|[^>]*)?>")
_MENTION = re.compile(r"<@([A-Z0-9]{2,32})(?:\|[^>]*)?>")
_WORDS = re.compile(r"[a-z']+")
_WORKFLOW = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_GREETINGS = frozenset(
    {
        "hi",
        "hello",
        "hey",
        "hiya",
        "yo",
        "howdy",
        "hola",
        "good morning",
        "good afternoon",
        "good evening",
        "morning",
        "hi there",
        "hello there",
        "hey there",
    }
)
_THANKS = frozenset({"thanks", "thank you", "thx", "ty", "cheers", "thanks a lot"})
_HELP_WORDS = frozenset({"help", "?", "what can you do", "how do i use you"})


def clean_text(raw: str, bot_user_id: str) -> str:
    """The mention text without our own mention, links unwrapped, spaces collapsed."""

    def other(match: re.Match[str]) -> str:
        return "" if match.group(1) == bot_user_id else f"@{match.group(1)}"

    text = _MENTION.sub(other, raw or "")
    text = _SLACK_LINK.sub(lambda m: m.group(1), text)
    return re.sub(r"\s+", " ", text).strip()


def parse_pr_url(text: str) -> str | None:
    """``owner/name#number`` (lower case) of the one pull request link in ``text``.

    Strict (see ``github_pr_url``): any other link, a look-alike host or two
    different pull requests make it ``None``.
    """
    found = parse_pull_request_url(text)
    return found.ref if found is not None else None


def _mentions_link(text: str) -> bool:
    lowered = text.lower()
    return "http" in lowered or "github.com" in lowered


def _bare(text: str) -> str:
    return " ".join(_WORDS.findall(text.lower()))


def _command(text: str, bare: str) -> Intent | None:
    """The text commands (``task:``, ``review``, ``run``, ``status``, stop, retry)."""
    if re.match(r"task\s*:", text.lower()):
        return Intent(IntentKind.TASK, text, text.split(":", 1)[1].strip())
    head, _, rest = text.partition(" ")
    rest, verb = rest.strip(), head.lower()
    if verb == "review":
        url = parse_pr_url(rest)
        if (
            url
            or _mentions_link(rest)
            or _bare(rest) in ("", "pr", "a pr", "this", "this pr")
        ):
            return Intent(IntentKind.REVIEW, text, url or "")
    if verb == "run" and (not rest or _WORKFLOW.fullmatch(rest) is not None):
        return Intent(IntentKind.RUN, text, rest)
    simple = {
        IntentKind.STATUS: ("status", "status please"),
        IntentKind.STOP: ("stop", "cancel", "stop it"),
        IntentKind.RETRY: ("retry", "try again"),
    }
    for kind, words in simple.items():
        if bare in words:
            return Intent(kind, text)
    return None


def classify(raw_text: str | None, bot_user_id: str) -> Intent:
    """Classify one mention (see the module docstring for the order)."""
    text = clean_text(raw_text or "", bot_user_id)
    bare = _bare(text)
    if not text or bare in _HELP_WORDS or text == "?":
        return Intent(IntentKind.HELP)
    if bare in _GREETINGS:
        return Intent(IntentKind.GREETING, text)
    if bare in _THANKS:
        return Intent(IntentKind.GREETING, text, thanks=True)
    return _command(text, bare) or Intent(IntentKind.ASK, text)
