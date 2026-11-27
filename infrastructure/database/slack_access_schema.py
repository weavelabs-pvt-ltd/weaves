"""Migration 33: Slack access (allowlist indexes, later-slice indexes, role backfill).

The three organization-scoped collections of the Slack access policy
(``slack_access_settings``, ``slack_allowed_users``, ``slack_allowed_channels``)
live in ``product_records`` like every tenant collection, so they inherit its
row-level security by ``org_id`` and the organization purge sweep. This
migration adds:

* partial unique indexes: one allowlist row per Slack user and per channel in an
  organization, and (for the later Ask slices) one ``slack_threads`` row per Slack
  thread and kind and one ``slack_feedback`` row per turn and asker;
* an idempotent backfill adding the ``slack.manage`` permission to stored Admin
  roles (system role key ``admin``) and to legacy "Organization owner" roles.
  New and re-seeded organizations get it from the Admin preset, which grants every
  permission, and the standard-role repair at startup (``seed_standard_roles``)
  also rewrites stored Admin roles; this statement makes it true at migration
  time, before any application process starts.

Every statement is idempotent, so the migration is safe on an empty database and
when re-run by hand. Version 32 belongs to the Slack installation tables.
"""

from __future__ import annotations

# Collections whose rows appear later (the Ask slices register them). The
# collection-drift test accepts SQL that names them until they are registered.
RESERVED_COLLECTIONS: tuple[str, ...] = ("slack_threads", "slack_feedback")

SLACK_ACCESS_MIGRATION: tuple[int, tuple[str, ...]] = (
    33,
    (
        """CREATE UNIQUE INDEX IF NOT EXISTS slack_allowed_users_org_user_uq
               ON product_records (org_id, (payload->>'slack_user_id'))
               WHERE collection = 'slack_allowed_users'""",
        """CREATE UNIQUE INDEX IF NOT EXISTS slack_allowed_channels_org_channel_uq
               ON product_records (org_id, (payload->>'channel_id'))
               WHERE collection = 'slack_allowed_channels'""",
        """CREATE UNIQUE INDEX IF NOT EXISTS slack_threads_key_uq
               ON product_records (
                   org_id, (payload->>'team_id'), (payload->>'channel_id'),
                   (payload->>'thread_ts'), (payload->>'kind'))
               WHERE collection = 'slack_threads'""",
        """CREATE UNIQUE INDEX IF NOT EXISTS slack_feedback_turn_asker_uq
               ON product_records (
                   org_id, (payload->>'turn_id'), (payload->>'asker_ref'))
               WHERE collection = 'slack_feedback'""",
        """UPDATE product_records
               SET payload = jsonb_set(
                   payload, '{permissions}',
                   (payload->'permissions') || '"slack.manage"'::jsonb)
               WHERE collection = 'roles'
                 AND (payload->>'system_role_key' = 'admin'
                      OR payload->>'name' = 'Organization owner')
                 AND jsonb_typeof(payload->'permissions') = 'array'
                 AND NOT (payload->'permissions' @> '["slack.manage"]'::jsonb)""",
    ),
)
