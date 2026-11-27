"""Which read capabilities a run may use, narrowed by the turn's origin.

A Slack turn (``origin.kind`` ``slack`` or ``slack_qa``) may run only the
capabilities the Slack pipeline approved for it (needed AND green), never the
agent version's whole list. The worker binds the origin around the agent run;
the capability loop asks ``allowed_capabilities`` for the list to run. Every
other run, with no origin bound, gets exactly the configured list. A Slack turn
whose approved list was never set runs nothing (fail closed).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

SLACK_KINDS = frozenset({"slack", "slack_qa"})
# Capabilities that exist only for an Execute request of the Slack bot: a run
# with no Slack origin (a web task, a workflow) or an Ask never runs them, even
# when an agent version lists them.
SLACK_EXECUTE_ONLY = frozenset({"github.pr.review_context"})


@dataclass(frozen=True)
class RunOrigin:
    """The origin of the run being executed on this thread of control."""

    kind: str
    capability_ids: tuple[str, ...] | None = None


_ORIGIN: ContextVar[RunOrigin | None] = ContextVar("weaves_run_origin", default=None)


@contextmanager
def bound_run_origin(origin: RunOrigin | None) -> Iterator[None]:
    """Bind ``origin`` for the duration of one agent run (``None`` binds nothing)."""
    if origin is None:
        yield
        return
    token = _ORIGIN.set(origin)
    try:
        yield
    finally:
        _ORIGIN.reset(token)


def allowed_capabilities(configured: Sequence[str]) -> tuple[str, ...]:
    """The capability ids the loop may run, in the agent version's order.

    Narrowing never adds a capability: the result is a subset of ``configured``.
    """
    origin = _ORIGIN.get()
    if origin is None or origin.kind != "slack":
        allowed = tuple(item for item in configured if item not in SLACK_EXECUTE_ONLY)
        if origin is None or origin.kind not in SLACK_KINDS:
            return allowed
        configured = allowed
    if origin.capability_ids is None:
        return ()
    approved = set(origin.capability_ids)
    return tuple(item for item in configured if item in approved)
