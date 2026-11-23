"""Per-organization and per-principal request token buckets and their mode."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.ports.limits import StaticLimitPolicy
from apps.api.limit_errors import install_limit_error_handlers
from apps.api.request_limits import RequestRateLimiter, install_request_limits
from shared.limits_config import LimitsConfig
from shared.request_context import reset_request_principal, set_request_identity


def limits(**settings: object) -> Any:
    return StaticLimitPolicy(LimitsConfig(**settings)).for_org("org")  # type: ignore[arg-type]


def test_a_burst_drains_then_refills_at_the_per_minute_rate() -> None:
    now = [0.0]
    bucket = RequestRateLimiter(clock=lambda: now[0])
    config = limits(
        request_burst=3, requests_per_minute_org=60, requests_per_minute_principal=60
    )
    assert [bucket.acquire("o", "p", config) for _ in range(3)] == [None] * 3
    wait = bucket.acquire("o", "p", config)
    assert wait == 1
    now[0] += 1.0
    assert bucket.acquire("o", "p", config) is None
    assert bucket.acquire("o", "p", config) == 1


def test_one_principal_cannot_exhaust_the_organization_for_its_peers() -> None:
    now = [0.0]
    bucket = RequestRateLimiter(clock=lambda: now[0])
    config = limits(
        request_burst=2, requests_per_minute_org=600, requests_per_minute_principal=60
    )
    assert bucket.acquire("o", "noisy", config) is None
    assert bucket.acquire("o", "noisy", config) is None
    now[0] += 0.2  # the organization bucket refills; the principal's barely does
    assert bucket.acquire("o", "noisy", config) is not None
    assert bucket.acquire("o", "quiet", config) is None


def test_the_organization_bucket_limits_all_its_principals_together() -> None:
    bucket = RequestRateLimiter(clock=lambda: 0.0)
    config = limits(
        request_burst=2, requests_per_minute_org=60, requests_per_minute_principal=600
    )
    assert bucket.acquire("o", "a", config) is None
    assert bucket.acquire("o", "b", config) is None
    assert bucket.acquire("o", "c", config) is not None
    assert bucket.acquire("other", "a", config) is None


def app_for(mode: str) -> TestClient:
    app = FastAPI()
    install_limit_error_handlers(app)
    install_request_limits(app)
    app.state.limit_policy = StaticLimitPolicy(
        LimitsConfig(
            mode=mode,  # type: ignore[arg-type]
            request_burst=2,
            requests_per_minute_org=60,
            requests_per_minute_principal=60,
        )
    )

    @app.middleware("http")
    async def bind_identity(request: Any, call_next: Any) -> Any:
        org = request.headers.get("x-org")
        if org is None:
            return await call_next(request)
        token = set_request_identity("principal", org, "workspace")
        try:
            return await call_next(request)
        finally:
            reset_request_principal(token)

    @app.get("/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


def test_enforce_answers_429_limit_exceeded_with_retry_after() -> None:
    client = app_for("enforce")
    headers = {"x-org": "org-a"}
    assert [client.get("/ping", headers=headers).status_code for _ in range(2)] == [
        200,
        200,
    ]
    refused = client.get("/ping", headers=headers)
    assert refused.status_code == 429
    body = refused.json()
    assert body["code"] == "limit_exceeded" and body["resource"] == "requests"
    assert body["limit"] == 60 and "used" not in body
    assert int(refused.headers["Retry-After"]) >= 1
    assert client.get("/ping", headers={"x-org": "org-b"}).status_code == 200


def test_observe_serves_everything_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = app_for("observe")
    with caplog.at_level(logging.WARNING, logger="product.limits"):
        statuses = [
            client.get("/ping", headers={"x-org": "o"}).status_code for _ in range(6)
        ]
    assert statuses == [200] * 6
    assert caplog.text.count("request rate would be limited") == 1


def test_off_and_unauthenticated_requests_are_never_limited() -> None:
    off = app_for("off")
    assert {off.get("/ping", headers={"x-org": "o"}).status_code for _ in range(8)} == {
        200
    }
    enforce = app_for("enforce")
    assert {enforce.get("/ping").status_code for _ in range(8)} == {200}
