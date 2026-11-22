"""Migration 24: indexes for the public waitlist's two global collections.

``waitlist_entries`` (one row per email) and ``waitlist_submissions`` (one row
per join) live in ``product_records`` with no organization. Their invisibility
to every tenant and every system purpose except ``waitlist`` is enforced by the
repeatable row-policy functions (``row_security``), which this migration also
re-applies so a database that never restarts between releases still has them.
Every statement is idempotent.
"""

from __future__ import annotations

from infrastructure.database.row_security import POLICY_FUNCTIONS

WAITLIST_MIGRATION: tuple[int, tuple[str, ...]] = (
    24,
    (
        *POLICY_FUNCTIONS,
        # One entry per email, compared without case: the race-safe backstop.
        """CREATE UNIQUE INDEX IF NOT EXISTS waitlist_entry_email_uq
               ON product_records ((lower(payload->>'email')))
               WHERE collection = 'waitlist_entries'""",
        # A resume token belongs to exactly one submission.
        """CREATE UNIQUE INDEX IF NOT EXISTS waitlist_submission_token_uq
               ON product_records ((payload->>'token_hash'))
               WHERE collection = 'waitlist_submissions'""",
        # Submissions of an entry (known-email joins, delete, retention).
        """CREATE INDEX IF NOT EXISTS waitlist_submission_entry_idx
               ON product_records ((payload->>'entry_id'))
               WHERE collection = 'waitlist_submissions'""",
        # Sign-ups per day and ordered listing.
        """CREATE INDEX IF NOT EXISTS waitlist_submission_created_idx
               ON product_records ((payload->>'created_at'))
               WHERE collection = 'waitlist_submissions'""",
    ),
)
