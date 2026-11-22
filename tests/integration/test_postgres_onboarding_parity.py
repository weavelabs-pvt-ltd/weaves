"""Onboarding design parity on PostgreSQL under the restricted ``weaves_app`` role."""

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from infrastructure.database.organization_identity import (
    EMAIL_DOMAIN_BACKFILL,
    SLUG_BACKFILL,
)
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import SCHEMA_HEAD_VERSION, ProductPostgresStore
from shared.request_context import require_tenant_context
from tests.support import onboarding_cases as cases
from tests.support import organization_identity_cases as identity


@pytest.fixture
def world(isolated_postgres_url, monkeypatch):
    owner = ProductPostgresStore(isolated_postgres_url)
    pool = open_postgres_pool(isolated_postgres_url, min_size=1, max_size=8)
    restricted = ProductPostgresStore(pool=pool, app_role="weaves_app")
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=restricted, seed_local_defaults=False
    )
    runtime.bootstrap()
    require_tenant_context(True)  # production posture: no implicit local tenant
    try:
        world = cases.onboarding_world(runtime, monkeypatch)
        world["owner_store"] = owner
        with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
            world["client"] = client
            yield world
    finally:
        require_tenant_context(False)
        runtime.close()
        owner.close()


def test_role_title_is_set_validated_cleared_and_private_to_the_account(world):
    cases.role_title_case(world)


def test_memberships_say_who_owns_and_which_role_to_show(world):
    cases.membership_roles_case(world)


def test_creation_stores_and_returns_timezone_and_locale(world):
    cases.creation_settings_case(world)


def test_throttled_creation_answers_429_with_retry_after(world):
    cases.throttle_case(world)


def test_the_cap_and_the_disabled_flag_show_on_the_me_payload(world, monkeypatch):
    cases.cap_case(world, monkeypatch)


def test_slug_availability_is_exact_private_and_budgeted(world):
    identity.slug_availability_case(world)


def test_creation_reserves_an_explicit_or_derived_slug(world):
    identity.slug_creation_case(world)


def test_an_admin_changes_the_slug_and_export_includes_it(world):
    identity.slug_settings_case(world)


def test_a_custom_email_domain_belongs_to_one_organization(world):
    identity.domain_claim_case(world)


def test_the_domain_kill_switch_and_the_extended_public_list(world, monkeypatch):
    identity.domain_switches_case(world, monkeypatch)


def test_racing_creators_on_one_custom_domain_yield_one_organization(world):
    identity.domain_race_case(world)


def test_racing_creators_on_one_slug_yield_one_holder(world):
    identity.slug_race_case(world)


def _sql(world, query, *params):
    with world["owner_store"].connection() as connection:
        cursor = connection.execute(query, params or None)
        return cursor.fetchall() if cursor.description else []


def _identity_rows(world):
    """(organization name, slug, email domain) for every settings row."""
    return sorted(
        _sql(
            world,
            "SELECT o.payload->>'name', s.payload->>'slug', "
            "s.payload->>'email_domain' FROM product_records s "
            "JOIN product_records o ON o.collection = 'organizations' "
            "AND o.record_id = s.record_id "
            "WHERE s.collection = 'organization_settings'",
        ),
        key=lambda row: (row[0], row[1] or ""),
    )


def _backfill(world):
    for statement in (SLUG_BACKFILL, EMAIL_DOMAIN_BACKFILL):
        _sql(world, statement)


def _set(world, field, value, org_id):
    _sql(
        world,
        "UPDATE product_records SET payload = "
        "jsonb_set(payload, ARRAY[%s], to_jsonb(%s::text)) "
        "WHERE collection = 'organization_settings' AND record_id = %s",
        field,
        value,
        org_id,
    )


def test_the_database_refuses_a_second_holder_of_a_slug_or_domain(world):
    assert SCHEMA_HEAD_VERSION == 24
    for field, held, wanted in (
        ("slug", "org-b", "ORG-B"),
        ("email_domain", "dup.test", "Dup.Test"),
    ):
        _set(world, field, held, world["b"][0].id)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _set(world, field, wanted, world["a"][0].id)


def test_the_backfill_is_deterministic_idempotent_and_never_guesses(world, monkeypatch):
    def made(email, key, name):
        response = cases.create(world, cases.token_for(world, email), key, name=name)
        assert response.status_code == 201, response.text

    made("ana@corp.test", "b-1", "Corp")
    monkeypatch.setenv("WEAVES_ENFORCE_ORGANIZATION_DOMAINS", "false")
    made("d1@dup.test", "b-2", "Dup One")
    made("d2@dup.test", "b-3", "Dup Two")
    made("pat@gmail.com", "b-4", "Admin")
    made("pat@gmail.com", "b-5", "Org A")
    made("pat@gmail.com", "b-6", "!!")
    monkeypatch.setenv("WEAVES_ENFORCE_ORGANIZATION_DOMAINS", "true")
    blank = (
        "UPDATE product_records SET payload = payload - 'slug' - 'email_domain' "
        "WHERE collection = 'organization_settings'"
    )
    _sql(world, blank)
    assert {row[1:] for row in _identity_rows(world)} == {(None, None)}
    _backfill(world)
    first = _identity_rows(world)
    assert first == [
        ("!!", "org", None),
        ("Admin", "admin-2", None),
        ("Corp", "corp", "corp.test"),
        ("Dup One", "dup-one", None),
        ("Dup Two", "dup-two", None),
        ("Org A", "org-a", None),
        ("Org A", "org-a-2", None),
        ("Org B", "org-b", None),
    ]
    _backfill(world)
    assert _identity_rows(world) == first
    # A later run restores a cleared value and never moves one that is set.
    _sql(world, blank + " AND payload->>'slug' IN ('corp', 'dup-one')")
    _backfill(world)
    assert _identity_rows(world) == first
    status = cases.me(world, cases.token_for(world, "ana@corp.test"))
    assert status["organization_creation"]["domain_organization_name"] == "Corp"
