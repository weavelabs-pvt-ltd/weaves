"""A started app wired to a fake Google provider, with one organization."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from tests.support.account_session import ORIGIN, configure_google
from tests.support.fake_google import FakeGoogle
from tests.support.organizations import create_organization

EMAIL = "person@example.test"
BINDING_COOKIE = "__Host-weaves_google_oauth"


@dataclass
class GoogleWorld:
    """Everything one Google sign-in test needs."""

    runtime: Any
    client: TestClient
    fake: FakeGoogle
    org: Any
    user: Any

    @property
    def service(self) -> Any:
        """The account directory service."""
        return self.runtime.account_sign_in_service

    @property
    def google(self) -> Any:
        """The configured Google sign-in flow, or None when unconfigured."""
        return self.client.app.state.google_sign_in


def cookies_of(response: Any) -> dict[str, Any]:
    """Parse every Set-Cookie header of a response into morsels by name."""
    found: dict[str, Any] = {}
    for header in response.headers.get_list("set-cookie"):
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(header)
        found.update(cookie)
    return found


@pytest.fixture
def google_world(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., GoogleWorld]]:
    """Return a factory that builds a started world; closed after the test."""
    stack = ExitStack()

    def build(*, configured: bool = True, **env: str) -> GoogleWorld:
        if configured:
            configure_google(monkeypatch, **env)
        else:
            monkeypatch.setenv("WEAVES_PUBLIC_FRONTEND_URL", ORIGIN)
            monkeypatch.setenv("WEAVES_BROWSER_SESSION_SECRET", "browser-secret-" * 4)
            for name in ("WEAVES_GOOGLE_CLIENT_ID", "WEAVES_GOOGLE_CLIENT_SECRET"):
                monkeypatch.delenv(name, raising=False)
            for name, value in env.items():
                monkeypatch.setenv(name, value)
        fake = FakeGoogle()
        runtime = compose_platform_runtime(
            http_client=fake.client(), store=None, seed_local_defaults=False
        )
        stack.callback(runtime.close)
        runtime.bootstrap()
        org, _, user, _, _ = create_organization(
            runtime, name="Org A", email=EMAIL, display_name="Person A"
        )
        client = TestClient(
            create_app(runtime), base_url=ORIGIN, raise_server_exceptions=False
        )
        stack.enter_context(client)
        return GoogleWorld(runtime, client, fake, org, user)

    yield build
    stack.close()
