"""Strict validation of the model's review (fail closed on any deviation).

The model is asked for one JSON object; the runtime validates it against the
agent's schema and this module validates it again, so a reply that adds a key,
changes a type, exceeds a bound, uses an unknown risk word or is not JSON at all
never reaches Slack: ``parse_review`` returns ``None`` and the reader of that
result says "I couldn't produce a review. Try again."
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from modules.slack.review.prompt import (
    FILE_CHARS,
    ISSUE_CHARS,
    MAX_FINDINGS,
    QUESTION_CHARS,
    RISKS,
    SUMMARY_CHARS,
)

_KEYS = frozenset({"summary", "risk", "findings", "question"})
_FINDING_KEYS = frozenset({"file", "issue", "severity"})


@dataclass(frozen=True)
class Finding:
    """One finding: a file path, a sentence and a severity (all untrusted text)."""

    file: str = field(repr=False)
    issue: str = field(repr=False)
    severity: str


@dataclass(frozen=True)
class Review:
    """A validated review. The texts are still untrusted and are sanitized to render."""

    summary: str = field(repr=False)
    risk: str
    findings: tuple[Finding, ...] = field(repr=False)
    question: str = field(repr=False)


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if 1 <= len(cleaned) <= limit else None


def _finding(value: object) -> Finding | None:
    if not isinstance(value, dict) or set(value) != _FINDING_KEYS:
        return None
    file = _text(value["file"], FILE_CHARS)
    issue = _text(value["issue"], ISSUE_CHARS)
    severity = value["severity"]
    if file is None or issue is None or severity not in RISKS:
        return None
    return Finding(file, issue, str(severity))


def parse_review(content: str) -> Review | None:
    """The review in ``content``, or ``None`` when it is not exactly the contract."""
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != _KEYS:
        return None
    summary = _text(data["summary"], SUMMARY_CHARS)
    question = _text(data["question"], QUESTION_CHARS)
    raw = data["findings"]
    if (
        summary is None
        or question is None
        or data["risk"] not in RISKS
        or not isinstance(raw, list)
        or len(raw) > MAX_FINDINGS
    ):
        return None
    findings = [_finding(item) for item in raw]
    if any(item is None for item in findings):
        return None
    return Review(
        summary,
        str(data["risk"]),
        tuple(item for item in findings if item is not None),
        question,
    )
