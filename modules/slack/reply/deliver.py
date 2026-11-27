"""Delivery of one request's messages: the working status, the answer, the receipt.

Rules (design 3.7): at most two bot posts per request (the working message and
the answer), the working message is *edited* (never re-posted) as the stage
changes and becomes the receipt, the answer is a new message so the asker is
notified, and one private (ephemeral) message at most. Every call that reaches
Slack stores its result on the thread row immediately, and the row is checked
before any post, so a retry after a failure never posts a second answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from application.ports.slack import SlackInstallation
from contracts.v1.slack_threads import SlackThread
from modules.slack import copy
from modules.slack.dispatch.threads import ThreadBook
from modules.slack.reply.compose import Reply
from modules.slack.reply.poster import PostError, SlackPoster
from modules.slack.reply.status_text import text_for
from shared.metrics import SLACK_REPLY, SLACK_REPLY_SECONDS

logger = logging.getLogger("product.slack")
NATIVE = "native"  # ``status_ts`` marker: the status lives in Slack's own UI
PRIVATE = "private"  # ``answer_ts`` marker: the answer was an ephemeral message


@dataclass(frozen=True)
class Target:
    """The installation and the clock one delivery step works with."""

    installation: SlackInstallation
    now: datetime
    native_status: bool = False


def _plain(text: str) -> str:
    """Slack's native status takes plain words (no emoji shortcode)."""
    return text.split(" ", 1)[1] if text.startswith(":") and " " in text else text


class Delivery:
    """Post, edit and settle the messages of requests (stateless between calls)."""

    def __init__(self, poster: SlackPoster, book: ThreadBook) -> None:
        """Bind the poster and the thread rows."""
        self._poster = poster
        self._book = book

    # -- working state -------------------------------------------------------

    def show_status(
        self, row: SlackThread, key: str, target: Target, text: str | None = None
    ) -> SlackThread:
        """Show ``key`` as the working status: native if on, else one edited message.

        ``text`` overrides the key's default words (a request kind that names more
        than a connector, such as the pull request a review is reading).
        """
        text = text if text is not None else text_for(key)
        if row.status_ts == NATIVE or (target.native_status and row.status_ts is None):
            ok = self._poster.native_status(
                target.installation, row.channel_id, row.thread_ts, _plain(text)
            )
            if ok:
                return self._book.save(
                    row.model_copy(
                        update={"status_ts": NATIVE, "status_text_key": key}
                    ),
                    target.now,
                )
            if row.status_ts == NATIVE:  # supported before, not now: keep going
                return self._book.save(
                    row.model_copy(update={"status_text_key": key}), target.now
                )
        if row.status_ts and row.status_ts != NATIVE:
            self._poster.edit(target.installation, row.channel_id, row.status_ts, text)
            return self._book.save(
                row.model_copy(update={"status_text_key": key}), target.now
            )
        if row.posts >= 2:
            return row
        ts = self._poster.post(
            target.installation, row.channel_id, text, thread_ts=row.thread_ts
        )
        SLACK_REPLY.inc(kind="status", outcome="posted")
        return self._book.save(
            row.model_copy(
                update={"status_ts": ts, "status_text_key": key, "posts": row.posts + 1}
            ),
            target.now,
        )

    # -- the reply -------------------------------------------------------------

    def send_reply(self, row: SlackThread, reply: Reply, target: Target) -> SlackThread:
        """Deliver the answer, refusal or failure exactly once and record it."""
        if row.answer_ts is not None:  # already delivered: never post twice
            return row
        if reply.ephemeral:
            return self._private(row, reply, target)
        if reply.kind == "failure" and row.status_ts and row.status_ts != NATIVE:
            self._poster.edit(
                target.installation, row.channel_id, row.status_ts, reply.text
            )
            SLACK_REPLY.inc(kind="failure", outcome="edited")
            return self._delivered(row, row.status_ts, reply, target, posts=0)
        if row.posts >= 2:
            raise PostError("permanent", "post_budget")
        ts = self._poster.post(
            target.installation, row.channel_id, reply.text, thread_ts=row.thread_ts
        )
        SLACK_REPLY.inc(kind=reply.kind, outcome="posted")
        return self._delivered(row, ts, reply, target, posts=1)

    def _private(self, row: SlackThread, reply: Reply, target: Target) -> SlackThread:
        if row.private_sent:  # a retry after the whisper already went out
            return self._delivered(row, PRIVATE, reply, target, posts=0)
        self._poster.ephemeral(
            target.installation,
            row.channel_id,
            row.requester_slack_user_id,
            reply.text,
            thread_ts=row.thread_ts,
        )
        SLACK_REPLY.inc(kind=reply.kind, outcome="private")
        row = self._book.save(row.model_copy(update={"private_sent": True}), target.now)
        self._withdraw_status(row, target)
        return self._delivered(row, PRIVATE, reply, target, posts=0)

    def _withdraw_status(self, row: SlackThread, target: Target) -> None:
        """A private reply leaves no public working message behind (best effort)."""
        try:
            if row.status_ts == NATIVE:
                self._poster.native_status(
                    target.installation, row.channel_id, row.thread_ts, ""
                )
            elif row.status_ts:
                try:
                    self._poster.delete(
                        target.installation, row.channel_id, row.status_ts
                    )
                except PostError:
                    self._poster.edit(
                        target.installation,
                        row.channel_id,
                        row.status_ts,
                        copy.PRIVATE_RECEIPT,
                    )
        except PostError as error:
            logger.warning("slack_status_withdraw_failed code=%s", error.code)

    def _delivered(
        self, row: SlackThread, ts: str, reply: Reply, target: Target, *, posts: int
    ) -> SlackThread:
        done = "failed" if reply.kind == "failure" else "done"
        saved = self._book.save(
            row.model_copy(
                update={
                    "answer_ts": ts,
                    "posts": row.posts + posts,
                    "run_state": done,
                    "finished_at": target.now,
                    "next_attempt_at": None,
                    "reply_attempts": 0,
                    "reply_error": "",
                }
            ),
            target.now,
        )
        if reply.kind == "answer":
            elapsed = (target.now - row.requested_at).total_seconds()
            SLACK_REPLY_SECONDS.observe(max(0.0, elapsed))
        return saved

    def notice(self, row: SlackThread, text: str, target: Target) -> None:
        """One private line to the asker (never counted as a post of the request)."""
        self._poster.ephemeral(
            target.installation,
            row.channel_id,
            row.requester_slack_user_id,
            text,
            thread_ts=row.thread_ts,
        )

    # -- the receipt ---------------------------------------------------------

    def finish_status(
        self, row: SlackThread, target: Target, receipt: str | None = None
    ) -> SlackThread:
        """Turn the working message into the receipt, then close the request.

        ``receipt`` replaces the default "Done in 41s" text (a review names what
        it reviewed).
        """
        answered_in_thread = (
            row.run_state == "done"
            and row.answer_ts not in (None, PRIVATE)
            and row.answer_ts != row.status_ts
        )
        if answered_in_thread and row.status_ts and row.status_ts != NATIVE:
            end = row.finished_at or target.now
            text = receipt or copy.RECEIPT.format(
                elapsed=copy.duration((end - row.requested_at).total_seconds())
            )
            self._poster.edit(target.installation, row.channel_id, row.status_ts, text)
        return self._book.save(
            row.model_copy(update={"reply_state": "posted", "next_attempt_at": None}),
            target.now,
        )
