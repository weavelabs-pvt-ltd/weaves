"""Compose the admission service for one process from its storage and the env."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from application.ports.limits import limit_policy_from_env
from application.ports.product_repositories import ProductRepositorySet
from application.ports.repository import Repository
from contracts.v1.base import ProductContract
from contracts.v1.limits import OrganizationLimitOverride
from infrastructure.persistence.admission_counts import (
    MemoryAdmissionCounts,
    PostgresAdmissionCounts,
)
from modules.limits.policy import OverridingLimitPolicy
from modules.limits.service import AdmissionService
from modules.platform_controls.controls import PlatformControls


def build_admission_service(
    *,
    store: Any | None,
    repositories: ProductRepositorySet,
    lock: Callable[[str], None] | None,
    env: Mapping[str, str] | None = None,
    controls: PlatformControls | None = None,
) -> AdmissionService:
    """Build limits (environment defaults plus stored overrides) and admission.

    Raises ``LimitsConfigError`` for a malformed setting so a bad value stops
    the process at start instead of silently changing behaviour.
    """
    base = limit_policy_from_env(os.environ if env is None else env)
    overrides: Repository[ProductContract] = repositories.get("organization_limits")

    def load(org_id: str) -> OrganizationLimitOverride | None:
        try:
            record = overrides.get_scoped(org_id, org_id)
        except KeyError:
            return None
        return record if isinstance(record, OrganizationLimitOverride) else None

    policy = OverridingLimitPolicy(base, load)
    counts = (
        PostgresAdmissionCounts(store)
        if store is not None
        else MemoryAdmissionCounts(repositories)
    )
    return AdmissionService(policy, counts, lock, controls)
