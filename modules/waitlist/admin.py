"""Operator-side waitlist reads and changes (list, export, stats, status, delete).

Used only by ``infra/gcp/singlevm/waitlist.py`` on the VM. Reports use each
email's latest submission. Nothing here prints or returns a resume token or its
digest.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from contracts.v1 import (
    WaitlistDomainType,
    WaitlistEntry,
    WaitlistHistoryEntry,
    WaitlistStatus,
    WaitlistSubmission,
)
from modules.waitlist.emails import entry_id, normalize_email
from modules.waitlist.errors import InvalidInput
from modules.waitlist.questions import Question, QuestionType
from modules.waitlist.service import SCOPE, WaitlistService
from shared.clock import utc_now
from shared.request_context import system_scope

ACTOR_KIND = "operator"
HISTORY_LIMIT = 50
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class Filters:
    """Optional list filters; all must match."""

    status: WaitlistStatus | None = None
    domain_type: WaitlistDomainType | None = None
    since: date | None = None
    until: date | None = None
    answer: tuple[str, str] | None = None


def parse_answer_filter(text: str) -> tuple[str, str]:
    """Read ``question_id=option_id`` from the command line."""
    question_id, separator, option = text.partition("=")
    if not separator or not question_id or not option:
        raise InvalidInput("--answer must look like question_id=option_id")
    return question_id, option


def _safe_cell(value: Any) -> str:
    """Neutralize spreadsheet formulas in a CSV cell."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_STARTS) else text


