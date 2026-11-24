"""Migrations 21 and later, each defined in its own module, in version order.

Append the next migration here (not in ``store.py``, which holds versions 1-20
inline and is at its size limit).
"""

from __future__ import annotations

from infrastructure.database.approvals_removal import APPROVALS_REMOVAL_MIGRATION
from infrastructure.database.bounded_queries_schema import BOUNDED_QUERIES_MIGRATION
from infrastructure.database.controls_schema import CONTROLS_MIGRATION
from infrastructure.database.legacy_purge import LEGACY_PURGE_MIGRATION
from infrastructure.database.limits_schema import LIMITS_MIGRATION
from infrastructure.database.organization_identity import (
    ORGANIZATION_IDENTITY_MIGRATION,
)
from infrastructure.database.row_security import ROW_SECURITY_MIGRATION
from infrastructure.database.scheduler_schema import SCHEDULER_MIGRATION
from infrastructure.database.usage_schema import USAGE_MIGRATION
from infrastructure.database.waitlist_schema import WAITLIST_MIGRATION

LATER_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    ROW_SECURITY_MIGRATION,
    LEGACY_PURGE_MIGRATION,
    ORGANIZATION_IDENTITY_MIGRATION,
    WAITLIST_MIGRATION,
    LIMITS_MIGRATION,
    SCHEDULER_MIGRATION,
    USAGE_MIGRATION,
    BOUNDED_QUERIES_MIGRATION,
    CONTROLS_MIGRATION,
    APPROVALS_REMOVAL_MIGRATION,
)
