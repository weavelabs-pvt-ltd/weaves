"""Validate one waitlist answer against its question."""

from __future__ import annotations

from typing import Any

from contracts.v1 import WaitlistAnswer
from modules.waitlist.errors import InvalidInput
from modules.waitlist.questions import (
    OTHER_MAX_LENGTH,
    OTHER_OPTION_ID,
    Question,
    QuestionType,
)

_BAD_CONTROL = set(range(0, 9)) | {11, 12} | set(range(14, 32)) | {127}


def _clean_text(value: Any, limit: int, what: str) -> str:
    """Strip a text answer and refuse control characters and overlong text."""
    if not isinstance(value, str):
        raise InvalidInput(f"{what} must be text")
    text = value.strip()
    if any(ord(char) in _BAD_CONTROL for char in text):
        raise InvalidInput(f"{what} contains unsupported characters")
    if len(text) > limit:
        raise InvalidInput(f"{what} must be at most {limit} characters")
    return text


def _one_option(question: Question, value: Any) -> str:
    if not isinstance(value, str) or value not in question.option_ids():
        raise InvalidInput("Unknown option for this question")
    return value


def _many_options(question: Question, value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise InvalidInput("Choose at least one option")
    if len(set(value)) != len(value):
        raise InvalidInput("Options must not repeat")
    return [_one_option(question, item) for item in value]


def _other_text(question: Question, chosen: list[str], other: str | None) -> str | None:
    """The Other text: required when Other is chosen on an ``allow_other`` question.

    Sets published before a question asked what Other is (``allow_other``
    false) keep accepting a bare ``other`` and also accept the text, so a
    session started under that set can continue with the current questions.
    """
    if other is None:
        if question.allow_other and OTHER_OPTION_ID in chosen:
            raise InvalidInput("Say what Other is")
        return None
    if OTHER_OPTION_ID not in chosen:
        raise InvalidInput("Other text needs the Other option")
    text = _clean_text(other, OTHER_MAX_LENGTH, "Other text")
    if not text:
        if question.allow_other:
            raise InvalidInput("Say what Other is")
        return None
    return text


def validate_answer(
    question: Question, value: Any, other: str | None = None
) -> WaitlistAnswer:
    """Return the stored form of an answer or raise ``ValueError`` (HTTP 422)."""
    if question.type is QuestionType.SHORT_TEXT:
        if other is not None:
            raise InvalidInput("This question has no Other text")
        text = _clean_text(value, question.max_length or 0, "Answer")
        if not text:
            raise InvalidInput("Answer must not be empty")
        return WaitlistAnswer(value=text)
    if question.type is QuestionType.MULTIPLE_CHOICE:
        chosen = _many_options(question, value)
        return WaitlistAnswer(value=chosen, other=_other_text(question, chosen, other))
    single = _one_option(question, value)
    return WaitlistAnswer(value=single, other=_other_text(question, [single], other))
