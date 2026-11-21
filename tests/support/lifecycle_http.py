"""Routes, world builder and helpers shared by the lifecycle test modules."""

from __future__ import annotations

from typing import Any

from tests.support.lifecycle_world import seed_rich
from tests.support.organization_administration import prepare

LIFECYCLE = "/api/v0/organization/lifecycle"
REQUEST = "/api/v0/organization/deletion-request"
CANCEL = "/api/v0/organization/deletion-cancel"
EXPORT = "/api/v0/organization/export"
SETTINGS = "/api/v0/organization/settings"


def populated_world(runtime: Any) -> dict[str, Any]:
    """Two tenants (A, B), one shared member, each organization richly seeded."""
    world = {"runtime": runtime, **prepare(runtime)}
    world["seed_a"] = seed_rich(runtime, world["a"], "ALPHA")
    world["seed_b"] = seed_rich(runtime, world["b"], "BRAVO")
    return world


def drain(runtime: Any, limit: int = 12) -> None:
    """Run worker deletion ticks until nothing is pending (crash-tolerant)."""
    for _ in range(limit):
        runtime.organization_purge_service.tick()
