"""A Slack Ask world: signed-in-process mentions, the real ticks, a fake Slack Web API.

Built on the pipeline world (real repositories, counting fake connectors, a fake
planner, the real worker). Adds a recording Slack Web API (``httpx.MockTransport``)
with injectable faults, a stored bot token, and helpers that drive the three
ticks the way the worker loop does. No network, no real secret.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from application.ports.slack import InboxEvent
from apps.runtime_services_slack_dispatch import attach_slack_dispatch
from clients.slack.client import SlackApiClient
from contracts.v1 import TaskTurnStatus
from modules.slack.interactions.ingest import SlackInteractionIngest
from modules.slack.interactions.payload import Press
from tests.support import slack_pipeline_world as base
from tests.support.slack_pipeline_world import CHANNEL, TEAM, USER, World

BOT_TOKEN = "xoxb-ask-world-token-never-logged"
BOT_USER = "U0BOT"


@dataclass
class FakeSlackWeb:
    """Records the bot's Web API calls; faults are popped one per call."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    ephemerals: list[dict[str, Any]] = field(default_factory=list)
    faults: dict[str, list[str]] = field(default_factory=dict)
    status_supported: bool = True
    sequence: int = 0
    workspace_url: str = "https://acme-test.slack.com/"

    def client(self) -> SlackApiClient:
        """A Slack client over this fake (no sleeping on rate limits)."""
        http = httpx.Client(transport=httpx.MockTransport(self._handle))
        return SlackApiClient(
            http, api_base_url="https://slack.test/api", sleep=lambda _: None
        )

    def count(self, method: str) -> int:
        """How many times ``method`` was called (faulted calls included)."""
        return Counter(c["method"] for c in self.calls)[method]

    def texts(self) -> list[str]:
        """Current text of every message posted to a channel, in post order."""
        return [m["text"] for m in self.messages if not m.get("deleted")]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = urlsplit(str(request.url)).path.rsplit("/", 1)[-1]
        params = {k: v[-1] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append({"method": method, "params": params})
        queue = self.faults.get(method)
        if queue:
            fault = queue.pop(0)
            if fault == "429":
                return httpx.Response(
                    429, headers={"Retry-After": "120"}, json={"ok": False}
                )
            if fault == "500":
                return httpx.Response(500, json={"ok": False})
            return httpx.Response(200, json={"ok": False, "error": fault})
        if method == "auth.test":
            body = {"ok": True, "url": self.workspace_url, "team_id": TEAM}
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=self._ok(method, params))

    def _edit(self, params: dict[str, str]) -> None:
        for message in self.messages:
            if message["ts"] == params["ts"]:
                message["text"] = params["text"]
                if "blocks" in params:  # like Slack: omitted blocks are kept
                    message["blocks"] = params["blocks"]

    def _ok(self, method: str, params: dict[str, str]) -> dict[str, Any]:
        self.sequence += 1
        ts = f"1700000{self.sequence:03d}.000100"
        if method == "chat.postMessage":
            self.messages.append({**params, "ts": ts})
            return {"ok": True, "channel": params["channel"], "ts": ts}
        if method == "chat.update":
            self._edit(params)
            return {"ok": True, "channel": params["channel"], "ts": params["ts"]}
        if method == "chat.delete":
            for message in self.messages:
                if message["ts"] == params["ts"]:
                    message["deleted"] = True
            return {"ok": True}
        if method == "chat.postEphemeral":
            self.ephemerals.append(params)
            return {"ok": True, "message_ts": ts}
        if method == "assistant.threads.setStatus" and not self.status_supported:
            return {"ok": False, "error": "method_not_supported_for_channel_type"}
        return {"ok": True}


