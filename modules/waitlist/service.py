"""Public waitlist use cases: join, resume, answer, clear, retention.

Nothing here touches accounts, organizations, users, sessions, sign-in or the
allowlist. Every storage call runs under the narrow ``waitlist`` system scope,
which row-level security limits to the two waitlist collections.

One email is one entry (its identifier is derived from the email, so the
primary key and a unique index both enforce it) and every join is a new
submission. A submission is reached only through its private token, stored as a
SHA-256 digest. Joining again never reveals or overwrites earlier answers.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import RLock
from typing import Any

from application.ports.repository import Repository
from application.ports.throttling import LoginRateLimiter
from contracts.v1 import (
    WaitlistAnswer,
    WaitlistEntry,
    WaitlistSource,
    WaitlistSubmission,
)
from modules.waitlist.answers import validate_answer
from modules.waitlist.emails import domain_of, domain_type, entry_id, normalize_email
from modules.waitlist.errors import InvalidInput
from modules.waitlist.questions import QuestionBank, QuestionSet, consent_id
from shared.clock import new_id, utc_now
from shared.errors import LoginThrottled
from shared.login_buckets import login_bucket_key
from shared.request_context import system_scope

SCOPE = "waitlist"
TOKEN_PREFIX = "wlt_"
TOKEN_LIFETIME = timedelta(days=30)
RETENTION_ENV = "WEAVES_WAITLIST_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 365
_TOKEN_SHAPE = re.compile(r"^wlt_[A-Za-z0-9_-]{43}$")
_HOUR = 3600


class InvalidToken(Exception):
    """The resume token is missing, malformed, unknown or expired."""


class QuestionVersionChanged(Exception):
    """The client's question set is not the submission's or the active one."""

    def __init__(self, submission_version: int, active_version: int) -> None:
        """Remember both versions so the client can reload the right set."""
        super().__init__("question set version changed")
        self.submission_version = submission_version
        self.active_version = active_version


class UnknownQuestionSet(LookupError):
    """No published question set has that version."""


@dataclass(frozen=True)
class WaitlistLimits:
    """The per-purpose request budgets, all on the shared database limiter."""

    join_source: LoginRateLimiter
    join_email: LoginRateLimiter
    token: LoginRateLimiter
    token_source: LoginRateLimiter
    questions: LoginRateLimiter

    @classmethod
    def from_limiter(cls, base: LoginRateLimiter) -> WaitlistLimits:
        """Derive the budgets.

        Join is 10/hour per source and 5/hour per email, answers 60/hour per
        token with a looser per-source guard on token calls, and the question
        set 60/minute per source.
        """
        return cls(
            join_source=base.sibling(limit=10, window_seconds=_HOUR),
            join_email=base.sibling(limit=5, window_seconds=_HOUR),
            token=base.sibling(limit=60, window_seconds=_HOUR),
            token_source=base.sibling(limit=300, window_seconds=_HOUR),
            questions=base.sibling(limit=60, window_seconds=60),
        )


@dataclass(frozen=True)
class JoinCommand:
    """A validated join request."""

    email: str
    consent_version: int
    source: WaitlistSource
    question_version: int | None = None
    idempotency_key: str | None = None
    honeypot_filled: bool = False


@dataclass(frozen=True)
class SubmissionView:
    """What a token holder sees: their own version and answers only."""

    question_version: int
    answers: dict[str, WaitlistAnswer] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        """The JSON body of every answer endpoint."""
        return {
            "question_version": self.question_version,
            "answers": {
                key: answer.model_dump(
                    mode="json", exclude_none=True, exclude={"schema_version"}
                )
                for key, answer in self.answers.items()
            },
        }


@dataclass(frozen=True)
class JoinResult:
    """The identical join response for a new and a known email."""

    token: str
    view: SubmissionView

    def public(self) -> dict[str, Any]:
        """The 201 body."""
        return {"token": self.token, **self.view.public()}


def retention_days() -> int:
    """Days a submission may sit untouched; a bad value keeps the default."""
    try:
        return max(1, int(os.environ.get(RETENTION_ENV, DEFAULT_RETENTION_DAYS)))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def token_hash(token: str) -> str:
    """The stored form of a resume token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _idempotency_digest(email: str, key: str) -> str:
    material = f"weaves-waitlist-key-v1\0{email}\0{key}"
    return hashlib.sha256(material.encode()).hexdigest()


