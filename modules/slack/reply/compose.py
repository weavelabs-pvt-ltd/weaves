"""Compose the text of a reply from a finished turn (no I/O, no Slack).

An answer is the model's markdown converted to mrkdwn, then a short footer of
italic lines: where it came from (document *titles* only, never links or
excerpts), the sources count and elapsed time, and the partial-mode note. The
footer is reserved before the body is capped, so the cap note can never push the
footer out. A refusal is the pipeline's own fixed text; a failure is a fixed line
with a short reference, never the error text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from contracts.v1 import TaskTurn
from modules.slack import copy
from modules.slack.text.mrkdwn import MAX_MESSAGE_CHARS, fit, to_mrkdwn

_TITLE_NOISE = re.compile(r"[_*~`<>|\[\]\n\r\t]+")


@dataclass(frozen=True)
class Reply:
    """What to send: the text, who may see it and what kind of reply it is."""

    kind: str  # answer | refusal | failure | cancelled
    text: str
    ephemeral: bool = False
    sources: int = 0


def short_ref(turn_id: str) -> str:
    """A six-character reference a person can quote (it appears in our logs)."""
    cleaned = re.sub(r"[^a-z0-9]", "", turn_id.lower())
    return cleaned[-6:] or "000000"


def clean_title(title: str) -> str:
    """A document title reduced to plain, short text."""
    return _TITLE_NOISE.sub(" ", title).strip()[:60]


def _footer(
    titles: Sequence[str], sources: int, elapsed_seconds: float, skipped: str | None
) -> str:
    cleaned = [t for t in (clean_title(x) for x in titles) if t]
    lines = [f"_{copy.evidence_line(cleaned, sources)}_"]
    timing = copy.duration(elapsed_seconds)
    if sources:
        lines.append(f"_{copy.sources_count(sources)} · {timing}_")
    else:
        lines[0] = f"_{copy.evidence_line(cleaned, 0)} · {timing}_"
    if skipped:
        lines.append(f"_{_TITLE_NOISE.sub(' ', skipped).strip()}_")
    return "\n".join(lines)


def answer(
    content: str,
    *,
    titles: Iterable[str],
    sources: int,
    elapsed_seconds: float,
    skipped_note: str | None,
    allowed_hosts: Iterable[str] = (),
) -> str:
    """The answer message: body, evidence line, ``3 sources · 41s``, skipped note."""
    footer = _footer(list(titles), sources, elapsed_seconds, skipped_note)
    body = to_mrkdwn(content, allowed_hosts=allowed_hosts) or "(no text)"
    capped = fit(body, MAX_MESSAGE_CHARS - len(footer) - 2)
    return f"{capped.text}\n\n{footer}"


def refusal(message: str | None, outcome: str) -> Reply:
    """A guard refusal: the fixed text (private), with its standard emoji."""
    text = message or "Weaves is unavailable right now. Try again later."
    return Reply("refusal", copy.refusal(text, outcome), ephemeral=True)


def failure(turn: TaskTurn) -> Reply:
    """A failed turn: no model text, no error text, only a reference."""
    error = turn.error
    if error is not None and error.code.startswith("model.") and not error.retryable:
        return Reply("failure", copy.NO_MODEL, ephemeral=True)
    return Reply("failure", copy.failure(short_ref(turn.turn_id)))
