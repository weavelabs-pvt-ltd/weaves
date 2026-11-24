"""A rich fixture organization and helpers for lifecycle, export and purge tests."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from contracts.v1 import (
    OAuthAuthorizationState,
    Permission,
)
from modules.auth.organization_retention import KEEP_COLLECTIONS
from shared.clock import new_id, utc_now
from tests.support.organization_administration import scope

SKILL = "---\nname: {name}\ndescription: Lifecycle fixture.\n---\nBe brief.\n"


def seed_rich(runtime: Any, owner: Any, marker: str) -> dict[str, Any]:
    """Create active tasks, schedules, skills, OAuth setup and secrets."""
    org, workspace, _, principal, _ = owner
    made: dict[str, Any] = {"marker": marker, "secrets": []}
    with scope(owner):
        agent = runtime.agent_service.create_agent(
            name=f"Agent {marker}", instructions=marker
        )
        workflow = runtime.workflow_definition_service.create_workflow(
            name=f"Flow {marker}", agent_id=agent.id
        )
        runtime.workflow_automation_service.create_workflow_schedule(
            workflow.workflow_id,
            name=f"Schedule {marker}",
            task_instructions=marker,
            interval_seconds=60,
        )
        _, secret = runtime.workflow_automation_service.create_workflow_trigger(
            workflow.workflow_id, name=f"Hook {marker}", task_instructions=marker
        )
        made["secrets"].append(secret)
        task = runtime.task_service.create_task(
            agent_id=agent.id, title=f"Task {marker}", initial_message=f"say {marker}"
        )
        made["task_id"] = task.task_id
        job, _ = runtime.enqueue_agent_run(f"queued {marker}")
        made["job_id"] = job.job_id
        runtime.skills_service.create_personal_skill(
            {"SKILL.md": SKILL.format(name=f"skill-{marker.lower()}")}
        )
        value = f"SECRET-VALUE-{marker}-{new_id()}"
        ref = runtime.secret_store.put(value, purpose="model_provider")
        made["secrets"] += [value, ref]
        provider = runtime.providers.list_scoped(org.id)[0]
        runtime.providers.put(
            provider.model_copy(
                update={
                    "auth_ref": ref,
                    "credential_last4": "ABCD",
                    "updated_at": utc_now(),
                }
            )
        )
        now = utc_now()
        runtime.oauth_authorization_states.create(
            OAuthAuthorizationState(
                oauth_state_id=f"STATE-{marker}-{new_id()}",
                provider="slack",
                step="authorize",
                principal_id=principal.id,
                org_id=org.id,
                workspace_id=workspace.id,
                configuration={},
                secret_ref=ref,
                created_at=now,
                expires_at=now + timedelta(minutes=10),
            )
        )
        _, _, credential, token = runtime.credential_service.create_service_account(
            name=f"svc-{marker}", permissions=(Permission.AGENTS_RUN,)
        )
        made["secrets"] += [token, credential.token_digest]
        made["agent_id"] = agent.id
    return made


def rows(runtime: Any, org_id: str, *, keep: bool = True) -> dict[str, list[str]]:
    """Canonical JSON of every row an organization owns, by collection."""
    data = runtime._repository_set.organization_data
    out: dict[str, list[str]] = {}
    for name in runtime._repository_set.names():
        collected = []
        after = None
        while True:
            page = data.page(name, org_id, after=after, limit=500)
            collected += [json.dumps(p, sort_keys=True, default=str) for _, p in page]
            if len(page) < 500:
                break
            after = page[-1][0]
        if collected and (keep or name not in KEEP_COLLECTIONS):
            out[name] = collected
    return out


def digest(snapshot: dict[str, list[str]]) -> str:
    """Byte-stable fingerprint of a snapshot."""
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
