"""Owner-only, bounded, secret-free JSON export of one organization."""

from __future__ import annotations

import json
import os
from typing import Any

from application.ports.organization_data import OrganizationData
from contracts.v1 import OrganizationLifecycleState
from modules.auth.lifecycle_actor import Actor
from modules.auth.organization_export_rules import (
    EXPORT_COLLECTIONS,
    Withheld,
    scrub,
    shape,
)
from modules.auth.organization_lifecycle import OrganizationLifecycleService
from shared.request_context import effective_org_id

State = OrganizationLifecycleState
PAGE = 500
_EXPORTABLE = {State.ACTIVE, State.DELETION_REQUESTED}


class ExportTooLarge(Exception):
    """The export would exceed the configured byte limit."""


def max_bytes_from_env() -> int:
    """Read the export size limit (bytes), defaulting to 50 MB."""
    raw = os.environ.get("WEAVES_ORGANIZATION_EXPORT_MAX_BYTES", "52428800").strip()
    if not raw.isdigit() or int(raw) < 1024:
        raise ValueError("WEAVES_ORGANIZATION_EXPORT_MAX_BYTES must be >= 1024")
    return int(raw)


class OrganizationExportService:
    """Build the export and record ``organization.exported`` on success."""

    def __init__(
        self,
        *,
        lifecycle: OrganizationLifecycleService,
        data: OrganizationData,
        max_bytes: int,
    ) -> None:
        """Bind the lifecycle service (owner check, audit) and the data port."""
        self.lifecycle = lifecycle
        self.data = data
        self.max_bytes = max_bytes

    def export(self) -> bytes:
        """Return the JSON document; raises ``PermissionError`` unless owner."""
        owner = self.lifecycle.require_owner()
        org_id = effective_org_id()
        if self.lifecycle.state_of(org_id) not in _EXPORTABLE:
            raise PermissionError("Organization not available")
        withheld = self._collect_withheld(org_id, owner.ref)
        used = 0
        collections: dict[str, list[dict[str, Any]]] = {}
        for name in EXPORT_COLLECTIONS:
            rows = []
            for row in self._rows(name, org_id, withheld):
                used += len(json.dumps(row, default=str))
                if used > self.max_bytes:
                    raise ExportTooLarge(
                        f"The organization export is larger than the configured "
                        f"limit of {self.max_bytes} bytes"
                    )
                rows.append(row)
            collections[name] = rows
        document = self._document(org_id, collections, withheld)
        body = json.dumps(document, default=str, indent=1).encode()
        if len(body) > self.max_bytes + 65536:
            raise ExportTooLarge("The organization export exceeds the size limit")
        self._audit(owner, collections, len(body), withheld)
        return body

    def _document(
        self,
        org_id: str,
        collections: dict[str, list[dict[str, Any]]],
        withheld: Withheld,
    ) -> dict[str, Any]:
        lifecycle = self.lifecycle
        settings = lifecycle.guard.current_settings()
        record = lifecycle.current(org_id)
        history = [e.model_dump(mode="json") for e in record.history]
        return {
            "export_version": 1,
            "exported_at": lifecycle.now().isoformat(),
            "organization": lifecycle.organizations.get(org_id).model_dump(mode="json"),
            "settings": scrub(settings.model_dump(mode="json")),
            "lifecycle": {
                "state": lifecycle.state_of(org_id).value,
                "reason": record.reason,
                "grace_deadline": record.grace_deadline,
                "history": history,
            },
            "withheld": {
                **withheld.counts,
                "note": "Other members' private tasks and personal skills "
                "are not included.",
            },
            "excluded_categories": [
                "secrets",
                "api credentials and token digests",
                "sessions, cookies and oauth states",
                "other organizations",
            ],
            "data": collections,
        }

    def _pages(self, collection: str, org_id: str) -> Any:
        after: str | None = None
        while True:
            page = self.data.page(collection, org_id, after=after, limit=PAGE)
            for _, payload in page:
                if payload.get("org_id", org_id) != org_id:
                    raise RuntimeError("export row belongs to another organization")
                yield payload
            if len(page) < PAGE:
                return
            after = page[-1][0]

    def _rows(self, name: str, org_id: str, withheld: Withheld) -> Any:
        for payload in self._pages(name, org_id):
            if withheld.keep(name, payload):
                yield shape(name, payload)

    def _collect_withheld(self, org_id: str, actor: str) -> Withheld:
        """First pass: find other members' private tasks, runs and skills."""
        withheld = Withheld(actor)
        for row in self._pages("tasks", org_id):
            withheld.note_task(row)
        for row in self._pages("skill_definitions", org_id):
            withheld.note_skill(row)
        self._withhold_runs(org_id, withheld)
        self._withhold_skill_versions(org_id, withheld)
        return withheld

    def _withhold_runs(self, org_id: str, withheld: Withheld) -> None:
        if not withheld.tasks:
            return
        for row in self._pages("task_turns", org_id):
            if row.get("task_id") in withheld.tasks and row.get("agent_run_id"):
                withheld.agent_runs.add(str(row["agent_run_id"]))
        for row in self._pages("agent_runs", org_id):
            if row.get("agent_run_id") in withheld.agent_runs:
                withheld.runs.add(str(row["run_id"]))

    def _withhold_skill_versions(self, org_id: str, withheld: Withheld) -> None:
        if not withheld.skills:
            return
        for row in self._pages("skill_versions", org_id):
            if row.get("skill_id") in withheld.skills:
                withheld.skill_versions.add(str(row["skill_version_id"]))

    def _audit(
        self,
        owner: Actor,
        collections: dict[str, list[dict[str, Any]]],
        size: int,
        withheld: Withheld,
    ) -> None:
        with self.lifecycle.guard.mutation():
            self.lifecycle.audit(
                effective_org_id(),
                owner,
                "organization.exported",
                "Organization data exported by the owner",
                {
                    "actor_kind": owner.kind.value,
                    "bytes": size,
                    "counts": {k: len(v) for k, v in collections.items()},
                    "withheld": withheld.counts,
                },
            )
