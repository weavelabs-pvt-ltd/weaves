"""Foreign IDs in every reference field are refused exactly like missing IDs.

Two organizations and three workspaces (A1, A2 in organization A; B1 in
organization B) hold comparable records. The caller is the owner of A1. For each
reference-bearing request the same call is made once with a real record from
another tenant and once with an ID that exists nowhere. The two responses must
be indistinguishable, and neither may change any repository, call a model
provider, or reach a tool or connector over the network.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from apps.api.app import create_app
from clients.model_providers.anthropic import AnthropicAdapter
from clients.model_providers.gemini import GeminiAdapter
from clients.model_providers.openai_compatible import OpenAICompatibleAdapter
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.deterministic_model import DeterministicMockModel
from tests.contracts import reference_scope_cases_admin  # noqa: F401
from tests.contracts.reference_scope_cases import CASES, Case
from tests.support.runtime import LocalPlatformRuntime
from tests.support.tenant_world import World, build_world


class Calls:
    """Count model completions and non-test network requests."""

    def __init__(self) -> None:
        self.model = 0
        self.network = 0


@pytest.fixture(scope="module")
def calls() -> Calls:
    return Calls()


@pytest.fixture(scope="module", params=["memory", "postgres"])
def world(request: pytest.FixtureRequest, calls: Calls) -> Iterator[World]:
    patch = pytest.MonkeyPatch()
    try:
        _count_external_calls(patch, calls)
        with _backend(request.param) as store:
            runtime = LocalPlatformRuntime(postgres_store=store)
            client = TestClient(create_app(runtime))
            with client:
                yield build_world(client, runtime)
    finally:
        patch.undo()


@contextmanager
def _backend(kind: str) -> Iterator[ProductPostgresStore | None]:
    """Run against in-memory repositories, or an isolated PostgreSQL schema."""
    if kind == "memory":
        yield None
        return
    database_url = os.environ.get("WEAVES_TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set WEAVES_TEST_DATABASE_URL to run the PostgreSQL matrix")
    schema = f"weaves_refscope_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated = make_conninfo(database_url, options=f"-c search_path={schema},public")
    store = ProductPostgresStore(isolated)
    try:
        yield store
    finally:
        store.close()
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


def _count_external_calls(patch: pytest.MonkeyPatch, calls: Calls) -> None:
    """Fail-safe counters: no catalog refresh or completion may run unnoticed."""
    for adapter in (
        AnthropicAdapter,
        GeminiAdapter,
        OpenAICompatibleAdapter,
        DeterministicMockModel,
    ):
        patch.setattr(adapter, "complete", _counting(calls, "model"))
        if hasattr(adapter, "list_models"):
            patch.setattr(adapter, "list_models", _counting(calls, "model"))
    original = httpx.Client.send

    def send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        if not isinstance(self, TestClient):
            calls.network += 1
        return original(self, request, **kwargs)

    patch.setattr(httpx.Client, "send", send)


def _counting(calls: Calls, field: str) -> Callable[..., object]:
    def count(*args: object, **kwargs: object) -> object:
        setattr(calls, field, getattr(calls, field) + 1)
        raise AssertionError("an external call was attempted")

    return count


def _snapshot(world: World) -> str:
    """Serialize every persisted record so any create or update is visible."""
    repositories = world.runtime._repository_set._repositories  # type: ignore[attr-defined]
    state = {
        name: sorted(record.model_dump_json() for record in repository.list())
        for name, repository in repositories.items()
        if hasattr(repository, "list")
    }
    return json.dumps(state, sort_keys=True)


def _foreign_ids(world: World, source: str) -> dict[str, str]:
    return {"org": world.b1.ids, "ws": world.a2.ids, "peer": world.peer_ids}[source]


def _send(world: World, request: tuple[str, str, dict | None]):
    method, path, body = request
    return world.client.request(method, path, headers=world.a1.headers, json=body)


def _normalised(response, used: dict[str, str]) -> tuple[int, str]:
    text = response.text
    for value in sorted(used.values(), key=len, reverse=True):
        text = text.replace(value, "<REF>")
    return response.status_code, text


def _attempt(world: World, case: Case, ids: dict[str, str] | None):
    """Send one request; reference keys resolve to ``ids`` or to missing IDs."""
    used: dict[str, str] = {}

    def ref(key: str) -> str:
        value = ids[key] if ids is not None else _missing(case, key)
        used[key] = value
        return value

    request = case.build(world, ref)
    before = _snapshot(world)
    response = _send(world, request)
    return _normalised(response, used), before == _snapshot(world)


def _missing(case: Case, key: str) -> str:
    return dict(case.missing).get(key, f"missing-{key.replace('_', '-')}")


def _parameters() -> list:
    return [
        pytest.param(case, source, id=f"{case.name}[{source}]")
        for case in CASES
        for source in case.sources
    ]


@pytest.mark.parametrize(("case", "source"), _parameters())
def test_foreign_reference_matches_missing_reference(
    world: World, calls: Calls, case: Case, source: str
) -> None:
    ids = _foreign_ids(world, source)
    if case.needs and case.needs not in ids:
        pytest.skip(f"{case.needs} needs durable storage")
    model_calls, network_calls = calls.model, calls.network
    missing, missing_unchanged = _attempt(world, case, None)
    foreign, foreign_unchanged = _attempt(world, case, ids)
    assert missing[0] in {400, 403, 404, 409, 412, 422}, missing
    assert foreign == missing
    assert missing_unchanged, "the missing-reference baseline changed records"
    assert foreign_unchanged, "the foreign reference changed records"
    assert (calls.model, calls.network) == (model_calls, network_calls)


def test_same_tenant_references_still_resolve(world: World) -> None:
    """The tenant's own records remain valid in the same fields."""
    own, headers = world.a1.ids, world.a1.headers
    created = world.client.post(
        "/api/v0/agents",
        headers=headers,
        json={
            "name": "Valid references",
            "instructions": "Reply.",
            "model_profile_id": own["profile"],
            "allowed_capability_ids": ["slack.messages.search"],
            "skill_version_ids": [own["org_skill_version"]],
        },
    )
    assert created.status_code == 201, created.text
    dependency = {
        "target_agent_id": own["agent2"],
        "depends_on_agent_ids": [own["agent"]],
    }
    steps = world.client.put(
        f"/api/v0/workflows/{own['workflow']}/steps",
        headers=headers,
        json={
            "agent_ids": [own["agent"], own["agent2"]],
            "step_dependencies": [dependency],
        },
    )
    assert steps.status_code == 200, steps.text
    task = world.client.post(
        "/api/v0/tasks",
        headers=headers,
        json={"agent_id": own["agent"], "title": "Ok", "initial_message": "Hi"},
    )
    assert task.status_code == 202, task.text
