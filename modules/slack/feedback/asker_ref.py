"""Who rated an answer, without storing the Slack user id (design 3.13).

An asker whom an administrator mapped to a Weaves member is recorded by that
member's principal id: it is already an organization-internal id, it joins to the
rest of the product, and it disappears with the member. Any other asker is
recorded as an HMAC-SHA256 of the Slack user id keyed with a per-organization
random pepper, so the same person keeps one stable reference inside an
organization, two organizations never share a reference for one Slack user, and
the raw Slack id is never stored.

The pepper is a random secret created on first use and kept in the secret store
(purpose ``slack_feedback_pepper``); its reference lives on the Slack access
settings row without bumping ``settings_version``. Creating it happens under the
organization's Slack mutation lock, so two workers cannot make two peppers.
Deleting the organization removes the secret with everything else. No pepper, no
reference: when the secret store cannot be read the reaction is retried and, if
that keeps failing, dropped. It is never stored with the raw id as a fallback.
"""

from __future__ import annotations

import hmac
import secrets as random_secrets
from dataclasses import dataclass
from hashlib import sha256

from application.ports.secrets import SecretStore
from modules.slack.access.service import SlackAccessService
from shared.request_context import effective_org_id

PEPPER_PURPOSE = "slack_feedback_pepper"
HASH_PREFIX = "hmac:"
HASH_CHARS = 32  # 128 bits: far beyond any collision worry inside one organization
MAX_CACHED_ORGS = 1000


@dataclass(frozen=True)
class AskerRef:
    """The chosen reference plus the other form this asker may already have rows in."""

    ref: str
    kind: str
    hashed: str


def hash_user(pepper: str, slack_user_id: str) -> str:
    """The per-organization HMAC reference of one Slack user id."""
    digest = hmac.new(
        pepper.encode("utf-8"), f"slack-user:{slack_user_id}".encode(), sha256
    )
    return HASH_PREFIX + digest.hexdigest()[:HASH_CHARS]


class AskerRefs:
    """Resolve a Slack user id to the reference stored with their feedback."""

    def __init__(self, *, access: SlackAccessService, secrets: SecretStore) -> None:
        """Bind the settings service (pepper reference, allowlist) and the secrets."""
        self._access = access
        self._secrets = secrets
        self._peppers: dict[str, str] = {}

    def resolve(self, slack_user_id: str) -> AskerRef:
        """Mapped member principal id, else the keyed hash (call under tenant scope)."""
        hashed = hash_user(self._pepper(), slack_user_id)
        row = self._access.allowlists.get_user(slack_user_id)
        if row is not None and row.principal_id:
            return AskerRef(row.principal_id, "member", hashed)
        return AskerRef(hashed, "hashed", hashed)

    def _pepper(self) -> str:
        org = effective_org_id()
        cached = self._peppers.get(org)
        if cached is not None:
            return cached
        pepper = self._read() or self._create()
        if len(self._peppers) >= MAX_CACHED_ORGS:
            self._peppers.clear()
        self._peppers[org] = pepper
        return pepper

    def _read(self) -> str | None:
        ref = self._access.feedback_pepper_ref()
        if ref is None:
            return None
        try:
            return self._secrets.get(ref)
        except KeyError:
            return None  # the secret is gone: a new pepper starts a new keyspace

    def _create(self) -> str:
        with self._access.mutation():
            existing = self._read()  # another worker may have won the race
            if existing is not None:
                return existing
            pepper = random_secrets.token_urlsafe(32)
            ref = self._secrets.put(
                pepper, purpose=PEPPER_PURPOSE, owner_ref=effective_org_id()
            )
            self._access.remember_feedback_pepper(ref)
            return pepper
