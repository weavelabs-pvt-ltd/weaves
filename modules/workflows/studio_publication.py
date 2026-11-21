"""Publish immutable canvas versions and manage workflow start controls."""

from pydantic import JsonValue

from contracts.v1 import (
    Permission,
    WorkflowDefinition,
    WorkflowVersion,
)
from contracts.v1.timezones import validate_timezone_name
from contracts.v1.workflow_studio import (
    CanvasPlan,
    CanvasSettings,
)
from modules.workflows.schedule_time import next_occurrence
from modules.workflows.studio_context import WorkflowStudioContext
from modules.workflows.studio_settings_edit import apply_settings_changes
from shared.clock import new_id, utc_now
from shared.request_context import (
    effective_principal_id,
)


class WorkflowStudioPublicationMixin(WorkflowStudioContext):
    """Validate and publish immutable workflow plans."""

    def publish(
        self,
        name: str,
        description: str,
        plan: CanvasPlan,
        workflow_id: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, dict[str, JsonValue]]:
        """Atomically save metadata, the immutable plan, and start controls.

        New settings use ``timezone`` or the organization's zone; republishing
        keeps the stored zone (a legacy record stays Asia/Kolkata) unless named.
        """
        self.authorize(Permission.WORKFLOWS_MANAGE)
        plan = CanvasPlan.model_validate(plan)
        ids = self._validate_publication_plan(plan)
        with self.guard(workflow_id or "publish"):
            workflow, version = self._publish_plan_version(
                name, description, plan, ids, workflow_id
            )
            now = utc_now()
            zone = validate_timezone_name(timezone) if timezone else None
            try:
                old = self.settings.get_scoped(
                    workflow.workflow_id, workflow.org_id, workflow.workspace_id
                )
                created = old.created_at
                zone = zone or old.timezone
            except KeyError:
                created = now
                zone = zone or self.default_timezone()
            settings = CanvasSettings(
                workflow_id=workflow.workflow_id,
                org_id=workflow.org_id,
                workspace_id=workflow.workspace_id,
                principal_id=effective_principal_id("local-developer"),
                manual=plan.manual,
                hook=plan.hook,
                event=plan.event,
                sched=plan.sched,
                calendar=plan.calendar.model_dump(),
                timezone=zone,
                next_run_at=next_occurrence(plan.calendar, now, zone)
                if plan.sched
                else None,
                created_at=created,
                updated_at=now,
            )
            self.settings.put(settings)
            return {
                "definition": workflow.model_dump(mode="json"),
                "version": version.model_dump(mode="json"),
            }

    def _validate_publication_plan(self, plan: CanvasPlan) -> list[str]:
        """Validate connected agents, start controls, and condition ordering."""
        if not (plan.manual or plan.hook or plan.sched):
            raise ValueError("Choose at least one way for it to start.")
        if len({s.id for s in plan.steps}) != len(plan.steps):
            raise ValueError("Step identifiers must be unique")
        ids = list(dict.fromkeys(a for step in plan.steps for a in step.a))
        if not ids:
            raise ValueError("Add at least one agent step before publishing.")
        if any(
            (s.type == "agent" and len(s.a) != 1)
            or (s.type == "par" and len(s.a) < 2)
            or (s.type in {"cond", "appr"} and s.a)
            for s in plan.steps
        ):
            raise ValueError(
                "Choose an agent for each agent step, and two or three "
                "for parallel steps"
            )
        self.next_dates(plan.calendar.model_dump())
        has_result = False
        for step in plan.steps:
            if step.type == "cond" and not has_result:
                raise ValueError(
                    "Add an agent before a condition so it has a result to check"
                )
            has_result = has_result or bool(step.a)
        return ids

    def _publish_plan_version(
        self,
        name: str,
        description: str,
        plan: CanvasPlan,
        ids: list[str],
        workflow_id: str | None,
    ) -> tuple[WorkflowDefinition, WorkflowVersion]:
        """Persist the definition and immutable plan inside the caller's lock."""
        if workflow_id:
            workflow = self.definitions.get_workflow(workflow_id)
            self.definitions._select_workflow_agents(tuple(ids))
            current = self.definitions.get_current_workflow_version(workflow)
            # Grant access to newly selected agents through existing publication.
            self.definitions.replace_workflow_agents(workflow_id, tuple(ids))
            published = self.definitions.get_current_workflow_version(
                self.definitions.get_workflow(workflow_id)
            )
            version_number = current.version + 1
        else:
            workflow = self.definitions.create_workflow(
                name=name, description=description, agent_ids=tuple(ids)
            )
            published = self.definitions.get_current_workflow_version(workflow)
            version_number = 1
        config = dict(published.configuration)
        config["agent_ids"] = [a for a in ids]
        config["canvas"] = plan.model_dump(mode="json")
        version_id = (
            published.workflow_version_id
            if not workflow_id
            or published.workflow_version_id != current.workflow_version_id
            else new_id()
        )
        version = WorkflowVersion.model_validate(
            published.model_copy(
                update={
                    "workflow_version_id": version_id,
                    "configuration": config,
                    "version": version_number,
                    "created_at": utc_now(),
                }
            ).model_dump()
        )
        self.definitions.workflow_versions.put(version)
        workflow = WorkflowDefinition.model_validate(
            workflow.model_copy(
                update={
                    "name": name.strip(),
                    "description": description.strip(),
                    "current_version_id": version.workflow_version_id,
                    "updated_at": utc_now(),
                }
            ).model_dump()
        )
        self.definitions.workflows.put(workflow)
        return workflow, version

    def save_settings(
        self, workflow_id: str, changes: dict[str, JsonValue]
    ) -> CanvasSettings:
        """Update actual dispatch controls, preserving the published step version."""
        self.authorize(Permission.WORKFLOWS_MANAGE)
        with self.guard(workflow_id):
            workflow = self.definitions.get_workflow(workflow_id)
            try:
                self.settings.get_scoped(
                    workflow_id, workflow.org_id, workflow.workspace_id
                )
                stored = True
            except KeyError:
                stored = False
            current = self.get_settings(workflow_id)
            updated = apply_settings_changes(
                current, changes, now=utc_now(), stored=stored
            )
            self.settings.put(updated)
            return updated
