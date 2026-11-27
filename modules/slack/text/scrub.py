"""The egress scrubber: nothing leaves for Slack with a secret or card number.

Every outgoing message passes ``scrub`` after it was sanitized. The scrubber
replaces secrets and a few kinds of personal data with a fixed placeholder and
reports only counts and category names (never the matched text).

It fails **closed**: if anything inside the scrubber raises, ``ScrubberUnavailable``
is raised and the caller must not post. A message that cannot be checked is not
sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_SCRUB_CHARS = 100_000


class ScrubberUnavailable(Exception):
    """The scrubber could not check the text; the text must not be posted."""


@dataclass(frozen=True)
class Scrubbed:
    """Cleaned text plus what was removed (categories only, never the values)."""

    text: str
    redactions: int = 0
    categories: tuple[str, ...] = ()


_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
# Order matters: long structured secrets first, generic assignments last.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", _PRIVATE_KEY),
    ("slack_token", re.compile(r"\bxox[abprso]-[A-Za-z0-9-]{8,}")),
    ("slack_app_token", re.compile(r"\bxapp-[A-Za-z0-9-]{8,}")),
    (
        "slack_webhook",
        re.compile(
            r"https://hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9/_-]+"
        ),
    ),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("api_key", re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{20,}")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
    ),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    (
        "credential",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|"
            r"auth[_-]?token|client[_-]?secret)\b\s*[:=]\s*[\"']?[^\s\"',;]{6,}"
        ),
    ),
    ("url_credentials", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")),
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}\b")
_SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _redact_cards(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            count += 1
            return "[card number removed]"
        return match.group(0)

    return _CARD.sub(replace, text), count


def scrub(text: str, *, redact_email: bool = True) -> Scrubbed:
    """Remove secrets and personal data; raise ``ScrubberUnavailable`` on any error."""
    try:
        if len(text) > MAX_SCRUB_CHARS:
            raise ValueError("text too long to scrub")
        cleaned = text
        categories: list[str] = []
        total = 0
        for name, pattern in _SECRET_PATTERNS:
            cleaned, hits = pattern.subn(f"[{name} removed]", cleaned)
            if hits:
                total += hits
                categories.append(name)
        cleaned, hits = _redact_cards(cleaned)
        if hits:
            total += hits
            categories.append("card")
        cleaned, hits = _SSN.subn("[ssn removed]", cleaned)
        if hits:
            total += hits
            categories.append("ssn")
        if redact_email:
            cleaned, hits = _EMAIL.subn("[email removed]", cleaned)
            if hits:
                total += hits
                categories.append("email")
        return Scrubbed(cleaned, total, tuple(categories))
    except Exception as error:
        raise ScrubberUnavailable(type(error).__name__) from None
