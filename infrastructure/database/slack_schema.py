"""Migration 32: the Slack bot's global installation table and event inbox.

Two real tables, an exception to the one-JSONB-table rule like the usage ledger
(``docs/DATA_MODEL.md``): the inbox needs a true unique key for Slack-retry
dedupe, and both are read before any organization is known (an event names only
a ``team_id``).

* ``slack_installations``: one row per (organization, workspace). The partial
  unique index on ``team_id`` where the row is *held* (active or suspended) is
  the rule that a Slack workspace belongs to exactly one Weaves organization.
  The bot token is not here: ``bot_secret_ref`` points into the encrypted
  secret store. Policy: a tenant sees only its own rows; the named system
  purpose ``slack`` (event ingest, the dispatcher, purge) sees all of them.
* ``slack_event_inbox``: ``event_id`` is the primary key (Slack retries insert
  nothing), and a second unique key on (team, channel, message ts) for
  ``app_mention`` stops one message becoming two rows under different event
  ids. Only the purpose ``slack`` can see it; no tenant can.

Every statement is idempotent. Message text is kept only for ``app_mention``.
"""

from __future__ import annotations

from infrastructure.database.row_security import APP_ROLE_GRANTS

_SLACK_PURPOSE = "nullif(current_setting('app.system_purpose', true), '') = 'slack'"
INSTALLATION_POLICY = (
    f"org_id = nullif(current_setting('app.org_id', true), '') OR {_SLACK_PURPOSE}"
)
INBOX_POLICY = _SLACK_PURPOSE

SLACK_MIGRATION: tuple[int, tuple[str, ...]] = (
    32,
    (
        """CREATE TABLE IF NOT EXISTS slack_installations (
               installation_id text PRIMARY KEY,
               team_id text NOT NULL,
               org_id text NOT NULL,
               workspace_id text NOT NULL,
               team_name text,
               enterprise_id text,
               app_id text,
               bot_user_id text NOT NULL,
               bot_secret_ref text,
               scopes text[] NOT NULL DEFAULT '{}',
               installed_by_principal_id text NOT NULL,
               installed_by_name text,
               installed_by_slack_user text,
               status text NOT NULL CHECK (status IN
                   ('active', 'revoked', 'uninstalled', 'suspended')),
               installed_at timestamptz NOT NULL,
               updated_at timestamptz NOT NULL,
               revoked_at timestamptz
           )""",
        # One organization holds a workspace; this is the cross-org backstop.
        """CREATE UNIQUE INDEX IF NOT EXISTS slack_installations_team_held_uq
               ON slack_installations (team_id)
               WHERE status IN ('active', 'suspended')""",
        # One row per organization and workspace; a reinstall updates it.
        """CREATE UNIQUE INDEX IF NOT EXISTS slack_installations_org_team_uq
               ON slack_installations (org_id, team_id)""",
        """CREATE INDEX IF NOT EXISTS slack_installations_org_idx
               ON slack_installations (org_id, updated_at)""",
        """CREATE TABLE IF NOT EXISTS slack_event_inbox (
               event_id text PRIMARY KEY,
               team_id text NOT NULL,
               org_id text,
               installation_id text,
               event_type text NOT NULL,
               state text NOT NULL CHECK (state IN
                   ('pending', 'processing', 'done', 'dropped')),
               drop_reason text,
               attempts integer NOT NULL DEFAULT 1,
               request_id text,
               channel_id text,
               user_id text,
               message_ts text,
               thread_ts text,
               text text,
               reaction text,
               received_at timestamptz NOT NULL
           )""",
        """CREATE UNIQUE INDEX IF NOT EXISTS slack_event_inbox_message_uq
               ON slack_event_inbox (team_id, channel_id, message_ts)
               WHERE event_type = 'app_mention'""",
        # The dispatcher's claim (oldest pending first).
        """CREATE INDEX IF NOT EXISTS slack_event_inbox_pending_idx
               ON slack_event_inbox (received_at) WHERE state = 'pending'""",
        # Retention (rows are deleted a few days after they are processed).
        """CREATE INDEX IF NOT EXISTS slack_event_inbox_received_idx
               ON slack_event_inbox (received_at)""",
        "ALTER TABLE slack_installations ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE slack_event_inbox ENABLE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS tenant_isolation ON slack_installations",
        "DROP POLICY IF EXISTS tenant_isolation ON slack_event_inbox",
        f"""CREATE POLICY tenant_isolation ON slack_installations
               USING ({INSTALLATION_POLICY}) WITH CHECK ({INSTALLATION_POLICY})""",
        f"""CREATE POLICY tenant_isolation ON slack_event_inbox
               USING ({INBOX_POLICY}) WITH CHECK ({INBOX_POLICY})""",
        APP_ROLE_GRANTS,
    ),
)
