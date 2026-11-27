"""A Slack-ready local runtime with counting fake connectors and a fake model.

Real repositories, real Jira and Slack search plugins over ``httpx.MockTransport``
servers that COUNT their requests (split into health probes and data requests),
the real model gateway with an instance-level hook that counts planner and answer
calls, and the real pipeline, guard and worker. Nothing here reaches a network.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from application.ports.slack import SlackInstallation
from contracts.v1 import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PluginInstallation,
    PluginInstallationStatus,
    TaskTurn,
    TurnOrigin,
)
from contracts.v1.slack_access import SlackConnectorHealth, SlackSettingsFields
from modules.slack.access.decision_audit import DecisionContext, DecisionRecorder
from modules.slack.access.models import AccessRequest, Action
from modules.slack.pipeline.discovery import ToolDiscovery
from modules.slack.pipeline.health import HealthSnapshots
from modules.slack.pipeline.models import PipelineRequest, PlannerTarget
from modules.slack.pipeline.pipeline import SlackPipeline
from modules.slack.pipeline.planner import SYSTEM_PROMPT, AskPlanner
from tests.support.runtime import LocalPlatformRuntime
from tests.support.slack_access_cases import GOOD_MEMBER, NOW, settings

ORG = "local-org"
WORKSPACE = "local-workspace"
TEAM = "T0FAKE001"
USER = "U0ASKER01"
CHANNEL = "C0CHAN001"
MARKER = "ZEBRA-QUESTION-TEXT-7731"
JIRA = "jira.issues.search"
SLACK = "slack.messages.search"


class FakeConnectors:
    """Two fake connector servers that count what they are asked."""

    def __init__(self) -> None:
        """Start with both servers healthy."""
        self.seen: Counter[tuple[str, str]] = Counter()
        self.fail_probe: set[str] = set()
        self.issues: list[dict[str, Any]] = []

    def count(self, connector: str, kind: str | None = None) -> int:
        """Requests seen by one connector (``probe``, ``data`` or both)."""
        if kind is not None:
            return self.seen[(connector, kind)]
        return sum(n for (name, _), n in self.seen.items() if name == connector)

    def total(self, kind: str | None = None) -> int:
        """Requests seen by every connector."""
        return sum(n for (_, k), n in self.seen.items() if kind is None or k == kind)

    def jira(self, request: httpx.Request) -> httpx.Response:
        """The fake Jira Cloud server."""
        if "/project/" in request.url.path:
            self.seen[("jira", "probe")] += 1
            if "jira" in self.fail_probe:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"key": "WEAVES"})
        self.seen[("jira", "data")] += 1
        return httpx.Response(200, json={"issues": self.issues})

    def slack(self, request: httpx.Request) -> httpx.Response:
        """The fake Slack Web API server of the search connector."""
        if request.url.path.endswith("/auth.test"):
            self.seen[("slack", "probe")] += 1
            if "slack" in self.fail_probe:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"ok": True, "team": "Fake"})
        self.seen[("slack", "data")] += 1
        return httpx.Response(200, json={"ok": True, "messages": {"matches": []}})


class FakeModels:
    """Counts planner and answer calls; the planner's reply is scripted."""

    def __init__(self) -> None:
        """Start with a planner that needs nothing."""
        self.planner_calls = 0
        self.answer_calls = 0
        self.planner_prompts: list[str] = []
        self.reply: Callable[[ModelRequest], str] = lambda _: json.dumps(
            {"needs": [], "general_ok": True}
        )
        self.delay = 0.0
        self.tokens = (500, 40)

    def hook(self, gateway: Any) -> None:
        """Route the runtime gateway's calls through this counter."""
        original = gateway.complete

        def complete(request: ModelRequest, binding: Any) -> ModelResponse:
            if request.messages[0].content != SYSTEM_PROMPT:
                self.answer_calls += 1
                return original(request, binding)  # type: ignore[no-any-return]
            self.planner_calls += 1
            self.planner_prompts.append(request.messages[1].content)
            if self.delay:
                time.sleep(self.delay)
            tokens_in, tokens_out = self.tokens
            return ModelResponse(
                provider_id=binding.provider.id,
                model_id=binding.profile.model,
                content=self.reply(request),
                usage=ModelUsage(
                    input_tokens=tokens_in,
                    output_tokens=tokens_out,
                    total_tokens=tokens_in + tokens_out,
                ),
            )

        gateway.complete = complete


