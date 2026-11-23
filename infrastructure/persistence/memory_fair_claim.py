"""In-memory mirror of the PostgreSQL fair claim (round robin plus running cap)."""

from __future__ import annotations

from datetime import UTC, datetime
from threading import RLock
from typing import TYPE_CHECKING, Any

from application.ports.fair_claim import FairClaim
from application.ports.limits import LimitPolicy
from contracts.v1.base import ProductContract


def _record_value(record: Any, field: str) -> Any:
    """Read a field from a product contract with a runtime-selected schema."""
    return getattr(record, field)


class FairGroup:
    """Round-robin state and members shared by queues that cap together."""

    def __init__(self) -> None:
        """Start with no members and no claim history."""
        self.lock = RLock()
        self.last_claimed: dict[str, datetime] = {}
        self.members: list[InMemoryFairClaimMixin[Any]] = []


class InMemoryFairClaimMixin[TContract: ProductContract]:
    """Fair claim, queue age and claim-state pruning over ``self._records``."""

    _records: dict[str, Any]
    _lock: RLock
    _fair_group: FairGroup

    if TYPE_CHECKING:

        def _record_id(self, record: Any) -> str: ...

        def _clone(self, record: TContract) -> TContract: ...

    def _init_fair_claims(self) -> None:
        """Start with a private group; ``share_fair_claims`` joins queues."""
        self._fair_group = FairGroup()
        self._fair_group.members.append(self)

    def share_fair_claims(self, *peers: InMemoryFairClaimMixin[Any]) -> None:
        """Count running work and round-robin state together with ``peers``."""
        group = FairGroup()
        for repository in (self, *peers):
            repository._fair_group = group
            group.members.append(repository)

    def _running_for(self, org_id: str) -> int:
        """Count this collection's running (or cancel-requested) records."""
        with self._lock:
            return sum(
                1
                for record in self._records.values()
                if getattr(record, "org_id", None) == org_id
                and getattr(getattr(record, "status", None), "value", None)
                in {"running", "cancel_requested"}
            )

    def claim_next_fair(
        self, worker_id: str, timestamp: datetime, limits: LimitPolicy
    ) -> FairClaim[TContract] | None:
        """Mirror the PostgreSQL fair claim: round robin plus a running cap."""
        group = self._fair_group
        with group.lock:
            queued: dict[str, list[TContract]] = {}
            with self._lock:
                for record in self._records.values():
                    status = getattr(getattr(record, "status", None), "value", None)
                    org = getattr(record, "org_id", None)
                    if status == "queued" and org is not None:
                        queued.setdefault(str(org), []).append(record)
            never = datetime.min.replace(tzinfo=UTC)
            order = sorted(
                queued, key=lambda org: (group.last_claimed.get(org, never), org)
            )
            for org_id in order:
                org_limits = limits.for_org(org_id)
                running = sum(member._running_for(org_id) for member in group.members)
                if org_limits.enforcing and running >= org_limits.max_running_per_org:
                    continue
                current = min(
                    queued[org_id],
                    key=lambda item: (
                        _record_value(item, "created_at"),
                        self._record_id(item),
                    ),
                )
                claimed = self._mark_running(current, worker_id, timestamp)
                group.last_claimed[org_id] = timestamp
                return FairClaim(
                    claimed, running, org_limits.max_running_per_org, org_limits.mode
                )
        return None

    def oldest_queued_at(self) -> datetime | None:
        """Return the creation time of the oldest queued record, or None."""
        with self._lock:
            times = [
                _record_value(record, "created_at")
                for record in self._records.values()
                if getattr(getattr(record, "status", None), "value", None) == "queued"
            ]
        return min(times) if times else None

    def prune_claim_state(self, claimed_before: datetime) -> int:
        """Forget round-robin history older than ``claimed_before``."""
        group = self._fair_group
        with group.lock:
            stale = [
                org for org, when in group.last_claimed.items() if when < claimed_before
            ]
            for org in stale:
                del group.last_claimed[org]
        return len(stale)

    def _mark_running(
        self, current: TContract, worker_id: str, timestamp: datetime
    ) -> TContract:
        """Persist and return ``current`` as running under ``worker_id``."""
        status_type = type(_record_value(current, "status"))
        claimed = type(current).model_validate(
            current.model_copy(
                update={
                    "status": status_type("running"),
                    "attempts": int(getattr(current, "attempts", 0)) + 1,
                    "worker_id": worker_id,
                    "started_at": timestamp,
                    "updated_at": timestamp,
                }
            ).model_dump()
        )
        with self._lock:
            self._records[self._record_id(current)] = self._clone(claimed)
        return self._clone(claimed)
