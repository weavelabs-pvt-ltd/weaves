"""Organization lifecycle metadata, separate from the stable identity payload.

``Organization.status`` keeps its original three values so a previous release's
strict reader still parses every stored organization. The richer state lives
here. Every non-active state projects to a non-active ``Organization.status``
(see ``modules.auth.lifecycle_state``), so an older reader fails closed.
"""

from pydantic import AwareDatetime, Field

from contracts.v1.base import (
    FrozenProductContract,
    MutableOrgScopedContract,
    OpaqueId,
    StrEnum,
)


class OrganizationLifecycleState(StrEnum):
    """Effective lifecycle of an organization."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETION_REQUESTED = "deletion_requested"
    DELETING = "deleting"
    DELETED = "deleted"


class LifecycleActorKind(StrEnum):
    """Who caused a lifecycle change."""

    OWNER = "owner"
    OPERATOR = "operator"
    SYSTEM = "system"


class LifecycleHistoryEntry(FrozenProductContract):
    """One state change: who did it, why, and when."""

    state: OrganizationLifecycleState
    reason: str | None = Field(default=None, max_length=500)
    actor_kind: LifecycleActorKind
    actor_ref: str = Field(min_length=1, max_length=320)
    at: AwareDatetime


class OrganizationLifecycle(MutableOrgScopedContract):
    """Current lifecycle state, grace deadline, history and purge checkpoint.

    The record is retained after deletion as part of the tombstone.
    """

    id: OpaqueId
    state: OrganizationLifecycleState = OrganizationLifecycleState.ACTIVE
    previous_state: OrganizationLifecycleState | None = None
    reason: str | None = Field(default=None, max_length=500)
    actor_kind: LifecycleActorKind | None = None
    actor_ref: str | None = Field(default=None, max_length=320)
    grace_deadline: AwareDatetime | None = None
    purge_now: bool = False
    deleted_at: AwareDatetime | None = None
    purge_step: str | None = Field(default=None, max_length=64)
    purged_rows: int = Field(default=0, ge=0)
    clean_ticks: int = Field(default=0, ge=0, le=100)
    revision: int = Field(default=1, ge=1)
    history: list[LifecycleHistoryEntry] = Field(default_factory=list, max_length=200)
