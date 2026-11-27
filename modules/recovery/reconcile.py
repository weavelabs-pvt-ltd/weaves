"""Re-apply recorded purges and revocations to a freshly restored database.

Run after any restore and before the API or worker start. The tombstone log
lives outside the database, so it knows about every deletion and revocation made
after the restored backup was taken. Replay only ever removes or revokes, and
every step is idempotent, so a repeat run (or a run against a backup that
already contains the change) changes nothing further.

What is replayed:

* ``organization_purge_started`` / ``organization_deleted``: the organization is
  forced to DELETING (if the restored database still has it) and the existing
  purge service runs until it reaches DELETED.
* ``credential_revoked``: the API credential is marked revoked.
* ``slack_installation_ended``: a Slack workspace that was uninstalled, revoked
  or disconnected after the backup is ended again (token reference cleared,
  pending events dropped, workspace freed), but only for the same install
  generation, so a newer reinstall that the backup already holds is left alone.
  Its bot token comes back only if the backup holds it, and the matching
  ``secret_deleted`` tombstone deletes it again.
* ``secret_deleted``: the secret (connector credential or provider key) is
  deleted again.
* ``secret_replaced``: the secret is deleted, because the rotated value is not in
  the backup and the old one must not return. Only tombstones newer than the
  backup's recorded log head are replayed, to avoid deleting a rotation that the
  backup already contains.
* All account sessions are revoked (they live 12 h, and the browser session
  secret must be new).

Nothing else is touched. Dangling references (for example a connector record
whose secret was deleted) are left for the owner to disconnect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from contracts.v1 import ApiCredentialStatus, OrganizationLifecycleState
from modules.auth.lifecycle_actor import Actor
from modules.recovery.tombstones import (
    CREDENTIAL_REVOKED,
    ORGANIZATION_DELETED,
    ORGANIZATION_PURGE_STARTED,
    SECRET_DELETED,
    SECRET_REPLACED,
    SLACK_INSTALLATION_ENDED,
    FileTombstoneLog,
    Tombstone,
    TombstoneLogCorrupt,
)
from modules.slack.lifecycle import generation
from shared.clock import utc_now
from shared.request_context import (
    reset_request_principal,
    set_request_identity,
    system_scope,
)

MAX_PURGE_PASSES = 500
SYSTEM = Actor.system().audit_id
_ORG_KINDS = {ORGANIZATION_PURGE_STARTED, ORGANIZATION_DELETED}


class ReconcileRefused(RuntimeError):
    """The log cannot be trusted to cover the backup; nothing was changed."""


@dataclass
class Plan:
    """What a reconcile run will do, derived from the log alone."""

    organizations: list[str] = field(default_factory=list)
    credentials: list[tuple[str, str]] = field(default_factory=list)
    secrets: list[tuple[str, str]] = field(default_factory=list)
    # (org_id, how it ended, installation generation); purges are covered above.
    slack_installations: list[tuple[str, str, str]] = field(default_factory=list)
    skipped_replacements: list[str] = field(default_factory=list)


def verify_head(entries: list[Tombstone], head: dict[str, Any] | None) -> int | None:
    """Refuse when the log is shorter than, or diverges from, the backup's head."""
    if head is None:
        return None
    seq = int(head["seq"])
    if seq > len(entries):
        raise ReconcileRefused(
            f"tombstone log has {len(entries)} entries but the backup recorded {seq}"
        )
    if seq and entries[seq - 1].hash != head["hash"]:
        raise ReconcileRefused("tombstone log diverges from the backup's log head")
    return seq


def build_plan(entries: list[Tombstone], head_seq: int | None) -> Plan:
    """Select the tombstones to replay; deletions are replayed unconditionally."""
    plan = Plan()
    for entry in entries:
        if entry.kind in _ORG_KINDS and entry.org_id not in plan.organizations:
            plan.organizations.append(entry.org_id)
        elif entry.kind == CREDENTIAL_REVOKED:
            plan.credentials.append((entry.org_id, entry.subject_id))
        elif entry.kind == SECRET_DELETED:
            plan.secrets.append((entry.org_id, entry.subject_id))
        elif entry.kind == SLACK_INSTALLATION_ENDED:
            if entry.subject_kind in {"uninstalled", "revoked"}:
                plan.slack_installations.append(
                    (entry.org_id, entry.subject_kind, entry.subject_id)
                )
        elif entry.kind == SECRET_REPLACED:
            if head_seq is not None and entry.seq > head_seq:
                plan.secrets.append((entry.org_id, entry.subject_id))
            else:
                plan.skipped_replacements.append(entry.subject_id)
    return plan


