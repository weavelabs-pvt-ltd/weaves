"""Cheap exits (scene H, E and F): no task, no model, no Ask quota.

Empty mention: help. Greeting: a greeting. ``status``: a private state line. The
Execute and stop/retry commands are recognized and answered politely but never
run in this slice; a command whose Execute rule is not satisfied gets the scene E
or system text instead of "coming soon", so a user is never told a feature exists
for them when an admin has not enabled it.

Each reply is one Slack post. The inbox row is settled *before* posting, so a
replay cannot post a second time (a crash or a transient Slack error here loses
the reply, which the user can ask for again by mentioning the bot).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime

from application.ports.slack import InboxEvent, SlackInstallation
from modules.slack import copy
from modules.slack.access import messages
from modules.slack.access.engine import rule_failure
from modules.slack.access.models import Action, Outcome, Reason
from modules.slack.classify import Intent, IntentKind
from modules.slack.dispatch.gates import DispatchGates
from modules.slack.dispatch.threads import ThreadBook
from modules.slack.reply.poster import LinkButton, PostError, SlackPoster
from modules.slack.reply.status_text import running_label

logger = logging.getLogger("product.slack")
_EXECUTE_ACTIONS = {
    IntentKind.REVIEW: ("review", Action.PR_REVIEW, "pr_review"),
    IntentKind.RUN: ("run", Action.RUN_WORKFLOW, "run_workflow"),
}
_SCENE_E = frozenset({Reason.RULE_OFF})


class CheapExits:
    """Decide and post the reply of a mention that never becomes a task."""

    def __init__(
        self,
        *,
        poster: SlackPoster,
        book: ThreadBook,
        gates: DispatchGates,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Bind the poster, thread rows and the dispatcher's gates."""
        self._poster = poster
        self._book = book
        self._gates = gates
        self._sleep = sleep

    def plan(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        intent: Intent,
        now: datetime,
    ) -> tuple[str, bool]:
        """The reply text and whether it is private (ephemeral)."""
        kind = intent.kind
        if kind is IntentKind.HELP:
            return copy.HELP, False
        if kind is IntentKind.GREETING:
            return (copy.THANKS if intent.thanks else copy.GREETING), False
        if kind is IntentKind.STATUS:
            return self._status(installation, event, now), True
        if kind in (IntentKind.STOP, IntentKind.RETRY):
            return copy.COMING_SOON[kind.value], True
        return self._execute(installation, event, intent, now)

    def _execute(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        intent: Intent,
        now: datetime,
    ) -> tuple[str, bool]:
        word, action, rule_name = _EXECUTE_ACTIONS[intent.kind]
        if intent.kind is IntentKind.REVIEW and not intent.arg:
            return copy.MISSING_PR, False  # scene F: needs a re-mention
        if intent.kind is IntentKind.RUN and not intent.arg:
            return copy.MISSING_WORKFLOW, False
        user, channel = event.user_id or "", event.channel_id or ""
        if intent.kind is IntentKind.RUN:
            # A workflow target is resolved in its own slice; only the rule here.
            user_listed, channel_listed = self._gates.standing(user, channel)
            failure = rule_failure(
                self._gates.rule_for(rule_name),
                user_listed=user_listed,
                channel_listed=channel_listed,
                channel_id=channel,
            )
            return self._blocked_or_soon(word, failure, None), True
        repo = intent.arg.split("#", 1)[0] if intent.kind is IntentKind.REVIEW else None
        decision = self._gates.evaluate(
            installation,
            slack_user_id=user,
            channel_id=channel,
            action=action,
            now=now,
            repo=repo,
        )
        if decision.allowed:
            return copy.COMING_SOON[word], True
        return self._blocked_or_soon(word, decision.reason, decision.message), True

    @staticmethod
    def _blocked_or_soon(word: str, reason: Reason | None, message: str | None) -> str:
        if reason is None:
            return copy.COMING_SOON[word]
        if reason in _SCENE_E:
            return copy.refusal(copy.NOT_SET_UP[word], Outcome.BLOCKED_RULE.value)
        text = message or messages.fixed(reason) or messages.UNAVAILABLE
        return copy.refusal(text, Outcome.BLOCKED_RULE.value)

    def _status(
        self, installation: SlackInstallation, event: InboxEvent, now: datetime
    ) -> str:
        thread_ts = event.thread_ts or event.message_ts or ""
        row = self._book.find(
            installation.org_id,
            installation.team_id,
            event.channel_id or "",
            thread_ts,
        )
        if row is None:
            return copy.NO_ACTIVITY
        if row.reply_state == "pending":
            seconds = (now - row.requested_at).total_seconds()
            return copy.status_report(running_label(row), seconds)
        if row.finished_at is not None:
            ago = copy.duration((now - row.finished_at).total_seconds())
            return f"{copy.NO_ACTIVITY} Your last question finished {ago} ago."
        return copy.NO_ACTIVITY

    def send(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        text: str,
        *,
        private: bool,
        button: LinkButton | None = None,
    ) -> str:
        """Post the reply once (one quick retry); returns an outcome label."""
        channel = event.channel_id or ""
        thread_ts = event.thread_ts or event.message_ts or ""
        for attempt in (1, 2):
            try:
                if private:
                    self._poster.ephemeral(
                        installation,
                        channel,
                        event.user_id or "",
                        text,
                        thread_ts=thread_ts,
                    )
                else:
                    self._poster.post(
                        installation,
                        channel,
                        text,
                        thread_ts=thread_ts,
                        rich=button is not None,
                        button=button,
                    )
                return "posted"
            except PostError as error:
                if error.kind == "token":
                    return "token"
                if error.kind in ("scrubber", "permanent") or attempt == 2:
                    return error.kind
                self._sleep(min(2.0, max(0.2, error.retry_after)))
        return "retry"
