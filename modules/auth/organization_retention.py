"""What organization deletion removes and what it deliberately keeps.

REMOVED, under the organization's own scope, bounded and in dependency order:
every tenant row carrying that ``org_id`` in every ``product_records``
collection (including collections this release no longer reads, via a final
sweep), the organization's encrypted secrets (through the secret store), API
credentials (revoked first, then deleted), OAuth setup states, private and
organization skills with their versions and snapshots, tasks, turns, jobs,
runs, artifacts, approvals, tenant-local users/principals/roles and this
organization's memberships. The global ``Account`` and its other memberships are
untouched; an account left with no membership simply remains.

RETAINED, because a tombstone must outlive the data it describes:

* The ``Organization`` row (id, ``DELETED`` status, ``updated_at``) with its
  name replaced by a placeholder. The id can therefore never be re-created or
  re-activated, and a second delete is a no-op. A previous-release reader
  tolerates the placeholder because ``name`` is an ordinary display string.
* The ``OrganizationLifecycle`` record with its full history (reason, actor
  kind and reference, timestamps) and the purge checkpoint: it is the
  accountability record for who deleted the organization and when.
* ``organization.lifecycle.*`` and ``organization.exported`` audit events,
  which are lifecycle accountability records and carry no tenant content. Every
  other audit event is removed.
* ``account_organization_requests`` rows. They are the per-account creation
  idempotency and quota ledger: a deleted organization still counts toward the
  account's creation cap, and a repeated creation key can never mint a second
  organization for the same request.
"""

from __future__ import annotations

# Collections the generic sweep must never touch. ``audit_events`` is handled by
# the explicit retention rule, never by the sweep.
KEEP_COLLECTIONS: frozenset[str] = frozenset(
    {
        "organizations",
        "organization_lifecycle",
        "account_organization_requests",
        "accounts",
        "account_identities",
        "account_oauth_states",
        "account_sessions",
        "audit_events",
    }
)

# (payload field, action prefixes) kept from the organization's audit trail.
RETAINED_AUDIT: tuple[str, tuple[str, ...]] = (
    "action",
    ("organization.lifecycle.", "organization.exported"),
)

DELETED_NAME_PLACEHOLDER = "deleted organization"

# Dependency order: work and results, then configuration, then identity.
PURGE_ORDER: tuple[str, ...] = (
    "execution_jobs",
    "task_turns",
    "task_messages",
    "tasks",
    "conversation_threads",
    "tool_invocations",
    "approval_requests",
    "artifacts",
    "agent_runs",
    "workflow_runs",
    "canvas_runs",
    "evaluation_executions",
    "evaluation_reports",
    "evaluation_suites",
    "workflow_schedules",
    "workflow_triggers",
    "workflow_versions",
    "workflows",
    "agent_versions",
    "agents",
    "skill_revocations",
    "skill_publications",
    "skill_versions",
    "skill_definitions",
    "plugin_installations",
    "plugins",
    "oauth_authorization_states",
    "model_profiles",
    "model_providers",
    "model_configurations",
    "approval_policies",
    "canvas_settings",
    "api_credentials",
    "role_bindings",
    "roles",
    "memberships",
    "principals",
    "users",
    "workspaces",
    "organization_settings",
    "user_sessions",
    "user_password_credentials",
    "email_login_challenges",
    "sso_connections",
    "sso_external_identities",
    "user_invitations",
)
