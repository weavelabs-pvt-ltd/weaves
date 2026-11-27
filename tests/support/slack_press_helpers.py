"""Shared helpers for the Stop and Try again tests (S4b)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.support.slack_ask_world import AskWorld
from tests.support.slack_pipeline_world import ORG

OTHER = "U0OTHER01"
ADMIN = "U0ADMIN01"


def public(w: AskWorld) -> list[dict]:
    return [m for m in w.slack.messages if not m.get("deleted")]


def audit(w: AskWorld) -> list:
    return [
        e
        for e in w.runtime.audit_events.list_scoped(ORG)
        if e.action == "slack.interaction"
    ]


def whispers(w: AskWorld) -> list[str]:
    return [e["text"] for e in w.slack.ephemerals]


def running(w: AskWorld) -> None:
    """A request the worker has claimed: the working message with its Stop button."""
    w.mention("what is WEAVES-1?")
    w.dispatch()
    w.at(w.latest().requested_at + timedelta(seconds=3))
    w.force_running()
    w.reply()


def failed(w: AskWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """A request that failed: the failure message with its Try again button."""

    def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider exploded with a secret detail")

    monkeypatch.setattr(w.runtime.agent_execution_service, "run_agent", broken)
    w.ask("what does idempotent mean?")


def admin_standing(w: AskWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """Map ADMIN to a member holding slack.manage and OTHER to one without it."""
    standing = {ADMIN: ("p-admin", True), OTHER: ("p-member", False)}
    monkeypatch.setattr(
        w.runtime.slack_access_service,
        "presser_standing",
        lambda user: standing.get(user, (None, False)),
    )
