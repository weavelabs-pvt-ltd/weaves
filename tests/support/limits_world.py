"""A two-organization PostgreSQL world for limits tests (restricted app role)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet

from apps.runtime_wiring import compose_platform_runtime
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from modules.limits.errors import LimitExceeded
from shared.request_context import require_tenant_context, system_scope
from tests.support.organization_administration import scope
from tests.support.organizations import create_organization

CAP = 5


def open_runtime(url: str, role: str) -> Any:
    pool = open_postgres_pool(url, min_size=1, max_size=8)
    store = ProductPostgresStore(pool=pool, app_role=role)
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=store, seed_local_defaults=False
    )
    runtime.bootstrap()
    return runtime


class World:
    """Two API-role runtimes, an operator runtime and two organizations."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.api, self.api2 = (open_runtime(url, "weaves_app") for _ in range(2))
        self.operator = open_runtime(url, "")
        require_tenant_context(True)
        with system_scope("bootstrap"):
            self.a = create_organization(
                self.api, name="Org A", email="a@example.test", display_name="A"
            )
            self.b = create_organization(
                self.api, name="Org B", email="b@example.test", display_name="B"
            )
        self.agent = {}
        for key, owner in (("a", self.a), ("b", self.b)):
            with scope(owner):
                agent = self.api.agent_service.create_agent(
                    name=f"Agent {key}", instructions="Be brief."
                )
            self.agent[key] = agent.id

    def create(self, key: str, name: str, runtime: Any = None, **extra: Any) -> Any:
        owner = self.a if key == "a" else self.b
        with scope(owner):
            return (runtime or self.api).task_service.create_task(
                agent_id=self.agent[key],
                title=name,
                initial_message=f"say {name}",
                **extra,
            )

    def queued(self, key: str) -> int:
        owner = self.a if key == "a" else self.b
        with psycopg.connect(self.url, autocommit=True) as connection:
            return int(
                connection.execute(
                    "SELECT count(*) FROM product_records WHERE org_id = %s "
                    "AND collection IN ('task_turns', 'execution_jobs') "
                    "AND payload->>'status' = 'queued'",
                    (owner[0].id,),
                ).fetchone()[0]
            )

    def close(self) -> None:
        require_tenant_context(False)
        for runtime in (self.api, self.api2, self.operator):
            runtime.close()


def build(url: str, monkeypatch: pytest.MonkeyPatch, **env: str) -> World:
    monkeypatch.setenv("WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WEAVES_LIMITS_MODE", "enforce")
    monkeypatch.setenv("WEAVES_LIMIT_MAX_QUEUED_PER_ORG", str(CAP))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    ProductPostgresStore(url).close()
    return World(url)


def race(world: World, count: int, key: str = "a") -> list[Any]:
    """Create ``count`` tasks simultaneously across two runtimes."""
    gate = Barrier(count)

    def attempt(index: int) -> Any:
        gate.wait()
        runtime = world.api if index % 2 == 0 else world.api2
        try:
            return world.create(key, f"race-{key}-{index}", runtime)
        except LimitExceeded as refusal:
            return refusal

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(attempt, range(count)))
