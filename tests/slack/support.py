"""Test world for the Slack install and events slices (no real Slack, no secrets)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from clients.slack.signature import compute_signature
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from modules.slack.install_settings import BOT_SCOPES
from shared.request_context import require_tenant_context
from tests.support.organizations import onboard

SIGNING_SECRET = "made-up-test-signing-secret"
BOT_TOKEN = "xoxb-test-bot-token-must-never-leak"
EVENTS = "/api/v0/slack/events"
START = "/api/v0/oauth/slack/install"
CALLBACK = "/api/v0/oauth/slack/install/callback"
STATUS = "/api/v0/organization/slack"


def access_payload(
    team: str = "T0ACME001",
    token: str = BOT_TOKEN,
    *,
    enterprise_install: bool = False,
    scope: str | None = None,
    team_name: str = "Acme Workspace",
) -> dict[str, Any]:
    """An ``oauth.v2.access`` answer for a bot install."""
    return {
        "ok": True,
        "app_id": "A0TEST001",
        "access_token": token,
        "token_type": "bot",
        "scope": ",".join(BOT_SCOPES) if scope is None else scope,
        "bot_user_id": "U0BOT0001",
        "team": None if enterprise_install else {"id": team, "name": team_name},
        "enterprise": {"id": "E0GRID001", "name": "Grid"}
        if enterprise_install
        else None,
        "is_enterprise_install": enterprise_install,
        "authed_user": {"id": "U0ADMIN01"},
    }


@dataclass
class FakeSlackApi:
    """An ``httpx`` transport that records Web API calls (tokens are not kept)."""

    access: dict[str, Any] = field(default_factory=access_payload)
    errors: dict[str, str] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        """The mock transport to hand to ``httpx.Client``."""
        return httpx.MockTransport(self._handle)

    def methods(self) -> list[str]:
        """Names of the methods called so far, in order."""
        return [call["method"] for call in self.calls]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = urlsplit(str(request.url)).path.rsplit("/", 1)[-1]
        params = {k: v[-1] for k, v in parse_qs(request.content.decode()).items()}
        auth = request.headers.get("authorization", "")
        self.tokens.append(auth.removeprefix("Bearer "))
        self.calls.append({"method": method, "params": params, "bearer": bool(auth)})
        if method in self.errors:
            return httpx.Response(200, json={"ok": False, "error": self.errors[method]})
        if method == "oauth.v2.access":
            return httpx.Response(200, json=self.access)
        return httpx.Response(200, json={"ok": True})


@dataclass
class Org:
    """One organization with its admin headers."""

    org_id: str
    workspace_id: str
    headers: dict[str, str]


@dataclass
class World:
    """A running API with one or two organizations and the fake Slack."""

    client: TestClient
    runtime: Any
    api: FakeSlackApi
    alpha: Org
    beta: Org

    def sign(self, body: bytes, *, secret: str = SIGNING_SECRET, age: int = 0) -> dict:
        """Slack's signature headers for ``body``."""
        stamp = str(int(time.time()) - age)
        signature = compute_signature(secret.encode(), stamp, body)
        return {
            "x-slack-request-timestamp": stamp,
            "x-slack-signature": signature,
            "content-type": "application/json",
        }

    def deliver(
        self, payload: dict[str, Any], *, extra: dict[str, str] | None = None, **kw: Any
    ) -> httpx.Response:
        """POST a signed event to the endpoint."""
        body = json.dumps(payload).encode()
        headers = {**self.sign(body, **kw), **(extra or {})}
        return self.client.post(EVENTS, content=body, headers=headers)

    def install(self, org: Org | None = None, code: str = "code-1") -> httpx.Response:
        """Run start and callback as ``org`` (alpha by default)."""
        org = org or self.alpha
        started = self.client.get(START, headers=org.headers)
        assert started.status_code == 200, started.text
        query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
        return self.client.get(
            CALLBACK,
            params={"code": code, "state": query["state"][0]},
            follow_redirects=False,
        )

    def status(self, org: Org | None = None) -> dict[str, Any]:
        """The status view for ``org``."""
        response = self.client.get(STATUS, headers=(org or self.alpha).headers)
        assert response.status_code == 200, response.text
        return dict(response.json())


def mention(
    *,
    event_id: str = "Ev0AAA001",
    team: str = "T0ACME001",
    channel: str = "C0CHAN001",
    ts: str = "1700000000.000100",
    user: str = "U0HUMAN01",
    text: str = "<@U0BOT0001> what is open?",
) -> dict[str, Any]:
    """An ``app_mention`` event_callback."""
    return {
        "type": "event_callback",
        "team_id": team,
        "event_id": event_id,
        "event": {
            "type": "app_mention",
            "user": user,
            "text": text,
            "ts": ts,
            "channel": channel,
        },
    }


def lifecycle_event(kind: str, *, team: str = "T0ACME001", **event: Any) -> dict:
    """An ``app_uninstalled`` or ``tokens_revoked`` callback."""
    return {
        "type": "event_callback",
        "team_id": team,
        "event_id": f"Ev0{kind[:3].upper()}001",
        "event": {"type": kind, **event},
    }


def _organization(client: TestClient, name: str, email: str) -> Org:
    body = onboard(client, name=name, owner_email=email, owner_display_name="Owner")
    headers = {
        "Authorization": f"Bearer {body['access_token']}",
        "X-Workspace-ID": body["workspace"]["id"],
    }
    return Org(body["organization"]["id"], body["workspace"]["id"], headers)


def slack_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """The deployment settings of a Slack-enabled API."""
    values = {
        "WEAVES_SLACK_ENABLED": "true",
        "WEAVES_SLACK_SIGNING_SECRET": SIGNING_SECRET,
        "WEAVES_SLACK_OAUTH_CLIENT_ID": "client-id-1",
        "WEAVES_SLACK_OAUTH_CLIENT_SECRET": "client-secret-1",
        "WEAVES_PUBLIC_API_BASE_URL": "https://api.example.test",
        "WEAVES_PUBLIC_FRONTEND_URL": "https://app.example.test",
        **overrides,
    }
    for name, value in values.items():
        if value == "":
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


@contextmanager
def running_world(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_url: str | None = None,
    **overrides: str,
) -> Iterator[World]:
    """Start an API and two orgs: memory storage, or PostgreSQL as ``weaves_app``."""
    slack_env(monkeypatch, **overrides)
    api = FakeSlackApi()
    store = None
    if database_url is not None:
        store = ProductPostgresStore(
            pool=open_postgres_pool(database_url), app_role="weaves_app"
        )
    runtime = compose_platform_runtime(
        http_client=httpx.Client(transport=api.transport()),
        store=store,
        seed_local_defaults=False,
    )
    if store is not None:
        require_tenant_context(True)
    try:
        with TestClient(create_app(runtime)) as client:
            yield World(
                client,
                runtime,
                api,
                _organization(client, "Alpha", "owner@alpha.test"),
                _organization(client, "Beta", "owner@beta.test"),
            )
    finally:
        require_tenant_context(False)
        runtime.close()


def forbid_database(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the store was touched")

    for name in ("find_by_team", "record_event", "set_status", "claim"):
        monkeypatch.setattr(world.runtime.slack_store, name, boom)
