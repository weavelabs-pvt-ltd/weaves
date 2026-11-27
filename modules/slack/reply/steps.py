"""Real pipeline progress for the working state: what has the run actually read?

While a Slack turn runs, its approved connectors (``origin.verdict.tools_green``)
are read one by one and each read leaves a ``ToolInvocation``. The step shown to
the asker is derived from those records, not from a timer: the next approved
connector that has no invocation yet, then "writing the answer". Display names
are the installations' own, never anything a user wrote.
"""

from __future__ import annotations

from application.ports.repository import Repository
from contracts.v1 import PluginInstallation, TaskTurn, ToolInvocation, WorkflowRun


class RunSteps:
    """Read the connector reads of a running turn."""

    def __init__(
        self,
        *,
        runs: Repository[WorkflowRun],
        invocations: Repository[ToolInvocation],
        installations: Repository[PluginInstallation],
    ) -> None:
        """Bind the (tenant-scoped) run, invocation and installation records."""
        self._runs = runs
        self._invocations = invocations
        self._installations = installations

    def waiting_for(self, turn: TaskTurn) -> list[str]:
        """Display names of approved connectors the run has not read yet."""
        verdict = turn.origin.verdict if turn.origin else None
        if verdict is None or not verdict.tools_green:
            return []
        done = self._done_capabilities(turn)
        names = self._display_names(turn.org_id, turn.workspace_id)
        return [
            names.get(capability, capability)
            for capability in verdict.tools_green
            if capability not in done
        ]

    def _done_capabilities(self, turn: TaskTurn) -> set[str]:
        if not turn.run_idempotency_key:
            return set()
        run = self._runs.find_by_idempotency_key(
            turn.run_idempotency_key,
            turn.requested_by_principal_id,
            turn.org_id,
            turn.workspace_id,
        )
        if run is None:
            return set()
        found = self._invocations.list_by_field("run_id", run.run_id)
        return {item.capability_id for item in found if item.org_id == turn.org_id}

    def _display_names(self, org_id: str, workspace_id: str) -> dict[str, str]:
        names: dict[str, str] = {}
        for installation in self._installations.list_scoped(org_id, workspace_id):
            for capability in installation.enabled_capability_ids:
                names.setdefault(capability, installation.display_name)
        return names