class WaitlistService:
    """Run the public waitlist over its two global repositories."""

    def __init__(
        self,
        *,
        entries: Repository[WaitlistEntry],
        submissions: Repository[WaitlistSubmission],
        bank: QuestionBank,
        limits: WaitlistLimits,
        unit_of_work: Callable[[], AbstractContextManager[None]],
        advisory_transaction_lock: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind the repositories, question bank, limiters and transaction."""
        self.entries = entries
        self.submissions = submissions
        self.bank = bank
        self._limits = limits
        self._unit_of_work = unit_of_work
        self._advisory_lock = advisory_transaction_lock
        self._clock = clock
        self._process_lock = RLock()

    # -- questions -----------------------------------------------------

    def questions(self, version: int | None, address: str) -> dict[str, Any]:
        """The question set (the active one unless a version is asked for)."""
        self._spend(self._limits.questions, "waitlist-questions", address)
        chosen: QuestionSet | None = (
            self.bank.active if version is None else self.bank.question_set(version)
        )
        if chosen is None:
            raise UnknownQuestionSet(str(version))
        consent = self.bank.active_consent
        return {
            **chosen.public(),
            "consent": {"version": consent_id(consent.version), "text": consent.text},
        }

    # -- join ----------------------------------------------------------

    def join(self, command: JoinCommand, address: str) -> JoinResult:
        """Save a spot: identical result for a new and a known email."""
        self._spend(self._limits.join_source, "waitlist-join-source", address)
        if command.honeypot_filled:
            view = SubmissionView(self.bank.active.version)
            return JoinResult(self._new_token(), view)
        email = normalize_email(command.email)
        if command.consent_version not in self.bank.consents:
            raise InvalidInput("Unknown consent version")
        active = self.bank.active.version
        if command.question_version not in (None, active):
            raise QuestionVersionChanged(command.question_version or 0, active)
        self._spend(self._limits.join_email, "waitlist-join-email", email)
        return self._store_join(command, email)

    def _store_join(self, command: JoinCommand, email: str) -> JoinResult:
        now = self._clock()
        key = entry_id(email)
        with self.transaction(f"waitlist-entry:{key}"):
            entry = self._entry_for(key, email, now)
            replay = self._replayed(entry, email, command.idempotency_key)
            token = self._new_token()
            if replay is not None:
                submission = replay.model_copy(
                    update={
                        "token_hash": token_hash(token),
                        "token_expires_at": now + TOKEN_LIFETIME,
                        "updated_at": now,
                    }
                )
                self.submissions.put(submission)
                return JoinResult(token, self._view(submission))
            submission = WaitlistSubmission(
                id=new_id(),
                entry_id=entry.id,
                token_hash=token_hash(token),
                token_expires_at=now + TOKEN_LIFETIME,
                question_version=self.bank.active.version,
                consent_version=command.consent_version,
                consented_at=now,
                source=command.source,
                idempotency_digest=(
                    _idempotency_digest(email, command.idempotency_key)
                    if command.idempotency_key
                    else None
                ),
                created_at=now,
                updated_at=now,
            )
            self.submissions.create(submission)
            self.entries.put(
                entry.model_copy(
                    update={
                        "last_joined_at": now,
                        "updated_at": now,
                        "submission_count": entry.submission_count + 1,
                    }
                )
            )
            return JoinResult(token, self._view(submission))

    def _entry_for(self, key: str, email: str, now: datetime) -> WaitlistEntry:
        """The email's entry, created with no submissions counted yet."""
        try:
            return self.entries.get(key)
        except KeyError:
            domain = domain_of(email)
            return self.entries.create(
                WaitlistEntry(
                    id=key,
                    email=email,
                    email_domain=domain,
                    domain_type=domain_type(domain),
                    first_joined_at=now,
                    last_joined_at=now,
                    submission_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )

    def _replayed(
        self, entry: WaitlistEntry, email: str, key: str | None
    ) -> WaitlistSubmission | None:
        """The submission an earlier request with this key created, if any."""
        if not key:
            return None
        wanted = _idempotency_digest(email, key)
        return next(
            (
                item
                for item in self.submissions.list_by_field("entry_id", entry.id)
                if item.idempotency_digest == wanted
            ),
            None,
        )

    # -- token holder --------------------------------------------------

    def current(self, token: str, address: str) -> SubmissionView:
        """The caller's own question version and answers."""
        with system_scope(SCOPE):
            return self._view(self._authenticate(token, address))

    def save_answer(
        self,
        token: str,
        question_id: str,
        value: Any,
        other: str | None,
        asserted_version: int | None,
        address: str,
    ) -> SubmissionView:
        """Save or change one answer, validated against the submission's set."""
        with system_scope(SCOPE):
            submission = self._authenticate(token, address)
            question = self._question(submission, question_id, asserted_version)
            answer = validate_answer(question, value, other)
            return self._rewrite(submission, question_id, answer)

    def clear_answer(
        self,
        token: str,
        question_id: str,
        asserted_version: int | None,
        address: str,
    ) -> SubmissionView:
        """Skip a question again after answering it."""
        with system_scope(SCOPE):
            submission = self._authenticate(token, address)
            self._question(submission, question_id, asserted_version)
            return self._rewrite(submission, question_id, None)

    def _question(
        self, submission: WaitlistSubmission, question_id: str, asserted: int | None
    ) -> Any:
        if asserted is not None and asserted != submission.question_version:
            raise QuestionVersionChanged(
                submission.question_version, self.bank.active.version
            )
        found = self.bank.sets[submission.question_version].find(question_id)
        if found is None:
            raise InvalidInput("Unknown question")
        return found

    def _rewrite(
        self, submission: WaitlistSubmission, question_id: str, answer: Any
    ) -> SubmissionView:
        """Apply one change to the freshest copy of the submission."""
        with self.transaction(f"waitlist-submission:{submission.id}"):
            fresh = self.submissions.get(submission.id)
            answers = dict(fresh.answers)
            if answer is None:
                answers.pop(question_id, None)
            else:
                answers[question_id] = answer
            saved = fresh.model_copy(
                update={"answers": answers, "updated_at": self._clock()}
            )
            self.submissions.put(saved)
            return self._view(saved)

    def _authenticate(self, token: str, address: str) -> WaitlistSubmission:
        """Throttle, then resolve a token to its live submission."""
        self._spend(self._limits.token_source, "waitlist-token-source", address)
        if not isinstance(token, str) or not _TOKEN_SHAPE.match(token):
            raise InvalidToken
        digest = token_hash(token)
        self._spend(self._limits.token, "waitlist-token", digest)
        found = self.submissions.find_by_field("token_hash", digest)
        if found is None or found.token_expires_at <= self._clock():
            raise InvalidToken
        return found

    # -- retention -----------------------------------------------------

    def purge_stale(self, days: int | None = None, limit: int = 500) -> tuple[int, int]:
        """Delete submissions untouched for ``days`` and entries left empty.

        Returns (submissions deleted, entries deleted). Bounded per call.
        """
        cutoff = self._clock() - timedelta(days=days or retention_days())
        removed_submissions = removed_entries = 0
        with system_scope(SCOPE):
            affected: set[str] = set()
            page = self.submissions.list_before("updated_at", cutoff, limit=limit)
            for stale in page:
                self.submissions.delete(stale.id)
                affected.add(stale.entry_id)
                removed_submissions += 1
            old = self.entries.list_before("last_joined_at", cutoff, limit=limit)
            affected |= {entry.id for entry in old}
            for key in sorted(affected):
                if not self.submissions.list_by_field("entry_id", key):
                    try:
                        self.entries.delete(key)
                    except KeyError:
                        continue
                    removed_entries += 1
        return removed_submissions, removed_entries

    # -- helpers -------------------------------------------------------

    def _view(self, submission: WaitlistSubmission) -> SubmissionView:
        return SubmissionView(submission.question_version, dict(submission.answers))

    @staticmethod
    def _new_token() -> str:
        return TOKEN_PREFIX + secrets.token_urlsafe(32)

    @contextmanager
    def transaction(self, lock_name: str) -> Iterator[None]:
        """One transaction under the waitlist scope, serialized per name."""
        guard = self._process_lock if self._advisory_lock is None else nullcontext()
        with guard, system_scope(SCOPE), self._unit_of_work():
            if self._advisory_lock is not None:
                self._advisory_lock(lock_name)
            yield

    @staticmethod
    def _spend(limiter: LoginRateLimiter, kind: str, value: str) -> None:
        decision = limiter.consume(login_bucket_key(kind, value), utc_now())
        if not decision.allowed:
            raise LoginThrottled(decision.retry_after_seconds)
