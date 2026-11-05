"""Publish canvas plans and advance durable workflows through real agent tasks."""

from contextlib import contextmanager
from datetime import timedelta
from threading import RLock
from zoneinfo import ZoneInfo

from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    Permission,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowVersion,
)
from contracts.v1.workflow_studio import Calendar, CanvasPlan, CanvasRun, CanvasSettings
from shared.clock import new_id, utc_now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
    reset_request_principal,
    set_request_identity,
)


class WorkflowStudioService:
    """Use transaction locks and idempotent tasks to survive worker restarts."""

    def __init__(
        self,
        *,
        definitions,
        tasks,
        runs,
        settings,
        unit_of_work,
        lock=None,
        artifacts=None,
    ):
        """Bind persistence, authorization, task execution and transaction ports."""
        self.definitions, self.tasks = definitions, tasks
        self.runs, self.settings = runs, settings
        self.unit_of_work, self.lock = unit_of_work, lock
        self.local_lock = RLock()
        self.artifacts = artifacts

    def authorize(self, permission):
        """Enforce workspace permissions at every public operation."""
        self.definitions.authorize(
            effective_principal_id("local-developer"), permission
        )

    @contextmanager
    def guard(self, key):
        """Serialize publication, dispatch, and decisions across processes."""
        with self.local_lock, self.unit_of_work():
            if self.lock:
                self.lock("workflow-studio:" + key)
            yield

    @staticmethod
    def next_dates(calendar, now=None):
        """Compute the next three real occurrences in Asia/Kolkata."""
        cal = Calendar.model_validate(calendar)
        if any(day < 0 or day > 6 for day in cal.days):
            raise ValueError("Weekdays must be between Monday and Sunday")
        now = (now or utc_now()).astimezone(ZoneInfo("Asia/Kolkata"))
        if cal.freq == "hourly":
            first = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            return [first + timedelta(hours=i) for i in range(3)]
        hour, minute = map(int, cal.time.split(":"))
        dates = []
        for offset in range(22):
            candidate = (now + timedelta(days=offset)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate > now and candidate.weekday() in cal.days:
                dates.append(candidate)
            if len(dates) == 3:
                break
        return dates

    def publish(self, name, description, plan, workflow_id=None):
        """Atomically save metadata, the immutable plan, and start controls."""
        self.authorize(Permission.WORKFLOWS_MANAGE)
        plan = CanvasPlan.model_validate(plan)
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
        with self.guard(workflow_id or "publish"):
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
            config.update(agent_ids=ids, canvas=plan.model_dump(mode="json"))
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
            now = utc_now()
            try:
                old = self.settings.get_scoped(
                    workflow.workflow_id, workflow.org_id, workflow.workspace_id
                )
                created = old.created_at
            except KeyError:
                created = now
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
                next_run_at=self.next_dates(plan.calendar.model_dump())[0]
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

    def get_settings(self, workflow_id):
        """Read start controls only inside the active workspace."""
        self.authorize(Permission.WORKFLOWS_VIEW)
        workflow = self.definitions.get_workflow(workflow_id)
        try:
            return self.settings.get_scoped(
                workflow_id, workflow.org_id, workflow.workspace_id
            )
        except KeyError:
            version = self.definitions.get_current_workflow_version(workflow)
            plan = dict(version.configuration).get("canvas", {})
            now = utc_now()
            return CanvasSettings(
                workflow_id=workflow_id,
                org_id=workflow.org_id,
                workspace_id=workflow.workspace_id,
                principal_id=effective_principal_id("local-developer"),
                manual=plan.get("manual", True),
                hook=plan.get("hook", False),
                sched=plan.get("sched", False),
                calendar=plan.get("calendar", Calendar().model_dump()),
                created_at=now,
                updated_at=now,
            )

    def save_settings(self, workflow_id, changes):
        """Update actual dispatch controls, preserving the published step version."""
        self.authorize(Permission.WORKFLOWS_MANAGE)
        with self.guard(workflow_id):
            current = self.get_settings(workflow_id)
            updated = CanvasSettings.model_validate(
                current.model_copy(
                    update={**changes, "updated_at": utc_now()}
                ).model_dump()
            )
            dates = self.next_dates(updated.calendar)
            updated.next_run_at = dates[0] if updated.sched else None
            self.settings.put(updated)
            return updated

    def audit(self, action, target, summary):
        """Record human-visible workflow decisions without storing input content."""
        self.definitions.audit_events.create(
            AuditEvent(
                audit_event_id=new_id(),
                org_id=effective_org_id(),
                workspace_id=effective_workspace_id(),
                target_scope=AuditTargetScope.WORKSPACE,
                actor_principal_id=effective_principal_id("local-developer"),
                action=action,
                target_type="workflow_run",
                target_id=target,
                summary=summary,
                request_id=target,
                metadata={},
                created_at=utc_now(),
            )
        )

    def list_runs(self, workflow_id):
        """Return the workflow's durable runs newest first."""
        self.authorize(Permission.WORKFLOWS_VIEW)
        self.definitions.get_workflow(workflow_id)
        return sorted(
            (
                r
                for r in self.runs.list_scoped(
                    effective_org_id(), effective_workspace_id()
                )
                if r.workflow_id == workflow_id
            ),
            key=lambda r: r.created_at,
            reverse=True,
        )[:100]

    def get_run(self, workflow_id, run_id):
        """Prevent cross-workflow and cross-tenant run inspection."""
        self.authorize(Permission.WORKFLOWS_VIEW)
        run = self.runs.get_scoped(run_id, effective_org_id(), effective_workspace_id())
        if run.workflow_id != workflow_id:
            raise KeyError(run_id)
        return run

    def start(
        self,
        workflow_id,
        instructions,
        context=None,
        key=None,
        source="manual",
        retry=None,
    ):
        """Queue a pinned plan, preserving completed results on retry."""
        self.authorize(Permission.WORKFLOWS_RUN)
        self.authorize(Permission.AGENTS_RUN)
        with self.guard(workflow_id):
            workflow = self.definitions.get_workflow(workflow_id)
            if workflow.status != WorkflowStatus.ACTIVE:
                raise ValueError("Enable the workflow to run it")
            settings = self.get_settings(workflow_id)
            if source == "manual" and not settings.manual:
                raise ValueError("On-demand runs are off")
            version = self.definitions.get_current_workflow_version(workflow)
            canvas = dict(version.configuration).get("canvas")
            if not canvas:
                ids = dict(version.configuration).get("agent_ids", [])
                canvas = {
                    "steps": [
                        {
                            "id": new_id(),
                            "type": "agent",
                            "a": [a],
                            "t": instructions or workflow.description or workflow.name,
                            "fail": "stop",
                        }
                        for a in ids
                    ]
                }
            plan = CanvasPlan.model_validate(canvas)
            self.definitions._select_workflow_agents(
                tuple(dict.fromkeys(a for s in plan.steps for a in s.a))
            )
            for run in self.list_runs(workflow_id):
                if (
                    key
                    and run.idempotency_key == key
                    and run.requested_by_principal_id
                    == effective_principal_id("local-developer")
                ):
                    if (
                        run.instructions
                        != (instructions or "Run with default instructions")
                        or run.context != (context or [])
                        or run.retry_of != retry
                    ):
                        raise ValueError(
                            "Idempotency key already used with a different run request"
                        )
                    return run
            now, run_id = utc_now(), new_id()
            states = [
                {
                    "id": s.id,
                    "status": "queued",
                    "tasks": [],
                    "attempt": 1,
                    "output": "",
                }
                for s in plan.steps
            ]
            if retry:
                previous = self.get_run(workflow_id, retry)
                if previous.status != "failed":
                    raise ValueError("Only failed runs can be retried")
                plan = CanvasPlan.model_validate(previous.plan)
                failed_index = next(
                    (
                        i
                        for i, state in enumerate(previous.states)
                        if state["status"] == "failed"
                    ),
                    0,
                )
                states = [
                    dict(s)
                    if i < failed_index
                    else {
                        "id": s["id"],
                        "status": "queued",
                        "tasks": [],
                        "attempt": 1,
                        "output": "",
                    }
                    for i, s in enumerate(previous.states)
                ]
                version = self.definitions.get_workflow_version(
                    workflow_id, previous.workflow_version_id
                )
            pinned = {
                self.definitions.agent_versions.get_scoped(
                    vid, workflow.org_id, workflow.workspace_id
                ).agent_id: vid
                for vid in version.agent_version_ids
            }
            run = CanvasRun(
                org_id=workflow.org_id,
                workspace_id=workflow.workspace_id,
                agent_versions=pinned,
                run_id=run_id,
                job_id=run_id,
                workflow_id=workflow_id,
                workflow_version_id=version.workflow_version_id,
                requested_by_principal_id=effective_principal_id("local-developer"),
                instructions=instructions or "Run with default instructions",
                context=context or [],
                trigger_type=source,
                idempotency_key=key,
                plan=plan.model_dump(mode="json"),
                states=states,
                created_at=now,
                updated_at=now,
                retry_of=retry,
            )
            self.runs.create(run)
            self.audit(
                "workflow.canvas.queued", run.run_id, "Workflow execution queued"
            )
            return run

    def decide(self, workflow_id, run_id, action):
        """Persist stop/approval decisions before the worker can advance."""
        self.authorize(
            Permission.ACTIONS_APPROVE
            if action in {"approve", "reject"}
            else Permission.WORKFLOWS_RUN
        )
        with self.guard(run_id):
            run = self.get_run(workflow_id, run_id)
            if action == "stop":
                if run.status not in {"queued", "running", "awaiting_approval"}:
                    raise ValueError("This run has already finished")
                self._cancel_tasks(run.states, run)
                run.status, run.finished_at = "cancelled", utc_now()
            else:
                if run.status != "awaiting_approval":
                    raise ValueError("This run is not waiting for approval")
                step = next(s for s in run.states if s["status"] == "awaiting_approval")
                step.update(
                    status="succeeded" if action == "approve" else "failed",
                    finished_at=utc_now().isoformat(),
                    decision_by=effective_principal_id("local-developer"),
                )
                run.status = "running" if action == "approve" else "cancelled"
                if action == "reject":
                    run.error, run.finished_at = (
                        "Approval rejected. No subsequent steps were started.",
                        utc_now(),
                    )
            if run.status == "cancelled":
                for state in run.states:
                    if state["status"] in {"queued", "running", "awaiting_approval"}:
                        state["status"] = "skipped"
            run.updated_at = utc_now()
            self.runs.put(run)
            self.audit(
                "workflow.canvas." + action, run.run_id, "Workflow decision: " + action
            )
            return run

    def _cancel_tasks(self, states, run):
        """Revoke queued or running child work before advancing past a failure."""
        for state in states:
            for task_id in state.get("tasks", []):
                task = self.tasks.tasks.get_scoped(
                    task_id, run.org_id, run.workspace_id
                )
                if task.active_turn_id:
                    self.tasks.cancel_turn(task_id, task.active_turn_id)

    def advance(self, run):
        """Advance at most one stage using durable child task results."""
        with self.guard(run.run_id):
            run = self.runs.get_scoped(run.run_id, run.org_id, run.workspace_id)
            if run.status not in {"queued", "running"}:
                return
            if not run.started_at:
                run.started_at = utc_now()
            run.status = "running"
            index = next(
                (
                    i
                    for i, s in enumerate(run.states)
                    if s["status"] not in {"succeeded", "skipped"}
                ),
                None,
            )
            if index is None:
                run.status, run.finished_at = "succeeded", utc_now()
                self.audit(
                    "workflow.canvas.completed",
                    run.run_id,
                    "Workflow execution completed",
                )
                run.summary = next(
                    (
                        str(state.get("output", ""))
                        for state in reversed(run.states)
                        if state.get("output")
                    ),
                    "Workflow finished. Review the recorded steps and their sources.",
                )[-12000:]
            else:
                self._advance_step(run, index)
            run.updated_at = utc_now()
            self.runs.put(run)

    def _advance_step(self, run, index):
        """Dispatch agent/parallel/condition tasks, or pause at an approval."""
        step, state = run.plan["steps"][index], run.states[index]
        if step["type"] == "appr":
            state.update(status="awaiting_approval", started_at=utc_now().isoformat())
            run.status = "awaiting_approval"
            return
        if not state["tasks"]:
            self._dispatch_step(run, index, step, state)
            return
        tasks = [
            self.tasks.tasks.get_scoped(t, run.org_id, run.workspace_id)
            for t in state["tasks"]
        ]
        failed = next(
            (t for t in tasks if str(t.latest_turn_status) in {"failed", "cancelled"}),
            None,
        )
        if failed:
            self._fail(
                run,
                index,
                failed.latest_response_summary
                or "The agent task failed. Check its model and connections, "
                "then retry.",
            )
            return
        if not all(str(t.latest_turn_status) == "succeeded" for t in tasks):
            return
        outputs = []
        for task in tasks:
            messages = self.tasks.messages.list_task_messages_page(
                task.task_id,
                run.org_id,
                run.workspace_id,
                cursor_created_at=None,
                cursor_message_id=None,
                limit=100,
            )
            outputs.extend(
                m.content for m in reversed(messages) if str(m.role) == "assistant"
            )
        self._collect_artifacts(run, tasks)
        output = "\n\n".join(outputs)
        if step["type"] == "cond":
            import json

            try:
                answer = json.loads(
                    output.strip().strip("`").removeprefix("json").strip().lower()
                )
                if not isinstance(answer, bool):
                    raise ValueError("not a boolean")
            except (ValueError, TypeError):
                self._fail(
                    run, index, "The condition evaluator did not return true or false."
                )
                return
            state["matched"] = answer
            if not answer and index + 1 < len(run.states):
                run.states[index + 1]["status"] = "skipped"
        state.update(
            status="succeeded",
            finished_at=utc_now().isoformat(),
            output=output[-32000:],
        )

    def _collect_artifacts(self, run, tasks):
        """Collect only artifacts produced by the run's actual child turns."""
        if self.artifacts:
            known = {a["artifact_id"] for a in run.artifacts}
            for task in tasks:
                turn = self.tasks.turns.get_scoped(
                    task.latest_turn_id, run.org_id, run.workspace_id
                )
                for artifact in self.artifacts.list_scoped(
                    run.org_id, run.workspace_id
                ):
                    if (
                        artifact.agent_run_id == turn.agent_run_id
                        and artifact.artifact_id not in known
                    ):
                        run.artifacts.append(artifact.model_dump(mode="json"))
                        known.add(artifact.artifact_id)

    def _dispatch_step(self, run, index, step, state):
        """Create idempotent child tasks pinned to the selected agent versions."""
        agent_ids = step["a"]
        if step["type"] == "cond":
            agent_ids = next(
                (s["a"][:1] for s in reversed(run.plan["steps"][:index]) if s["a"]),
                [],
            )
        if not agent_ids:
            self._fail(run, index, "A condition requires an earlier agent result.")
            return
        prior = "\n\n".join(str(s.get("output", "")) for s in run.states[:index])[
            -16000:
        ]
        instruction = (
            f"{run.instructions}\n\nStep: {step['t']}\n\n"
            f"Previous step results:\n{prior}\n\nSelected context:\n"
            + "\n".join(run.context)
        )
        if step["type"] == "cond":
            instruction = (
                f"Evaluate this condition against the results below: {step['t']}\n"
                "Return only true or false. Do not take any action or use tools.\n"
                f"{prior}"
            )
        for member, agent_id in enumerate(agent_ids):
            task = self.tasks.create_task(
                agent_id=agent_id,
                title=step["t"][:160],
                initial_message=instruction[:32000],
                idempotency_key=f"canvas:{run.run_id}:{index}:{member}:{state['attempt']}",
                workflow_id=run.workflow_id,
                pinned_version_id=run.agent_versions.get(agent_id),
                condition_only=step["type"] == "cond",
            )
            state["tasks"].append(task.task_id)
        state.update(status="running", started_at=utc_now().isoformat())
        return

    def _fail(self, run, index, error):
        """Apply stop, two retries, or skip without losing earlier results."""
        step, state = run.plan["steps"][index], run.states[index]
        self._cancel_tasks([state], run)
        if step["fail"] == "retry" and state["attempt"] < 3:
            state.update(status="queued", tasks=[], attempt=state["attempt"] + 1)
        elif step["fail"] == "skip":
            state.update(
                status="skipped", output=error, finished_at=utc_now().isoformat()
            )
        else:
            state.update(
                status="failed", output=error, finished_at=utc_now().isoformat()
            )
            run.status, run.error, run.finished_at = "failed", error, utc_now()
            for later in run.states[index + 1 :]:
                later["status"] = "skipped"

    def tick(self):
        """Resume queued work and dispatch due calendar occurrences after restart."""
        for settings in self.settings.list():
            if (
                not settings.sched
                or not settings.next_run_at
                or settings.next_run_at > utc_now()
            ):
                continue
            token = set_request_identity(
                settings.principal_id, settings.org_id, settings.workspace_id
            )
            try:
                with self.guard(settings.workflow_id):
                    current = self.settings.get_scoped(
                        settings.workflow_id, settings.org_id, settings.workspace_id
                    )
                    if not current.sched or current.next_run_at > utc_now():
                        continue
                    workflow = self.definitions.get_workflow(settings.workflow_id)
                    if workflow.status == WorkflowStatus.ACTIVE:
                        self.start(
                            settings.workflow_id,
                            workflow.description or workflow.name,
                            key=f"calendar:{settings.workflow_id}:{current.next_run_at.isoformat()}",
                            source="schedule",
                        )
                    current.last_run_at, current.next_run_at = (
                        utc_now(),
                        self.next_dates(current.calendar)[0],
                    )
                    current.updated_at = utc_now()
                    current.last_error = None
                    self.settings.put(current)
            except (PermissionError, KeyError, ValueError):
                with self.guard(settings.workflow_id):
                    current = self.settings.get_scoped(
                        settings.workflow_id, settings.org_id, settings.workspace_id
                    )
                    current.sched, current.next_run_at = False, None
                    current.last_error = (
                        "Schedule paused because its owner no longer has access "
                        "or its workflow configuration is unavailable."
                    )
                    current.updated_at = utc_now()
                    self.settings.put(current)
            finally:
                reset_request_principal(token)
        for run in self.runs.list():
            if run.status not in {"queued", "running"}:
                continue
            token = set_request_identity(
                run.requested_by_principal_id, run.org_id, run.workspace_id
            )
            try:
                self.advance(run)
            except Exception as exc:
                with self.guard(run.run_id):
                    latest = self.runs.get(run.run_id)
                    if latest.status in {"queued", "running"}:
                        latest.status, latest.error, latest.finished_at = (
                            "failed",
                            str(exc)[:1000],
                            utc_now(),
                        )
                        latest.updated_at = utc_now()
                        self.runs.put(latest)
            finally:
                reset_request_principal(token)
