"""The worker's pre-run guard port (Slack turns, design 3.5.2).

A guard re-evaluates a claimed turn's policy at pickup, because settings,
tokens, health and membership can change between the dispatcher and the worker.
It returns ``None`` for turns it does not govern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from contracts.v1 import Task, TaskTurn, TurnOrigin

CANCEL_REASON_REVOKED = "slack_access_revoked"


@dataclass(frozen=True)
class GuardDecision:
    """What to do with a claimed turn.

    ``origin`` is the turn's origin with the verdict and the approved capability
    ids filled in (the reply step reads it). ``proceed`` runs the answering
    model. Otherwise the turn is cancelled with ``cancel_reason`` (the policy no
    longer passes) or, when ``cancel_reason`` is ``None``, finishes with the fixed
    ``message`` and no model call (a red tool or a failed plan).
    """

    proceed: bool
    origin: TurnOrigin
    cancel_reason: str | None = None
    message: str | None = None


class TurnGuard(Protocol):
    """Decide, at pickup, whether a turn may run and with which capabilities."""

    def check(
        self, turn: TaskTurn, task: Task, question: str, run_key: str
    ) -> GuardDecision | None:
        """Return the decision, or ``None`` when the turn is not guarded."""
        ...