@dataclass
class World:
    """One Slack-ready organization on the in-memory runtime."""

    runtime: Any
    connectors: FakeConnectors = field(default_factory=FakeConnectors)
    models: FakeModels = field(default_factory=FakeModels)
    jira: PluginInstallation | None = None
    slack: PluginInstallation | None = None
    clients: list[httpx.Client] = field(default_factory=list)
    serial: int = 0
    agent_id: str = "local-assistant"

    def close(self) -> None:
        """Release the runtime and the fake connector clients."""
        for client in self.clients:
            client.close()
        self.runtime.close()

    # -- connectors and snapshots ------------------------------------------

    def snapshot(
        self, installation: PluginInstallation, status: str, age_minutes: int = 0
    ) -> None:
        """Seed a health snapshot ``age_minutes`` old."""
        checked = datetime.now(UTC) - timedelta(minutes=age_minutes)
        self.runtime.slack_connector_health.put(
            SlackConnectorHealth(
                id=installation.plugin_installation_id,
                org_id=ORG,
                installation_id=installation.plugin_installation_id,
                status=status,  # type: ignore[arg-type]
                checked_at=checked,
                created_at=checked,
                updated_at=checked,
            )
        )

    def disable(self, installation: PluginInstallation) -> None:
        """Turn a connector installation off."""
        self.runtime.installations.put(
            installation.model_copy(
                update={"status": PluginInstallationStatus.DISABLED}
            )
        )

    def set_slack_settings(self, **changes: Any) -> None:
        """Replace the Slack access settings (connector audiences and so on)."""
        current = self.runtime.slack_access_service.view()
        fields = {name: current[name] for name in SlackSettingsFields.model_fields}
        fields.update(changes)
        self.runtime.slack_access_service.update(
            SlackSettingsFields(**fields), current["settings_version"]
        )

    # -- the pipeline in isolation -----------------------------------------

    def pipeline(self, planner_timeout: float = 5.0) -> SlackPipeline:
        """The real pipeline over this world's repositories and fake model."""
        snapshots = HealthSnapshots(
            self.runtime.slack_connector_health,
            self.runtime.integration_admin_service.probe_plugin_installation,
        )
        return SlackPipeline(
            discovery=ToolDiscovery(
                installations=self.runtime.installations,
                plugins=self.runtime.plugins,
                snapshots=snapshots,
            ),
            planner=AskPlanner(
                self.runtime.model_gateway,
                self.runtime.usage_service,
                timeout_seconds=planner_timeout,
            ),
            recorder=DecisionRecorder(self.runtime.audit_events),
        )

    def planner_target(self) -> PlannerTarget:
        """The local model profile the planner runs on."""
        profile = self.runtime.model_profiles.get("local-model-profile")
        provider = self.runtime.providers.get(profile.provider_id)
        return PlannerTarget(profile, provider, None, WORKSPACE, "local-assistant")

    def request(
        self,
        question: str,
        access: AccessRequest,
        *,
        workflow_capability_ids: tuple[str, ...] = (),
        with_planner: bool = True,
    ) -> PipelineRequest:
        """A pipeline request with a fresh request id and planner usage key."""
        self.serial += 1
        request_id = f"slack-evt{self.serial}"
        return PipelineRequest(
            access=access,
            context=DecisionContext(
                org_id=ORG,
                event_id=f"evt{self.serial}",
                request_id=request_id,
                slack_user_id=access.slack_user_id,
                channel_id=access.channel_id,
            ),
            usage_key=f"task:turn{self.serial}:run:1:plan",
            question=question,
            planner_target=self.planner_target() if with_planner else None,
            workspace_id=WORKSPACE,
            workflow_capability_ids=workflow_capability_ids,
        )

    # -- the worker, end to end --------------------------------------------

    def queued_turn(
        self,
        question: str,
        *,
        origin: dict[str, Any] | None = None,
        agent_id: str | None = None,
    ) -> TaskTurn:
        """Create a task whose queued turn carries a Slack origin."""
        self.serial += 1
        task = self.runtime.task_service.create_task(
            agent_id=agent_id or self.agent_id,
            title="Slack question",
            initial_message=question,
        )
        turn = self.runtime.task_turns.get(task.active_turn_id)
        fields: dict[str, Any] = {
            "kind": "slack_qa",
            "team_id": TEAM,
            "channel_id": CHANNEL,
            "thread_ts": "1700000000.000100",
            "slack_user_id": USER,
            "event_id": f"evt{self.serial}",
            "settings_version": 1,
            "action": "ask",
            **(origin or {}),
        }
        return self.runtime.task_turns.put(
            turn.model_copy(update={"origin": TurnOrigin(**fields)})
        )

    def process(self) -> TaskTurn:
        """Run the worker once and return the finished turn."""
        done = self.runtime.task_worker_service.process_next_turn("test-worker")
        assert done is not None
        return done


def build_world(
    *, with_jira: bool = True, capabilities: tuple[str, ...] = (JIRA, SLACK)
) -> World:
    """A runtime with Jira and Slack search installed, a Q&A agent and Slack bound."""
    runtime = LocalPlatformRuntime()
    world = World(runtime=runtime)
    runtime.bootstrap()
    world.models.hook(runtime.model_gateway)
    runtime.jira_plugin._client = _client(world, world.connectors.jira)
    runtime.slack_plugin._client = _client(world, world.connectors.slack)
    connection = runtime.integration_connection_service

    def bundle(provider: str) -> str:
        return json.dumps(
            {"weaves_oauth": True, "provider": provider, "access_token": "fixture"}
        )

    if with_jira:
        world.jira = connection.create_jira_oauth_installation(
            display_name="Jira",
            token_bundle=bundle("jira"),
            site_url="https://example.atlassian.net",
            cloud_id="cloud-weaves",
            project_keys=("WEAVES",),
        )
    world.slack = connection.create_slack_oauth_installation(
        display_name="Slack search",
        token_bundle=bundle("slack"),
        channels=("engineering",),
    )
    agent = runtime.agent_service.create_agent(
        name="Slack Q&A",
        instructions="Answer questions from the connected sources.",
        allowed_capability_ids=capabilities,
    )
    world.agent_id = agent.id
    now = datetime.now(UTC)
    runtime.slack_store.claim(
        SlackInstallation(
            installation_id="slack-inst-1",
            team_id=TEAM,
            org_id=ORG,
            workspace_id=WORKSPACE,
            bot_user_id="U0BOT",
            installed_by_principal_id="local-developer",
            installed_at=now,
            updated_at=now,
        )
    )
    return world


def _client(world: World, handler: Callable[[httpx.Request], httpx.Response]) -> Any:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    world.clients.append(client)
    return client


def ask_access(**overrides: Any) -> AccessRequest:
    """A passing Ask request with no tools (the pipeline fills them in)."""
    from tests.support.slack_access_cases import request

    base: dict[str, Any] = {"now": datetime.now(UTC), "settings": settings()}
    base.update(overrides)
    return request(Action.ASK, **base)


__all__ = ["GOOD_MEMBER", "NOW", "World", "ask_access", "build_world"]