class WaitlistAdmin:
    """Read and change waitlist rows as an operator."""

    def __init__(self, service: WaitlistService) -> None:
        """Operate on the service's repositories and question bank."""
        self._service = service
        self._entries = service.entries
        self._submissions = service.submissions
        self._bank = service.bank

    # -- reads ---------------------------------------------------------

    def _latest(self) -> list[tuple[WaitlistEntry, WaitlistSubmission | None]]:
        """Every entry with its newest submission, oldest sign-up first."""
        with system_scope(SCOPE):
            newest: dict[str, WaitlistSubmission] = {}
            for item in self._submissions.list():
                held = newest.get(item.entry_id)
                if held is None or item.created_at > held.created_at:
                    newest[item.entry_id] = item
            entries = sorted(self._entries.list(), key=lambda e: e.first_joined_at)
        return [(entry, newest.get(entry.id)) for entry in entries]

    @staticmethod
    def _matches(
        entry: WaitlistEntry, latest: WaitlistSubmission | None, filters: Filters
    ) -> bool:
        joined = entry.first_joined_at.date()
        checks = (
            filters.status in (None, entry.status),
            filters.domain_type in (None, entry.domain_type),
            filters.since is None or joined >= filters.since,
            filters.until is None or joined <= filters.until,
        )
        if not all(checks):
            return False
        if filters.answer is None:
            return True
        answer = (latest.answers if latest else {}).get(filters.answer[0])
        if answer is None:
            return False
        values = answer.value if isinstance(answer.value, list) else [answer.value]
        return filters.answer[1] in values

    def _summary(self, version: int, answers: dict[str, Any]) -> dict[str, str]:
        """Human labels for each answer, keyed by question id."""
        found = self._bank.question_set(version)
        out: dict[str, str] = {}
        for question_id, answer in answers.items():
            question = found.find(question_id) if found else None
            out[question_id] = _label(question, answer)
        return out

    def list_entries(self, filters: Filters) -> list[dict[str, Any]]:
        """Entries matching the filters, with a summary of the latest answers."""
        rows = []
        for entry, latest in self._latest():
            if not self._matches(entry, latest, filters):
                continue
            rows.append(
                {
                    "email": entry.email,
                    "domain_type": entry.domain_type.value,
                    "status": entry.status.value,
                    "notes": entry.notes,
                    "submission_count": entry.submission_count,
                    "first_joined_at": entry.first_joined_at,
                    "last_joined_at": entry.last_joined_at,
                    "question_version": latest.question_version if latest else None,
                    "answers": (
                        self._summary(latest.question_version, latest.answers)
                        if latest
                        else {}
                    ),
                }
            )
        return rows

    def export_csv(self, filters: Filters | None = None) -> str:
        """A CSV of the matching entries with one column per active question."""
        active = self._bank.active
        header = [
            "email",
            "email_domain",
            "domain_type",
            "status",
            "notes",
            "first_joined_at",
            "last_joined_at",
            "submission_count",
            "question_version",
            "consent_version",
            "source_page",
            "source_referrer",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            *(question.id for question in active.questions),
        ]
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(header)
        for entry, latest in self._latest():
            if not self._matches(entry, latest, filters or Filters()):
                continue
            writer.writerow(self._csv_row(entry, latest))
        return out.getvalue()

    def _csv_row(
        self, entry: WaitlistEntry, latest: WaitlistSubmission | None
    ) -> list[str]:
        source = latest.source if latest else None
        labels = (
            self._summary(latest.question_version, latest.answers) if latest else {}
        )
        cells: list[Any] = [
            entry.email,
            entry.email_domain,
            entry.domain_type.value,
            entry.status.value,
            entry.notes,
            entry.first_joined_at.isoformat(),
            entry.last_joined_at.isoformat(),
            entry.submission_count,
            latest.question_version if latest else "",
            latest.consent_version if latest else "",
            *(
                getattr(source, field, None) if source else ""
                for field in (
                    "page",
                    "referrer",
                    "utm_source",
                    "utm_medium",
                    "utm_campaign",
                    "utm_term",
                    "utm_content",
                )
            ),
            *(labels.get(q.id, "") for q in self._bank.active.questions),
        ]
        return [_safe_cell(cell) for cell in cells]

    def stats(self) -> dict[str, Any]:
        """Sign-ups per day, per-question completion and answer distribution."""
        pairs = self._latest()
        total = len(pairs)
        per_day = Counter(
            entry.first_joined_at.date().isoformat() for entry, _ in pairs
        )
        domains = Counter(entry.domain_type.value for entry, _ in pairs)
        questions = {
            question.id: _question_stats(
                question, [sub for _, sub in pairs if sub is not None], total
            )
            for question in self._bank.active.questions
        }
        return {
            "entries": total,
            "submissions": sum(entry.submission_count for entry, _ in pairs),
            "signups_per_day": dict(sorted(per_day.items())),
            "company_vs_public": {
                "company": domains.get("company", 0),
                "public": domains.get("public", 0),
                "company_share": round(domains.get("company", 0) / total, 4)
                if total
                else 0.0,
            },
            "questions": questions,
        }

    # -- changes -------------------------------------------------------

    def _existing(self, email: str) -> WaitlistEntry:
        key = entry_id(normalize_email(email))
        try:
            return self._entries.get(key)
        except KeyError as exc:
            raise KeyError("no waitlist entry for that email") from exc

    def _record(
        self, entry: WaitlistEntry, action: str, operator: str, **changes: Any
    ) -> WaitlistEntry:
        now = utc_now()
        history = [
            *entry.history,
            WaitlistHistoryEntry(
                action=action, actor_kind=ACTOR_KIND, operator=operator, at=now
            ),
        ][-HISTORY_LIMIT:]
        return entry.model_copy(
            update={"history": history, "updated_at": now, **changes}
        )

    def set_status(self, email: str, status: str, operator: str) -> dict[str, Any]:
        """Move an entry to a new status."""
        try:
            wanted = WaitlistStatus(status)
        except ValueError as exc:
            raise InvalidInput(
                "status must be new, reviewed, contacted or converted"
            ) from exc
        with self._service.transaction("waitlist-admin"):
            entry = self._existing(email)
            saved = self._record(
                entry, f"status:{wanted.value}", operator, status=wanted
            )
            self._entries.put(saved)
        return {"entry_id": saved.id, "status": saved.status.value}

    def note(self, email: str, text: str, operator: str) -> dict[str, Any]:
        """Replace an entry's operator notes (empty text clears them)."""
        if len(text) > 2000:
            raise InvalidInput("a note is at most 2000 characters")
        with self._service.transaction("waitlist-admin"):
            entry = self._existing(email)
            saved = self._record(entry, "note", operator, notes=text)
            self._entries.put(saved)
        return {"entry_id": saved.id, "note_length": len(text)}

    def delete(self, email: str) -> dict[str, Any]:
        """Erase an entry and every one of its submissions."""
        with self._service.transaction("waitlist-admin"):
            entry = self._existing(email)
            submissions = self._submissions.list_by_field("entry_id", entry.id)
            for item in submissions:
                self._submissions.delete(item.id)
            self._entries.delete(entry.id)
        return {"entry_id": entry.id, "submissions_deleted": len(submissions)}


def _label(question: Question | None, answer: Any) -> str:
    """Readable text for one stored answer."""
    values = answer.value if isinstance(answer.value, list) else [answer.value]
    if question is None or question.type is QuestionType.SHORT_TEXT:
        return "; ".join(str(v) for v in values)
    names = {option.id: option.label for option in question.options or ()}
    text = "; ".join(names.get(str(v), str(v)) for v in values)
    return f"{text} ({answer.other})" if answer.other else text


def _question_stats(
    question: Question, submissions: list[WaitlistSubmission], total: int
) -> dict[str, Any]:
    """Completion and distribution of one question over latest submissions."""
    distribution: Counter[str] = Counter()
    answered = 0
    for submission in submissions:
        answer = submission.answers.get(question.id)
        if answer is None:
            continue
        answered += 1
        if question.type is not QuestionType.SHORT_TEXT:
            values = answer.value if isinstance(answer.value, list) else [answer.value]
            distribution.update(str(v) for v in values)
    return {
        "label": question.summary_label,
        "answered": answered,
        "completion": round(answered / total, 4) if total else 0.0,
        "distribution": dict(distribution.most_common()),
    }


def parse_date(text: str | None) -> date | None:
    """Read ``YYYY-MM-DD`` (UTC) from the command line."""
    if text is None:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise InvalidInput("dates must look like 2026-11-22") from exc