@dataclass
class AskWorld:
    """The pipeline world plus a bot token, a fake Slack and tick helpers."""

    world: World
    slack: FakeSlackWeb
    serial: int = 0
    clock_offset: float = 0.0

    @property
    def runtime(self) -> Any:
        """The runtime under test."""
        return self.world.runtime

    # -- inbox -------------------------------------------------------------------

    def mention(
        self,
        text: str,
        *,
        user: str = USER,
        channel: str = CHANNEL,
        thread_ts: str | None = None,
        event_id: str | None = None,
        received_at: datetime | None = None,
    ) -> InboxEvent:
        """Store an ``app_mention`` as the events endpoint would."""
        self.serial += 1
        event = InboxEvent(
            event_id=event_id or f"Ev{self.serial:06d}",
            team_id=TEAM,
            event_type="app_mention",
            received_at=received_at or datetime.now(UTC),
            org_id=base.ORG,
            installation_id="slack-inst-1",
            request_id=f"slack-{event_id or f'Ev{self.serial:06d}'}",
            channel_id=channel,
            user_id=user,
            message_ts=f"1699999{self.serial:03d}.000100",
            thread_ts=thread_ts,
            text=f"<@{BOT_USER}> {text}",
        )
        assert self.runtime.slack_store.record_event(event)
        return event

    # -- ticks ------------------------------------------------------------------

    def dispatch(self) -> int:
        """Run the dispatcher tick."""
        return int(self.runtime.slack_dispatcher.run())

    def reply(self) -> int:
        """Run the reply tick."""
        return int(self.runtime.slack_reply_tick.run())

    def work(self) -> Any:
        """Run the worker once (the guard and the run)."""
        return self.world.process()

    def ask(self, text: str, **kw: Any) -> Any:
        """Mention, dispatch, work and reply; returns the finished turn."""
        self.mention(text, **kw)
        self.dispatch()
        turn = self.work()
        self.reply()
        return turn

    def at(self, moment: datetime) -> None:
        """Pin the reply tick's clock (queued notice, deadlines, backoff)."""
        self.runtime.slack_reply_tick._clock = lambda: moment

    def latest(self) -> Any:
        """The only thread row (the stored copy, not a stale one)."""
        (row,) = self.runtime.slack_threads.list()
        return row

    def force_running(self, verdict: Any | None = None) -> Any:
        """Make the newest queued turn look claimed by a worker (no worker runs)."""
        row = self.latest()
        turn = self.runtime.task_turns.get(row.turn_id)
        origin = turn.origin
        if verdict is not None and origin is not None:
            origin = origin.model_copy(update={"verdict": verdict})
        return self.runtime.task_turns.put(
            turn.model_copy(
                update={
                    "status": TaskTurnStatus.RUNNING,
                    "attempts": 1,
                    "worker_id": "test-worker",
                    "started_at": datetime.now(UTC),
                    "origin": origin,
                }
            )
        )

    # -- buttons --------------------------------------------------------------------

    @staticmethod
    def buttons(message: dict[str, Any]) -> list[tuple[str, str]]:
        """``(action_id, value)`` of the buttons a message currently carries."""
        blocks = json.loads(message.get("blocks") or "[]")
        return [
            (e["action_id"], e["value"])
            for block in blocks
            if block["type"] == "actions"
            for e in block["elements"]
        ]

    def press(
        self,
        action: str,
        *,
        user: str = USER,
        ref: str | None = None,
        message_ts: str | None = None,
        thread_ts: str | None = None,
        action_ts: str | None = None,
        enabled: bool = True,
    ) -> str:
        """Store a button press as the endpoint does; returns the ingest outcome."""
        self.serial += 1
        row = self.latest()
        press = Press(
            team_id=TEAM,
            user_id=user,
            action_id=f"weaves.{action}",
            value=row.run_ref if ref is None else ref,
            channel_id=row.channel_id,
            message_ts=message_ts or row.status_ts or row.answer_ts or "1.1",
            thread_ts=thread_ts or row.thread_ts,
            action_ts=action_ts or f"1700100{self.serial:03d}.000100",
        )
        ingest = SlackInteractionIngest(
            store=self.runtime.slack_store, enabled=enabled, org_present=lambda _: True
        )
        return ingest.handle(press)

    def interact(self) -> int:
        """Run the interactions tick."""
        return int(self.runtime.slack_interactions.run())

    def threads(self) -> list[Any]:
        """Every thread row of the organization."""
        return list(self.runtime.slack_threads.list())

    def inbox(self, state: str | None = None) -> list[InboxEvent]:
        """Inbox rows, oldest first."""
        return self.runtime.slack_store.list_events(state=state)

    def open_audience(self) -> None:
        """Let everyone ask Jira and Slack search, with healthy snapshots."""
        w = self.world
        assert w.jira is not None and w.slack is not None
        w.set_slack_settings(
            connector_audience={
                w.jira.plugin_installation_id: "everyone",
                w.slack.plugin_installation_id: "everyone",
            }
        )
        w.snapshot(w.jira, "healthy")
        w.snapshot(w.slack, "healthy")

    def plan(self, *needs: str) -> None:
        """Script what the fake planner answers."""
        ok = not needs
        self.world.models.reply = lambda _: json.dumps(
            {"needs": list(needs), "general_ok": ok}
        )


def build_ask_world() -> AskWorld:
    """A world with a stored bot token and the Slack ticks pointed at the fake."""
    world = base.build_world()
    runtime = world.runtime
    slack = FakeSlackWeb()
    ref = runtime.secret_store.put(BOT_TOKEN, purpose="slack_bot_token", owner_ref=TEAM)
    held = runtime.slack_store.find_by_team(TEAM)
    assert held is not None
    runtime.slack_store.claim(
        replace(
            held,
            bot_secret_ref=ref,
            bot_user_id=BOT_USER,
            scopes=("app_mentions:read", "chat:write"),
        )
    )
    attach_slack_dispatch(runtime, slack.client())
    return AskWorld(world=world, slack=slack)
