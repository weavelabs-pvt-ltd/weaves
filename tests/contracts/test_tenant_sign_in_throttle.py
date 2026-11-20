"""The same email has independent sign-in allowances in each organization."""

import pytest

from infrastructure.auth.throttle import InMemoryLoginRateLimiter, login_bucket_key
from shared.errors import LoginThrottled
from tests.support.runtime import LocalPlatformRuntime


@pytest.fixture
def auth():
    runtime = LocalPlatformRuntime(postgres_store=None)
    service = runtime.authentication_service
    service.login_rate_limiter = InMemoryLoginRateLimiter(limit=2, window_seconds=600)
    yield service
    runtime.close()


def test_failed_attempts_in_one_tenant_do_not_consume_the_other_tenants_allowance(
    auth,
):
    email = "shared@example.com"
    for _ in range(2):
        auth._consume_email_login_limits(email, None, "org-a")
    with pytest.raises(LoginThrottled):
        auth._consume_email_login_limits(email, None, "org-a")

    auth._consume_email_login_limits(email, None, "org-b")
    auth._consume_email_login_limits(email, None, "org-b")


def test_global_source_limit_still_applies_across_tenants(auth):
    email = "shared@example.com"
    auth._consume_email_login_limits(email, "203.0.113.9", "org-a")
    auth._consume_email_login_limits(email, "203.0.113.9", "org-b")
    with pytest.raises(LoginThrottled):
        auth._consume_email_login_limits("other@example.com", "203.0.113.9", "org-c")


def test_password_bucket_is_scoped_per_organization_and_stays_private():
    plain = login_bucket_key("account", "owner@example.com")
    scoped_a = login_bucket_key("account", "OWNER@example.com", "org-a")
    scoped_b = login_bucket_key("account", "owner@example.com", "org-b")

    assert len({plain, scoped_a, scoped_b}) == 3
    assert "owner" not in scoped_a and "org-a" not in scoped_a
    assert scoped_a == login_bucket_key("account", "owner@example.com", "org-a")
