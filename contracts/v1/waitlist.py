"""Public waitlist records: one entry per email, one submission per join.

Both are global rows (no organization) that no tenant, account or session ever
reads. A submission holds the answers one browser gave, keyed by question id,
and is reachable only through its private token, which is stored as a digest.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field, StringConstraints

from contracts.v1.base import (
    MutableProductContract,
    OpaqueId,
    ProductContract,
)

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SourceText = Annotated[str, StringConstraints(max_length=512)]


class WaitlistStatus(StrEnum):
    """Where an operator has taken an entry."""

    NEW = "new"
    REVIEWED = "reviewed"
    CONTACTED = "contacted"
    CONVERTED = "converted"


class WaitlistDomainType(StrEnum):
    """Whether an address is on a company domain or a public mailbox provider."""

    COMPANY = "company"
    PUBLIC = "public"


class WaitlistAnswer(ProductContract):
    """One answer: option ids, a band id or short text, plus optional other text."""

    value: str | list[str]
    other: str | None = Field(default=None, max_length=60)


class WaitlistSource(ProductContract):
    """Where the visitor came from; every field is optional campaign context."""

    page: SourceText | None = None
    referrer: SourceText | None = None
    utm_source: SourceText | None = None
    utm_medium: SourceText | None = None
    utm_campaign: SourceText | None = None
    utm_term: SourceText | None = None
    utm_content: SourceText | None = None


class WaitlistHistoryEntry(ProductContract):
    """One operator action on an entry (never a token or an answer)."""

    action: str = Field(min_length=1, max_length=40)
    actor_kind: str = Field(min_length=1, max_length=20)
    operator: str = Field(min_length=1, max_length=320)
    at: AwareDatetime


class WaitlistEntry(MutableProductContract):
    """One person's email on the waitlist. ``id`` is derived from the email."""

    id: OpaqueId
    email: str = Field(min_length=3, max_length=254)
    email_domain: str = Field(min_length=1, max_length=253)
    domain_type: WaitlistDomainType
    status: WaitlistStatus = WaitlistStatus.NEW
    notes: str = Field(default="", max_length=2000)
    first_joined_at: AwareDatetime
    last_joined_at: AwareDatetime
    submission_count: int = Field(default=1, ge=0)
    history: list[WaitlistHistoryEntry] = Field(default_factory=list, max_length=50)


class WaitlistSubmission(MutableProductContract):
    """One join from one browser, editable only with its own private token."""

    id: OpaqueId
    entry_id: OpaqueId
    token_hash: Digest
    token_expires_at: AwareDatetime
    question_version: int = Field(ge=1)
    answers: dict[str, WaitlistAnswer] = Field(default_factory=dict)
    consent_version: int = Field(ge=1)
    consented_at: AwareDatetime
    source: WaitlistSource = Field(default_factory=WaitlistSource)
    idempotency_digest: Digest | None = None
