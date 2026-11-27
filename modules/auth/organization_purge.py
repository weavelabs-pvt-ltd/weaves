"""Resumable, idempotent, bounded purge of one organization's data.

Driven by the worker loop. Each pass runs under the organization's own scope and
performs independent steps in order (cancel work, revoke credentials, delete
secrets, delete each collection by bounded batches, sweep unknown collections,
audit retention). Every step is idempotent and commits on its own, so killing
the process between any two steps and restarting converges. The pass finalizes
only after two consecutive passes delete nothing, under the administration lock,
which also serializes against a concurrent first sign-in linking a membership.
See ``organization_retention`` for what is removed and retained.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from application.ports.organization_data import OrganizationData
from application.ports.repository import Repository
from application.ports.secrets import SecretStore
from application.ports.usage import UsageLedger
from contracts.v1 import (
    ApiCredential,
    ApiCredentialStatus,
    OrganizationLifecycle,
    OrganizationLifecycleState,
)
from modules.auth.lifecycle_actor import Actor
from modules.auth.organization_lifecycle import OrganizationLifecycleService
from modules.auth.organization_retention import (
    KEEP_COLLECTIONS,
    PURGE_ORDER,
    RETAINED_AUDIT,
)
from modules.recovery.tombstones import (
    ORGANIZATION_DELETED,
    ORGANIZATION_PURGE_STARTED,
    NullTombstones,
    TombstoneSink,
)
from shared.request_context import (
    reset_request_principal,
    set_request_identity,
    system_scope,
)

logger = logging.getLogger("product.worker")
State = OrganizationLifecycleState
BATCH = 200
CLEAN_PASSES = 2
# What a completed purge erased (categories only; see ``organization_retention``).
PURGE_ERASED = (
    "tenant_records",
    "secrets",
    "api_credentials",
    "usage",
    "slack",
    "memberships",
)


class SlackDataPurge(Protocol):
    """The Slack step: installations, bot tokens and inbox rows of one organization."""

    def purge_organization(self, limit: int) -> int:
        """Delete up to ``limit`` rows of the bound organization."""
        ...

    def remaining_rows(self) -> int:
        """Rows the bound organization still owns."""
        ...


class OrganizationPurgeService:
    """Move expired deletions to DELETING and purge DELETING organizations."""

    def __init__(
        self,
        *,
        lifecycle: OrganizationLifecycleService,
        lifecycle_records: Repository[OrganizationLifecycle],
        data: OrganizationData,
        secrets: SecretStore,
        credentials: Repository[ApiCredential],
        usage: UsageLedger,
        hook: Callable[[str], None] | None = None,
        tombstones: TombstoneSink | None = None,
        slack: SlackDataPurge | None = None,
    ) -> None:
        """Bind the lifecycle state machine and the bulk data/secret ports."""
        self.lifecycle = lifecycle
        self.records = lifecycle_records
        self.data = data
        self.secrets = secrets
        self.credentials = credentials
        self.usage = usage
        self.hook = hook
        self.tombstones: TombstoneSink = tombstones or NullTombstones()
        # Set after composition (the Slack store is built outside the capability set).
        self.slack = slack

    def _step(self, name: str) -> None:
        """Failure-injection seam: tests raise here to simulate a crash."""
        if self.hook is not None:
            self.hook(name)

    def tick(self) -> int:
        """Advance every pending organization; one failure never stops the rest."""
        with system_scope("lifecycle"):
            requested = self.records.list_by_field("state", State.DELETION_REQUESTED)
            deleting = self.records.list_by_field("state", State.DELETING)
        now = self.lifecycle.now()
        due = [
            r.id
            for r in requested
            if r.purge_now or (r.grace_deadline is not None and r.grace_deadline <= now)
        ]
        progressed = 0
        for org_id in dict.fromkeys([*due, *[r.id for r in deleting]]):
            try:
                self._advance(org_id)
                progressed += 1
            except Exception:
                logger.exception("organization purge failed org_id=%s", org_id)
        return progressed

    def _advance(self, org_id: str) -> None:
        self._step("begin")
        self.lifecycle.begin_deleting(org_id)
        token = set_request_identity(Actor.system().audit_id, org_id, "lifecycle")
        try:
            deleting = self.lifecycle.state_of(org_id) is State.DELETING
        finally:
            reset_request_principal(token)
        if deleting:
            self.purge_pass(org_id)

    def purge_pass(self, org_id: str) -> bool:
        """Run every step once; return True when the organization is DELETED."""
        actor = Actor.system()
        token = set_request_identity(actor.audit_id, org_id, "lifecycle")
        try:
            # Fail closed: nothing is erased until the intent is durably recorded.
            self._step("tombstone_started")
            self.tombstones.record(ORGANIZATION_PURGE_STARTED, org_id)
            deleted = self._delete_everything(org_id)
            self._record_pass(org_id, deleted)
            if deleted:
                return False
            return self._finalize_when_clean(org_id)
        finally:
            reset_request_principal(token)

    def _delete_everything(self, org_id: str) -> int:
        self._step("cancel_work")
        self.data.cancel_work(org_id, now=self.lifecycle.now(), limit=BATCH)
        self._step("revoke_credentials")
        self._revoke_credentials(org_id)
        # Before the secret purge: the bot token must still exist to be revoked.
        self._step("slack")
        deleted = self.slack.purge_organization(BATCH) if self.slack else 0
        self._checkpoint(org_id, "slack", deleted)
        self._step("secrets")
        secrets = self.secrets.purge_organization(BATCH)
        deleted += secrets
        self._checkpoint(org_id, "secrets", secrets)
        # Usage dies with the organization: ledger and rollup rows do not
        # outlive it (retention days apply to live organizations only).
        self._step("usage")
        usage = self.usage.purge_organization(BATCH)
        deleted += usage
        self._checkpoint(org_id, "usage", usage)
        for collection in PURGE_ORDER:
            self._step(collection)
            count = self.data.delete_batch(collection, org_id, limit=BATCH)
            deleted += count
            self._checkpoint(org_id, collection, count)
        self._step("audit_events")
        deleted += self.data.delete_batch(
            "audit_events", org_id, limit=BATCH, retain=RETAINED_AUDIT
        )
        self._step("sweep")
        deleted += self.data.delete_remaining_batch(
            org_id, keep_collections=KEEP_COLLECTIONS, limit=BATCH
        )
        return deleted

    def _revoke_credentials(self, org_id: str) -> None:
        now = self.lifecycle.now()
        for credential in self.credentials.list_scoped(org_id):
            if credential.status is ApiCredentialStatus.ACTIVE:
                self.credentials.put(
                    credential.model_copy(
                        update={
                            "status": ApiCredentialStatus.REVOKED,
                            "revoked_at": now,
                            "updated_at": now,
                        }
                    )
                )

    def _checkpoint(self, org_id: str, step: str, deleted: int) -> None:
        """Record the last completed step; resuming never depends on it."""
        record = self.lifecycle.current(org_id)
        if record.state is State.DELETING:
            self.records.put(
                record.model_copy(
                    update={
                        "purge_step": step,
                        "purged_rows": record.purged_rows + deleted,
                        "updated_at": self.lifecycle.now(),
                    }
                )
            )

    def _record_pass(self, org_id: str, deleted: int) -> None:
        """Count consecutive passes that found nothing left to delete."""
        record = self.lifecycle.current(org_id)
        if record.state is State.DELETING:
            clean = 0 if deleted else min(record.clean_ticks + 1, CLEAN_PASSES)
            self.records.put(
                record.model_copy(
                    update={"clean_ticks": clean, "purge_step": "pass_complete"}
                )
            )

    def _finalize_when_clean(self, org_id: str) -> bool:
        if self.lifecycle.current(org_id).clean_ticks < CLEAN_PASSES:
            return False
        self._step("finalize")
        with self.lifecycle.guard.mutation():
            state = self.lifecycle.state_of(org_id)
            if state is not State.DELETING:
                return state is State.DELETED
            self.data.delete_batch("memberships", org_id, limit=BATCH)
            if self.data.count_remaining(org_id, keep_collections=KEEP_COLLECTIONS):
                return False
            if self.usage.remaining_rows():
                return False
            if self.slack is not None and self.slack.remaining_rows():
                return False
            self.tombstones.record(ORGANIZATION_DELETED, org_id, erased=PURGE_ERASED)
            self.lifecycle.finish_deletion(org_id)
        return True

    def remaining_rows(self, org_id: str) -> int:
        """Rows still owned by the organization outside the retained set.

        The usage ledger counts rows of the bound tenant, so a caller outside a
        request (the operator ``status`` command) must bind the organization or a
        strict process refuses with ``TenantContextRequired``.
        """
        token = set_request_identity(Actor.system().audit_id, org_id, "lifecycle")
        try:
            return (
                self.data.count_remaining(org_id, keep_collections=KEEP_COLLECTIONS)
                + self.usage.remaining_rows()
                + (self.slack.remaining_rows() if self.slack else 0)
            )
        finally:
            reset_request_principal(token)