def _purge_organization(runtime: Any, org_id: str) -> str:
    lifecycle = runtime.organization_lifecycle_service
    purge = runtime.organization_purge_service
    token = set_request_identity(SYSTEM, org_id, "lifecycle")
    try:
        runtime.organizations.get(org_id)
    except KeyError:
        return "absent"
    finally:
        reset_request_principal(token)
    lifecycle.begin_restore_purge(org_id)
    for _ in range(MAX_PURGE_PASSES):
        if purge.purge_pass(org_id):
            return "deleted"
        token = set_request_identity(SYSTEM, org_id, "lifecycle")
        try:
            if lifecycle.state_of(org_id) is OrganizationLifecycleState.DELETED:
                return "deleted"
        finally:
            reset_request_principal(token)
    raise ReconcileRefused(f"purge of {org_id} did not converge")


def _revoke_credential(runtime: Any, org_id: str, credential_id: str) -> str:
    token = set_request_identity(SYSTEM, org_id, "lifecycle")
    try:
        try:
            credential = runtime.api_credentials.get_scoped(credential_id, org_id)
        except KeyError:
            return "absent"
        if credential.status is ApiCredentialStatus.REVOKED:
            return "already_revoked"
        now = utc_now()
        runtime.api_credentials.put(
            credential.model_copy(
                update={
                    "status": ApiCredentialStatus.REVOKED,
                    "revoked_at": now,
                    "updated_at": now,
                }
            )
        )
        return "revoked"
    finally:
        reset_request_principal(token)


def _delete_secret(runtime: Any, org_id: str, secret_ref: str) -> str:
    token = set_request_identity(SYSTEM, org_id, "lifecycle")
    try:
        runtime.secret_store.delete(secret_ref)
        return "deleted"
    finally:
        reset_request_principal(token)


def _end_slack_installation(
    runtime: Any, org_id: str, how: str, generation_id: str
) -> str:
    """End a restored installation of the recorded generation (no Slack call)."""
    installation_id = generation_id.partition("@")[0]
    token = set_request_identity(SYSTEM, org_id, "lifecycle")
    try:
        row = runtime.slack_store.get_for_org(installation_id)
        if row is None:
            return "absent"
        if generation(row) != generation_id:
            return "superseded"
        ended = runtime.slack_lifecycle.end(
            row, how, revoke=False, actor="restore_reconcile"
        )
        return "ended" if ended is not None else "already_ended"
    finally:
        reset_request_principal(token)


def _revoke_sessions(runtime: Any) -> int:
    now = utc_now()
    count = 0
    with system_scope("account"):
        for session in runtime.account_sessions.list():
            if session.revoked_at is None:
                runtime.account_sessions.put(
                    session.model_copy(update={"revoked_at": now, "updated_at": now})
                )
                count += 1
    return count


def reconcile(
    runtime: Any,
    log: FileTombstoneLog,
    *,
    head: dict[str, Any] | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Replay the log onto ``runtime``'s database and return a report."""
    try:
        entries = log.read()
    except TombstoneLogCorrupt as exc:
        raise ReconcileRefused(f"tombstone log cannot be trusted: {exc}") from exc
    plan = build_plan(entries, verify_head(entries, head))
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "tombstones": len(entries),
        "head_checked": head is not None,
        "skipped_secret_replacements": plan.skipped_replacements,
        "organizations": {},
        "credentials": {},
        "secrets": {},
        "slack_installations": {},
        "sessions_revoked": 0,
    }
    if dry_run:
        report["organizations"] = {org: "planned" for org in plan.organizations}
        report["credentials"] = {c: "planned" for _, c in plan.credentials}
        report["secrets"] = {s: "planned" for _, s in plan.secrets}
        report["slack_installations"] = {
            g: "planned" for _, _, g in plan.slack_installations
        }
        return report
    for org_id in plan.organizations:
        report["organizations"][org_id] = _purge_organization(runtime, org_id)
    purged = set(plan.organizations)
    for org_id, credential_id in plan.credentials:
        if org_id not in purged:
            report["credentials"][credential_id] = _revoke_credential(
                runtime, org_id, credential_id
            )
    for org_id, how, generation_id in plan.slack_installations:
        if org_id not in purged:
            report["slack_installations"][generation_id] = _end_slack_installation(
                runtime, org_id, how, generation_id
            )
    for org_id, secret_ref in plan.secrets:
        if org_id not in purged:
            report["secrets"][secret_ref] = _delete_secret(runtime, org_id, secret_ref)
    report["sessions_revoked"] = _revoke_sessions(runtime)
    return report
