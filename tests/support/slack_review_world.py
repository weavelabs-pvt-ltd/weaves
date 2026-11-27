"""A Slack PR review world: the Ask world plus a GitHub connection over the fake GitHub.

Real repositories, the real GitHub review plugin over ``httpx.MockTransport`` backed
by ``tenancy_harness.fake_github`` (recorded pull requests, an injection attempt in
a description and a diff), the real dispatcher, worker guard and reply tick, and a
scripted review model (a hook in front of the model gateway). Nothing here reaches
a network or a real key.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from contracts.v1 import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PluginInstallation,
    PluginInstallationStatus,
)
from contracts.v1.slack_access import SlackAllowedChannel, SlackAllowedUser
from modules.slack.review.prompt import REVIEW_INSTRUCTIONS
from tenancy_harness import fake_github
from tests.support.slack_ask_world import AskWorld, build_ask_world
from tests.support.slack_pipeline_world import CHANNEL, ORG, USER

GITHUB = "github"
REPOS = ("acme/api", "acme/private")
MEMBER = "local-developer"
GOOD_REVIEW = {
    "summary": "Adds exponential backoff to the payments client.",
    "risk": "medium",
    "findings": [
        {
            "file": "payments/client.py",
            "issue": "Retries are unbounded on 5xx responses.",
            "severity": "high",
        },
        {
            "file": "tests/test_retry.py",
            "issue": "There is no timeout test for the new retry loop.",
            "severity": "low",
        },
    ],
    "question": "Should retries apply to POST /charge?",
}


class FakeTokens:
    """An installation-token provider that records the scope it was asked for."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, tuple[str, ...]]] = []

    def installation_token(
        self, installation_id: str, repositories: tuple[str, ...]
    ) -> str:
        self.asked.append((installation_id, repositories))
        return fake_github.TOKEN


@dataclass
class ReviewWorld:
    """The Ask world, a GitHub connection and a scripted review model."""

    aworld: AskWorld
    tokens: FakeTokens
    review_requests: list[ModelRequest] = field(default_factory=list)
    review_calls: int = 0
    reply: Callable[[ModelRequest], str] = lambda _: json.dumps(GOOD_REVIEW)
    github_installation: PluginInstallation | None = None

    @property
    def runtime(self) -> Any:
        """The runtime under test."""
        return self.aworld.runtime

    def allow(
        self,
        *,
        rule: str = "user_only",
        repos: tuple[str, ...] = ("acme/api",),
        mapped: bool = True,
        listed: bool = True,
        channel_listed: bool = False,
        user: str = USER,
    ) -> None:
        """Turn the review rule on for the asker and allowlist repositories."""
        w = self.aworld.world
        w.set_slack_settings(
            rules={
                "create_task": "off",
                "pr_review": rule,
                "run_workflow": "off",
                "ask_private": "off",
            },
            pr_review_repos=list(repos),
        )
        now = datetime.now(UTC)
        if listed:
            self.runtime.slack_allowed_users.put(
                SlackAllowedUser(
                    id=f"{ORG}:{user}",
                    org_id=ORG,
                    slack_user_id=user,
                    principal_id=MEMBER if mapped else None,
                    added_by=MEMBER,
                    created_at=now,
                    updated_at=now,
                )
            )
        if channel_listed:
            self.runtime.slack_allowed_channels.put(
                SlackAllowedChannel(
                    id=f"{ORG}:{CHANNEL}",
                    org_id=ORG,
                    channel_id=CHANNEL,
                    name_cached="general",
                    added_by=MEMBER,
                    created_at=now,
                    updated_at=now,
                )
            )

    def review(self, number: int, repo: str = "acme/api", **kw: Any) -> Any:
        """Mention ``review <url>`` and return the inbox event."""
        url = f"https://github.com/{repo}/pull/{number}"
        return self.aworld.mention(f"review {url}", **kw)

    def run_all(self) -> Any:
        """Dispatch, work and reply; returns the finished turn (or ``None``)."""
        self.aworld.dispatch()
        turn = self.aworld.work() if self.aworld.threads() else None
        self.aworld.reply()
        return turn

    def tasks(self) -> list[Any]:
        """Every task in the organization."""
        return list(self.runtime.tasks.list())


def build_review_world(*, covered: bool = True) -> ReviewWorld:
    """A world whose GitHub connection predates the review capability."""
    aworld = build_ask_world()
    runtime = aworld.runtime
    tokens = FakeTokens()
    world = ReviewWorld(aworld, tokens)

    def handler(request: httpx.Request) -> httpx.Response:
        status, payload, headers = fake_github.handle(
            request.method,
            request.url.path,
            dict(request.url.params),
            request.headers.get("authorization", ""),
            None,
        )
        return httpx.Response(status, json=payload, headers=headers)

    plugin = runtime.github_plugin
    plugin._client = httpx.Client(transport=httpx.MockTransport(handler))
    plugin._installation_tokens = tokens
    aworld.world.clients.append(plugin._client)
    fake_github.STATE.reset()
    # An active, linked member for the mapped principal (the local owner).
    runtime.slack_access_service.allowlists.members.is_active = lambda _pid: True
    now = datetime.now(UTC)
    if covered:
        world.github_installation = runtime.installations.put(
            PluginInstallation(
                plugin_installation_id="gh-install-1",
                org_id=ORG,
                workspace_id="local-workspace",
                plugin_id=GITHUB,
                display_name="GitHub",
                configuration={
                    "auth_method": "github_app",
                    "github_installation_id": "12345",
                    "repositories": list(REPOS),
                },
                enabled_capability_ids=("github.issues.search", "github.code.search"),
                status=PluginInstallationStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )
    gateway = runtime.model_gateway
    original = gateway.complete

    def complete(request: ModelRequest, binding: Any) -> ModelResponse:
        if REVIEW_INSTRUCTIONS not in request.messages[0].content:
            return original(request, binding)  # type: ignore[no-any-return]
        world.review_calls += 1
        world.review_requests.append(request)
        return ModelResponse(
            provider_id=binding.provider.id,
            model_id=binding.profile.model,
            content=world.reply(request),
            usage=ModelUsage(input_tokens=900, output_tokens=120, total_tokens=1020),
        )

    gateway.complete = complete
    return world
