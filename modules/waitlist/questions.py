"""Versioned, immutable waitlist question sets and consent texts.

Every published version is one file under ``contracts/waitlist`` and is pinned
by ``published.lock.json`` (a SHA-256 of the file's bytes). Startup refuses to
run when a published file was edited, when a file is missing from the lock, or
when a set breaks the schema, so an answer always keeps the exact question it
was given. A new version is a new file plus a new lock entry, never an edit.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from modules.waitlist.errors import InvalidInput

CONTENT_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "waitlist"
LOCK_NAME = "published.lock.json"
MAX_QUESTIONS = 6
OTHER_OPTION_ID = "other"
OTHER_MAX_LENGTH = 60
SHORT_TEXT_MAX_LENGTH = 280

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,40}$")]


class QuestionSetError(RuntimeError):
    """A question set or consent text is invalid, unpinned or was edited."""


class QuestionType(StrEnum):
    """How a question is asked and answered."""

    SINGLE_CHOICE = "single_choice"
    MULTIPLE_CHOICE = "multiple_choice"
    SIZE_BAND = "size_band"
    SHORT_TEXT = "short_text"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class QuestionOption(_Strict):
    """One choice, with an optional second line of explanation."""

    id: Identifier
    label: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, min_length=1, max_length=200)


class Question(_Strict):
    """One question as the website renders it."""

    id: Identifier
    type: QuestionType
    prompt: str = Field(min_length=1, max_length=200)
    helper: str | None = Field(default=None, min_length=1, max_length=200)
    summary_label: str = Field(min_length=1, max_length=60)
    optional: Literal[True]
    options: tuple[QuestionOption, ...] | None = None
    allow_other: bool = False
    other_max_length: Literal[60] | None = None
    max_length: Literal[280] | None = None

    @model_validator(mode="after")
    def shape_matches_type(self) -> Question:
        """Choice questions list options; short text lists none and has a cap."""
        if self.type is QuestionType.SHORT_TEXT:
            if self.options or self.allow_other or self.other_max_length:
                raise ValueError("short_text takes no options")
            if self.max_length != SHORT_TEXT_MAX_LENGTH:
                raise ValueError("short_text needs max_length 280")
            return self
        if self.max_length is not None:
            raise ValueError("only short_text has max_length")
        options = self.options or ()
        if not 2 <= len(options) <= 20:
            raise ValueError("choice questions need 2 to 20 options")
        if len({option.id for option in options}) != len(options):
            raise ValueError("option ids must be unique")
        self._check_other(options)
        return self

    def _check_other(self, options: tuple[QuestionOption, ...]) -> None:
        """``allow_other`` needs an ``other`` option and its 60-character cap."""
        if not self.allow_other:
            if self.other_max_length is not None:
                raise ValueError("other_max_length needs allow_other")
            return
        if OTHER_OPTION_ID not in {option.id for option in options}:
            raise ValueError("allow_other needs an option with id 'other'")
        if self.other_max_length != OTHER_MAX_LENGTH:
            raise ValueError("allow_other needs other_max_length 60")

    def option_ids(self) -> frozenset[str]:
        """The ids a choice answer may use."""
        return frozenset(option.id for option in self.options or ())

    def public(self) -> dict[str, Any]:
        """The JSON the website receives."""
        return self.model_dump(mode="json", exclude_none=True, exclude_defaults=True)


class QuestionSet(_Strict):
    """An immutable, numbered list of at most six questions."""

    version: int = Field(ge=1)
    questions: tuple[Question, ...] = Field(min_length=1, max_length=MAX_QUESTIONS)

    @model_validator(mode="after")
    def ids_are_unique(self) -> QuestionSet:
        """A question id names one question inside its set."""
        ids = [question.id for question in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("question ids must be unique")
        return self

    def find(self, question_id: str) -> Question | None:
        """The question with this id, or None."""
        return next((q for q in self.questions if q.id == question_id), None)

    def public(self) -> dict[str, Any]:
        """The JSON served by ``GET /questions``."""
        return {
            "version": self.version,
            "questions": [question.public() for question in self.questions],
        }


class ConsentText(_Strict):
    """The consent wording shown at join, under a version number."""

    version: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=1000)


def digest(path: Path) -> str:
    """SHA-256 of a published file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QuestionSetError(f"{path.name} is not readable JSON") from exc


def published_files(root: Path) -> dict[str, Path]:
    """Every question set and consent file under the content root, by name."""
    found = {
        f"{folder}/{path.name}": path
        for folder in ("questions", "consent")
        for path in sorted((root / folder).glob("*.json"))
    }
    if not found:
        raise QuestionSetError("no waitlist question sets are published")
    return found


def _verify_lock(root: Path, files: dict[str, Path]) -> None:
    locked = _load_json(root / LOCK_NAME).get("files", {})
    if set(locked) != set(files):
        raise QuestionSetError("published.lock.json does not match the files")
    for name, path in files.items():
        if digest(path) != locked[name]:
            raise QuestionSetError(f"published file {name} was edited")


def _version_of(path: Path) -> int:
    match = re.fullmatch(r"v([1-9][0-9]*)\.json", path.name)
    if match is None:
        raise QuestionSetError(f"{path.name} must be named v<number>.json")
    return int(match.group(1))


def consent_id(version: int) -> str:
    """The public id of a consent text version: ``v1``, ``v2``..."""
    return f"v{version}"


def consent_number(identifier: str) -> int:
    """Read a consent id such as ``v1``; anything else is a 422."""
    match = re.fullmatch(r"v([1-9][0-9]{0,5})", identifier)
    if match is None:
        raise InvalidInput("Unknown consent version")
    return int(match.group(1))


@dataclass(frozen=True)
class QuestionBank:
    """Every published question set and consent text, keyed by version."""

    sets: dict[int, QuestionSet]
    consents: dict[int, ConsentText]

    @classmethod
    def load(cls, root: Path | None = None) -> QuestionBank:
        """Load and strictly validate every file; raise on any problem."""
        base = root or CONTENT_ROOT
        files = published_files(base)
        _verify_lock(base, files)
        sets: dict[int, QuestionSet] = {}
        consents: dict[int, ConsentText] = {}
        for name, path in files.items():
            model: type[QuestionSet] | type[ConsentText] = (
                QuestionSet if name.startswith("questions/") else ConsentText
            )
            try:
                item = model.model_validate_json(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise QuestionSetError(f"{name} is invalid: {exc}") from exc
            if item.version != _version_of(path):
                raise QuestionSetError(f"{name} version does not match its name")
            if isinstance(item, QuestionSet):
                sets[item.version] = item
            else:
                consents[item.version] = item
        if not sets or not consents:
            raise QuestionSetError("a question set and a consent text are required")
        return cls(sets=sets, consents=consents)

    @property
    def active(self) -> QuestionSet:
        """The set new submissions are asked."""
        return self.sets[max(self.sets)]

    @property
    def active_consent(self) -> ConsentText:
        """The consent text the website shows now."""
        return self.consents[max(self.consents)]

    def question_set(self, version: int) -> QuestionSet | None:
        """A published set by version, or None."""
        return self.sets.get(version)
