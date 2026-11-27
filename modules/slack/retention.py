"""Inbox retention and the unfinished-token sweep, run by one worker tick.

The inbox is a queue and a dedupe table, not an archive (design 3.2): a row is
deleted ``PROCESSED_DAYS`` after it was received once it is done or dropped, and
a row nobody processed (a dead dispatcher) goes after ``PENDING_DAYS`` so a
mention's text never lingers. Text and row go together: the design keeps ids and
the capped mention text for the same period, and nothing earlier needs the text
once the row is processed because the dispatcher copies what it needs into the
task. Deletes run in bounded batches (``BATCH`` rows, at most ``MAX_BATCHES`` per
tick), so a backlog drains over several ticks without long transactions.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from application.ports.slack import SlackStore
from modules.slack.lifecycle import SlackLifecycle

logger = logging.getLogger("product.slack")
PROCESSED_DAYS = 7
PENDING_DAYS = 14  # ASSUMPTION: a mention older than two weeks is never answered
BATCH = 500
MAX_BATCHES = 10


def purge_inbox(lifecycle: SlackLifecycle) -> int:
    """Delete expired inbox rows in bounded batches; returns the rows removed."""
    store: SlackStore = lifecycle.store
    now = lifecycle.clock()
    cutoffs = (
        (now - timedelta(days=PROCESSED_DAYS), False),
        (now - timedelta(days=PENDING_DAYS), True),
    )
    removed = 0
    for cutoff, include_pending in cutoffs:
        for _ in range(MAX_BATCHES):
            count = store.purge_events(
                cutoff, limit=BATCH, include_pending=include_pending
            )
            removed += count
            if count < BATCH:
                break
    return removed


def run_tick(lifecycle: SlackLifecycle) -> int:
    """One worker tick: finish crashed token retirements, then expire the inbox."""
    swept = lifecycle.sweep()
    removed = purge_inbox(lifecycle)
    if swept or removed:
        logger.info("slack lifecycle swept=%d inbox_removed=%d", swept, removed)
    return swept + removed
