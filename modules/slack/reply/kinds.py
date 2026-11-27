"""How a request kind other than a plain Ask words its messages.

The reply tick owns when to post, edit and retry; a kind owns what to say. A
thread row's ``kind`` selects the implementation (an Ask has none and uses the
defaults of ``compose`` and ``status_text``). Implementations only read records
of the turn and produce text; they never talk to Slack.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from contracts.v1 import TaskTurn
from contracts.v1.slack_threads import SlackThread
from modules.slack.reply.compose import Reply


class ReplyKind(Protocol):
    """The wording of one request kind."""

    def working_key(
        self, row: SlackThread, turn: TaskTurn, age: timedelta, running: bool
    ) -> str | None:
        """The status key to show now, or ``None`` for no working message yet."""
        ...

    def working_text(self, row: SlackThread, turn: TaskTurn, key: str) -> str | None:
        """The text of a status key, or ``None`` to use the default text."""
        ...

    def reply(self, row: SlackThread, turn: TaskTurn, now: datetime) -> Reply | None:
        """The message for a finished turn (``None``: nothing to say)."""
        ...

    def receipt_text(self, row: SlackThread, turn: TaskTurn) -> str | None:
        """The receipt the working message becomes, or ``None`` for the default."""
        ...
