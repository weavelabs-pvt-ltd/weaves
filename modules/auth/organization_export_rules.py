"""What an organization export contains, and what it never contains.

Included: the organization, settings, lifecycle history, workspaces, users
(display name, email, status), roles and bindings, membership status only (never
account ids), agents and versions, workflows with versions, triggers (without
secret digests) and schedules, tasks with messages and turns, runs, artifact
metadata, model providers and profiles (``auth_ref`` is
removed; ``credential_last4`` stays), plugin installations without credentials,
skills and audit events.

Withheld, and counted in the export so the omission is visible: other members'
private tasks (with their messages, turns and runs) and other members' personal
skills (with their versions and publications). The exporting owner receives
their own personal content, because it is theirs; they are not given access to
other people's private work through administration.

Never exported: secrets and ciphertext, API credentials and token digests,
session, cookie, CSRF and OAuth state records, other tenants' data, and the
global account directory.
"""

from __future__ import annotations

from typing import Any

# Collections exported, in output order. Each is tenant-scoped by ``org_id``.
EXPORT_COLLECTIONS: tuple[str, ...] = (
    "workspaces",
    "users",
    "roles",
    "role_bindings",
    "memberships",
    "agents",
    "agent_versions",
    "workflows",
    "workflow_versions",
    "workflow_triggers",
    "workflow_schedules",
    "tasks",
    "task_messages",
    "task_turns",
    "conversation_threads",
    "workflow_runs",
    "agent_runs",
    "tool_invocations",
    "artifacts",
    "model_providers",
    "model_profiles",
    "model_configurations",
    "plugin_installations",
    "skill_definitions",
    "skill_versions",
    "skill_publications",
    "skill_revocations",
    "audit_events",
)

_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "users": ("id", "display_name", "email", "status", "created_at", "updated_at"),
    "memberships": (
        "id",
        "user_id",
        "principal_id",
        "status",
        "created_at",
        "updated_at",
    ),
}
_DROP_KEYS = frozenset(
    {
        "auth_ref",
        "secret_ref",
        "token",
        "api_key",
        "password",
        "secret",
        "ciphertext",
        "csrf_token",
    }
)
# Content hashes of skill versions are integrity metadata, not credentials.
_KEEP_DIGESTS = frozenset({"content_digest"})


def scrub(value: Any) -> Any:
    """Recursively drop credential references and every digest of a secret."""
    if isinstance(value, dict):
        return {
            key: scrub(item)
            for key, item in value.items()
            if key not in _DROP_KEYS
            and (not key.endswith("_digest") or key in _KEEP_DIGESTS)
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def shape(collection: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the collection's allow-list, then scrub the remainder."""
    allowed = _ALLOWLISTS.get(collection)
    if allowed is not None:
        payload = {key: payload[key] for key in allowed if key in payload}
    return dict(scrub(payload))


class Withheld:
    """Identifiers of other members' private content, collected before export."""

    def __init__(self, actor: str) -> None:
        """Remember the exporting principal, whose own content is included."""
        self.actor = actor
        self.tasks: set[str] = set()
        self.threads: set[str] = set()
        self.agent_runs: set[str] = set()
        self.runs: set[str] = set()
        self.skills: set[str] = set()
        self.skill_versions: set[str] = set()
        self.counts = {"private_tasks": 0, "personal_skills": 0}

    def note_task(self, row: dict[str, Any]) -> None:
        """Withhold a private task owned by someone else."""
        if row.get("audience") == "private" and (
            row.get("audience_owner_principal_id") != self.actor
        ):
            self.tasks.add(str(row["task_id"]))
            self.threads.add(str(row.get("thread_id")))
            self.counts["private_tasks"] += 1

    def note_skill(self, row: dict[str, Any]) -> None:
        """Withhold another member's personal skill."""
        if row.get("scope") == "personal" and (
            row.get("owner_principal_id") != self.actor
        ):
            self.skills.add(str(row["skill_id"]))
            self.counts["personal_skills"] += 1

    def keep(self, collection: str, row: dict[str, Any]) -> bool:
        """Report whether a row may be exported to this actor."""
        checks = {
            "tasks": lambda: row.get("task_id") not in self.tasks,
            "task_messages": lambda: row.get("task_id") not in self.tasks,
            "task_turns": lambda: row.get("task_id") not in self.tasks,
            "conversation_threads": lambda: row.get("thread_id") not in self.threads,
            "agent_runs": lambda: row.get("agent_run_id") not in self.agent_runs,
            "workflow_runs": lambda: row.get("run_id") not in self.runs,
            "tool_invocations": lambda: row.get("run_id") not in self.runs,
            "artifacts": lambda: row.get("run_id") not in self.runs,
            "skill_definitions": lambda: row.get("skill_id") not in self.skills,
            "skill_versions": lambda: row.get("skill_id") not in self.skills,
            "skill_publications": lambda: (
                row.get("skill_version_id") not in self.skill_versions
            ),
            "skill_revocations": lambda: (
                row.get("skill_version_id") not in self.skill_versions
            ),
        }
        return checks.get(collection, lambda: True)()
