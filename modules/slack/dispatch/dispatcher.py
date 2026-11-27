"""The ``slack_dispatch`` worker tick: from an inbox row to a task or a cheap reply.

The tick reads pending ``app_mention`` rows oldest first. For each one it
resolves the workspace to its organization, binds that organization's tenant
context, classifies the text by the fixed grammar and then either

* queues a hidden Q&A turn for an Ask (nothing else happens here: the worker's
  guard runs the pipeline at pickup and the reply tick posts what comes out), or
* answers a cheap exit (help, greeting, status, a text command that is not live
  yet, a refusal) with no task, no model call and no Ask quota.

Claiming is a compare-and-set on the inbox row's state, so two dispatchers (or a
replay after a crash) never act on one event twice: refusals and cheap replies
settle the row *before* posting, an Ask settles it after the task exists (the
task key makes the replay a no-op).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime

from application.ports.slack import InboxEvent, SlackInstallation, SlackStore
from modules.slack import copy
from modules.slack.access import messages
from modules.slack.access.models import Outcome
from modules.slack.classify import Intent, IntentKind, classify
from modules.slack.dispatch.ask_flow import AskFlow, AskKind, AskResult, request_id_of
from modules.slack.dispatch.cheap import CheapExits
from modules.slack.dispatch.gates import DispatchGates
from modules.slack.dispatch.task_flow import TaskFlow, TaskKind, TaskResult
from modules.slack.events import tenant_of
from modules.slack.reply.loss import InstallationLoss
from modules.slack.reply.poster import LinkButton
from modules.slack.reply.throttle import NoticeThrottle
from shared.clock import utc_now
from shared.correlation import extend_context
from shared.metrics import SLACK_DISPATCH, SLACK_INBOX_AGE

logger = logging.getLogger("product.slack")
BATCH = 25
TICK_BUDGET_SECONDS = 15.0
# How long a mention waits for the previous answer in its thread before giving up.
BUSY_PATIENCE_SECONDS = 120.0
MAX_FAILURES = 5
MAX_TRACKED_FAILURES = 1000


class SlackDispatcher:
    """Handle pending ``app_mention`` inbox rows."""

    def __init__(
        self,
        *,
        store: SlackStore,
        gates: DispatchGates,
        ask: AskFlow,
        task: TaskFlow,
        cheap: CheapExits,
        loss: InstallationLoss,
        enabled: Callable[[], bool],
        clock: Callable[[], datetime] = utc_now,
        budget_seconds: float = TICK_BUDGET_SECONDS,
    ) -> None:
        """Bind the store, the gates, the two flows and the state readers."""
        self._store = store
        self._gates = gates
        self._ask = ask
        self._task = task
        self._cheap = cheap
        self._loss = loss
        self._enabled = enabled
        self._clock = clock
        self._budget = budget_seconds
        self._throttle = NoticeThrottle()
        self._failures: dict[str, int] = {}

    def run(self) -> int:
        """Handle a batch of pending mentions; returns how many were settled."""
        started = time.monotonic()
        handled = 0
        for event in self._store.list_events(
            state="pending", limit=BATCH, event_type="app_mention"
        ):
            if time.monotonic() - started > self._budget:
                break
            try:
                handled += self._handle(event)
                self._failures.pop(event.event_id, None)
            except Exception as error:
                handled += self._poisoned(event, error)
        return handled

    def _poisoned(self, event: InboxEvent, error: Exception) -> int:
        """Count a failed attempt; a mention that keeps failing is dropped.

        Without this one bad row (for example an installer who lost the admin
        role, so the agent cannot be created) would be retried every second and
        starve the newer rows behind it.
        """
        SLACK_DISPATCH.inc(intent="unknown", outcome="error")
        count = self._failures.get(event.event_id, 0) + 1
        if len(self._failures) < MAX_TRACKED_FAILURES:
            self._failures[event.event_id] = count
        logger.error(
            "slack_dispatch_failed event=%s attempt=%d kind=%s",
            event.event_id,
            count,
            type(error).__name__,
        )
        if count < MAX_FAILURES:
            return 0
        self._failures.pop(event.event_id, None)
        return self._drop(event, ("dispatch_error:" + type(error).__name__ + ":" + str(error))[:180], "unknown")  # TEMP-DIAG

    # -- one event ---------------------------------------------------------------

    def _handle(self, event: InboxEvent) -> int:
        now = self._clock()
        SLACK_INBOX_AGE.observe(max(0.0, (now - event.received_at).total_seconds()))
        installation = self._store.find_by_team(event.team_id)
        if installation is None or installation.status != "active":
            return self._drop(event, "installation_inactive", "unknown")
        if not self._enabled():
            return self._drop(event, "slack_disabled", "unknown")
        with tenant_of(installation), extend_context(request_id=request_id_of(event)):
            intent = classify(event.text, installation.bot_user_id)
            if intent.kind is IntentKind.ASK:
                return self._ask_event(installation, event, intent, now)
            if intent.kind is IntentKind.TASK:
                return self._task_event(installation, event, intent, now)
            return self._cheap_event(installation, event, intent, now)

    def _drop(self, event: InboxEvent, reason: str, intent: str) -> int:
        won = self._store.settle_event(event.event_id, "dropped", reason=reason)
        SLACK_DISPATCH.inc(intent=intent, outcome=f"dropped_{reason}"[:40])
        return 1 if won else 0

    def _done(self, event: InboxEvent, intent: Intent, outcome: str) -> bool:
        won = self._store.settle_event(event.event_id, "done")
        if won:
            SLACK_DISPATCH.inc(intent=intent.kind.value, outcome=outcome)
            logger.info(
                "slack_dispatch event=%s request=%s intent=%s outcome=%s",
                event.event_id,
                request_id_of(event),
                intent.kind.value,
                outcome,
            )
        return won

    # -- Ask --------------------------------------------------------------------

    def _ask_event(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        intent: Intent,
        now: datetime,
    ) -> int:
        result = self._ask.run(installation, event, intent.text, now)
        if result.kind is AskKind.BUSY and self._should_wait(event, now):
            return 0  # the earlier answer is still going out; try again next tick
        if result.created_work:
            return 1 if self._done(event, intent, result.kind.value) else 0
        if result.kind is AskKind.DROPPED:
            return self._drop(event, result.reason or "dropped", intent.kind.value)
        if not self._done(event, intent, result.kind.value):
            return 0
        self._refuse(installation, event, result)
        return 1

    @staticmethod
    def _should_wait(event: InboxEvent, now: datetime) -> bool:
        return (now - event.received_at).total_seconds() < BUSY_PATIENCE_SECONDS

    def _refuse(
        self, installation: SlackInstallation, event: InboxEvent, result: AskResult
    ) -> None:
        text = {
            AskKind.NO_MODEL: copy.NO_MODEL,
            AskKind.BUSY: copy.BUSY,
        }.get(result.kind, result.message or messages.UNAVAILABLE)
        neutral = result.reason in ("platform_paused", "org_unavailable", "bot_paused")
        if neutral and not self._throttle.allow(
            installation.team_id, event.channel_id or "", self._clock()
        ):
            return  # one neutral line per channel per hour
        outcome = Outcome.RATE_LIMITED.value if result.kind is AskKind.LIMITED else ""
        self._post(installation, event, copy.refusal(text, outcome), private=True)

    # -- task: ----------------------------------------------------------------------

    def _task_event(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        intent: Intent,
        now: datetime,
    ) -> int:
        result = self._task.run(installation, event, intent.arg, now)
        if result.created_work:
            return 1 if self._done(event, intent, result.kind.value) else 0
        if result.kind is TaskKind.DROPPED:
            return self._drop(event, result.reason or "dropped", intent.kind.value)
        if not self._done(event, intent, result.kind.value):
            return 0
        neutral = result.reason in ("platform_paused", "org_unavailable", "bot_paused")
        if neutral and not self._throttle.allow(
            installation.team_id, event.channel_id or "", self._clock()
        ):
            return 1  # one neutral line per channel per hour
        self._post_task(installation, event, result)
        return 1

    def _post_task(
        self, installation: SlackInstallation, event: InboxEvent, result: TaskResult
    ) -> None:
        button = (
            LinkButton(result.button_label, result.button_url)
            if result.button_label and result.button_url
            else None
        )
        outcome = self._cheap.send(
            installation,
            event,
            result.message or messages.UNAVAILABLE,
            private=not result.public,
            button=button,
        )
        if outcome == "token":
            self._loss(installation)
        if outcome != "posted":
            SLACK_DISPATCH.inc(intent="reply", outcome=f"post_{outcome}"[:40])

    # -- cheap exits --------------------------------------------------------------

    def _cheap_event(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        intent: Intent,
        now: datetime,
    ) -> int:
        blocked = self._gates.state(
            installation,
            slack_user_id=event.user_id or "",
            channel_id=event.channel_id or "",
            now=now,
        )
        if blocked is not None:
            if blocked.outcome is Outcome.DROPPED:
                return self._drop(
                    event,
                    blocked.reason.value if blocked.reason else "dropped",
                    intent.kind.value,
                )
            if not self._done(event, intent, "blocked"):
                return 0
            if self._throttle.allow(installation.team_id, event.channel_id or "", now):
                self._post(
                    installation,
                    event,
                    blocked.message or messages.UNAVAILABLE,
                    private=True,
                )
            return 1
        text, private = self._cheap.plan(installation, event, intent, now)
        if not self._done(event, intent, "cheap"):
            return 0
        self._post(installation, event, text, private=private)
        return 1

    def _post(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        text: str,
        *,
        private: bool,
    ) -> None:
        outcome = self._cheap.send(installation, event, text, private=private)
        if outcome == "token":
            self._loss(installation)
        if outcome != "posted":
            SLACK_DISPATCH.inc(intent="reply", outcome=f"post_{outcome}"[:40])
