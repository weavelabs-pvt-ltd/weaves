"""Migration 31: remove the data of the retired "Approvals & Policy" section.

The signed-in app's "Approvals & Policy" (approval policies, approval requests
and their decisions) and "Artifacts & Audit" sections are gone, together with
the routes that served only them. This migration deletes what only they owned:

* every ``approval_policies`` and ``approval_requests`` row;
* tool invocations that belong to an approval (``approval_request_id`` set,
  only the retired decision flow wrote them), and the retired
  ``approval_request_id`` key on the remaining invocations;
* the retired ``approval_policy_id`` key on agent versions;
* the retired ``approval_policies.manage`` permission inside stored roles;
* the indexes that served the removed queries.

Kept on purpose: audit events (still recorded by every service), artifacts
(runs and evaluations write and read them) and workflow approval steps (they
live in ``workflow_runs`` and are decided in the run view).

Every statement is idempotent, so the migration is safe on an empty database
and when re-run by hand.
"""

from __future__ import annotations

# Collections that used to hold records and no longer exist in
# ``REPOSITORY_SPECS``; the collection-drift tests accept them as removed.
REMOVED_COLLECTIONS: tuple[str, ...] = ("approval_policies", "approval_requests")

_COLLECTION_LITERALS = ", ".join(f"'{name}'" for name in REMOVED_COLLECTIONS)

APPROVALS_REMOVAL_MIGRATION: tuple[int, tuple[str, ...]] = (
    31,
    (
        f"DELETE FROM product_records WHERE collection IN ({_COLLECTION_LITERALS})",
        """DELETE FROM product_records
               WHERE collection = 'tool_invocations'
                 AND payload->>'approval_request_id' IS NOT NULL""",
        """UPDATE product_records SET payload = payload - 'approval_request_id'
               WHERE collection = 'tool_invocations'
                 AND payload->'approval_request_id' IS NOT NULL""",
        """UPDATE product_records SET payload = payload - 'approval_policy_id'
               WHERE collection = 'agent_versions'
                 AND payload->'approval_policy_id' IS NOT NULL""",
        """UPDATE product_records
               SET payload = jsonb_set(
                   payload, '{permissions}',
                   (payload->'permissions') - 'approval_policies.manage')
               WHERE collection = 'roles'
                 AND payload->'permissions' @> '["approval_policies.manage"]'::jsonb""",
        "DROP INDEX IF EXISTS approval_requests_page_idx",
        "DROP INDEX IF EXISTS approval_requests_pending_idx",
        "DROP INDEX IF EXISTS tool_invocations_approval_idx",
    ),
)
