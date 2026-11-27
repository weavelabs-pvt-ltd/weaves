"""Helpers for the Slack ``task:`` tests: a mapped member, a default agent, a rule.

The in-memory runtime has one signed-in principal (``local-developer``, an owner);
the helpers map the asker's Slack id to it, make the member directory treat it as
an active member, and point the default task agent at an agent that lists one
read connector. Everything else is the real dispatcher, guard, worker and ticks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from contracts.v1.slack_access import SlackAllowedChannel, SlackAllowedUser
from tests.support.slack_ask_world import AskWorld
from tests.support.slack_pipeline_world import CHANNEL, JIRA, ORG, USER

MEMBER = "local-developer"
APP_URL = "https://app.example.test"
TASK_TEXT = "prepare the weekly KPI summary"


def allow_user(w: AskWorld, *, mapped: bool = True, user: str = USER) -> None:
    """Put ``user`` on the allowlist, mapped to the signed-in member or not."""
    now = datetime.now(UTC)
    w.runtime.slack_allowed_users.put(
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


def allow_channel(w: AskWorld, *, name: str = "ops", channel: str = CHANNEL) -> None:
    """Put ``channel`` on the allowlist with a cached display name."""
    now = datetime.now(UTC)
    w.runtime.slack_allowed_channels.put(
        SlackAllowedChannel(
            id=f"{ORG}:{channel}",
            org_id=ORG,
            channel_id=channel,
            name_cached=name,
            added_by=MEMBER,
            created_at=now,
            updated_at=now,
        )
    )


def make_agent(w: AskWorld, *, name: str = "Ops assistant", caps: Any = (JIRA,)) -> str:
    """Create the agent that will run Slack tasks; returns its id."""
    agent = w.runtime.agent_service.create_agent(
        name=name,
        instructions="Do the task the user describes.",
        allowed_capability_ids=tuple(caps),
    )
    return str(agent.id)


def enable_tasks(
    w: AskWorld,
    *,
    rule: str = "user_only",
    agent: bool = True,
    mapped: bool = True,
    listed: bool = True,
    channel_listed: bool = False,
    **settings: Any,
) -> str | None:
    """Turn ``task:`` on for the asker; returns the default agent id (if any)."""
    members = w.runtime.slack_access_service.allowlists.members
    members.is_active = lambda principal_id: True  # the member is signed in, active
    if listed:
        allow_user(w, mapped=mapped)
    if channel_listed:
        allow_channel(w)
    agent_id = make_agent(w) if agent else None
    w.world.set_slack_settings(
        rules={
            "create_task": rule,
            "pr_review": "off",
            "run_workflow": "off",
            "ask_private": "off",
        },
        default_task_agent_id=agent_id,
        **settings,
    )
    return agent_id


def tasks(w: AskWorld) -> list[Any]:
    """Every task of the organization (the stored copies)."""
    return list(w.runtime.tasks.list())


def task_rows(w: AskWorld) -> list[Any]:
    """The ``slack_threads`` rows of kind ``task``."""
    return [r for r in w.threads() if r.kind == "task"]


def run_task(w: AskWorld, text: str = TASK_TEXT, **kw: Any) -> Any:
    """Mention ``task: text``, dispatch, post the first message, work, reply."""
    w.mention(f"task: {text}", **kw)
    w.dispatch()
    w.reply()
    turn = w.work()
    w.reply()
    return turn
