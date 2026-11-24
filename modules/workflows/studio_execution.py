"""Dispatch and advance workflows using pinned durable agent tasks."""

from pydantic import TypeAdapter

from contracts.v1 import (
    Permission,
    Task,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowVersion,
)
from contracts.v1.workflow_studio import (
    CanvasPlan,
    CanvasRun,
    CanvasStep,
    CanvasStepState,
)
from modules.workflows.studio_context import WorkflowStudioContext
from shared.clock import new_id, utc_now
from shared.request_context import (
    effective_principal_id,
)


class WorkflowStudioExecutionMixin(WorkflowStudioContext):
    """Dispatch and advance durable workflow stages."""

    def start(
        self,
        workflow_id: str,
        instructions: str,
        context: list[str] | None = None,
        key: str | None = None,
        source: str = "manual",
        retry: str | None = None,
    ) -> CanvasRun:
        """Queue a pinned plan, preserving completed results on retry."""
        self.authorize(Permission.WORKFLOWS_RUN)
        self.authorize(Permission.AGENTS_RUN)
        with self.guard(workflow_id):
            workflow, version, plan = self._execution_target(
                workflow_id, instructions, source
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
            plan, version, states = self._execution_states(
                workflow_id, plan, version, retry
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

    def _execution_target(
        self, workflow_id: str, instructions: str, source: str
    ) -> tuple[WorkflowDefinition, WorkflowVersion, CanvasPlan]:
        """Resolve an enabled workflow and validate its selected agents."""
        workflow = self.definitions.get_workflow(workflow_id)
        if workflow.status != WorkflowStatus.ACTIVE:
            raise ValueError("Enable the workflow to run it")
        settings = self.get_settings(workflow_id)
        if source == "manual" and not settings.manual:
            raise ValueError("On-demand runs are off")
        version = self.definitions.get_current_workflow_version(workflow)
        canvas = dict(version.configuration).get("canvas")
        if not canvas:
            ids = TypeAdapter(list[str]).validate_python(
                dict(version.configuration).get("agent_ids", [])
            )
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
        return workflow, version, plan

    def _execution_states(
        self,
        workflow_id: str,
        plan: CanvasPlan,
        version: WorkflowVersion,
        retry: str | None,
    ) -> tuple[CanvasPlan, WorkflowVersion, list[CanvasStepState]]:
        """Start queued progress or preserve completed stages for a retry."""
        states: list[CanvasStepState] = [
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
                s.copy()
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
        return plan, version, states

    def decide(self, workflow_id: str, run_id: str, action: str) -> CanvasRun:
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
                    {
                        "status": "succeeded" if action == "approve" else "failed",
                        "finished_at": utc_now().isoformat(),
                        "decision_by": effective_principal_id("local-developer"),
                    }
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

    def _cancel_tasks(self, states: list[CanvasStepState], run: CanvasRun) -> None:
        """Revoke queued or running child work before advancing past a failure."""
        for state in states:
            for task_id in state.get("tasks", []):
                task = self.tasks.tasks.get_scoped(
                    task_id, run.org_id, run.workspace_id
                )
                if task.active_turn_id:
                    self.tasks.cancel_turn(task_id, task.active_turn_id)

    def advance(self, run: CanvasRun) -> None:
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

    def _advance_step(self, run: CanvasRun, index: int) -> None:
        """Dispatch agent/parallel/condition tasks, or pause at an approval."""
        step, state = (
            CanvasPlan.model_validate(run.plan).steps[index],
            run.states[index],
        )
        if step.type == "appr":
            state.update(
                {"status": "awaiting_approval", "started_at": utc_now().isoformat()}
            )
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
        outputs: list[str] = []
        for task in tasks:
            messages = self.tasks.messages.list_task_records_page(
                task.task_id,
                run.org_id,
                run.workspace_id,
                cursor_created_at=None,
                cursor_record_id=None,
                limit=100,
            )
            outputs.extend(
                m.content for m in reversed(messages) if str(m.role) == "assistant"
            )
        self._collect_artifacts(run, tasks)
        output = "\n\n".join(outputs)
        if step.type == "cond":
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
            {
                "status": "succeeded",
                "finished_at": utc_now().isoformat(),
                "output": output[-32000:],
            }
        )

    def _collect_artifacts(self, run: CanvasRun, tasks: list[Task]) -> None:
        """Collect only artifacts produced by the run's actual child turns."""
        if self.artifacts:
            known = {a["artifact_id"] for a in run.artifacts}
            for task in tasks:
                turn = self.tasks.turns.get_scoped(
                    task.latest_turn_id or "", run.org_id, run.workspace_id
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

    def _dispatch_step(
        self, run: CanvasRun, index: int, step: CanvasStep, state: CanvasStepState
    ) -> None:
        """Create idempotent child tasks pinned to the selected agent versions."""
        agent_ids = step.a
        if step.type == "cond":
            agent_ids = next(
                (
                    s.a[:1]
                    for s in reversed(CanvasPlan.model_validate(run.plan).steps[:index])
                    if s.a
                ),
                [],
            )
        if not agent_ids:
            self._fail(run, index, "A condition requires an earlier agent result.")
            return
        prior = "\n\n".join(str(s.get("output", "")) for s in run.states[:index])[
            -16000:
        ]
        instruction = (
            f"{run.instructions}\n\nStep: {step.t}\n\n"
            f"Previous step results:\n{prior}\n\nSelected context:\n"
            + "\n".join(run.context)
        )
        if step.type == "cond":
            instruction = (
                f"Evaluate this condition against the results below: {step.t}\n"
                "Return only true or false. Do not take any action or use tools.\n"
                f"{prior}"
            )
        for member, agent_id in enumerate(agent_ids):
            task = self.tasks.create_task(
                agent_id=agent_id,
                title=step.t[:160],
                initial_message=instruction[:32000],
                idempotency_key=f"canvas:{run.run_id}:{index}:{member}:{state['attempt']}",
                workflow_id=run.workflow_id,
                pinned_version_id=run.agent_versions.get(agent_id),
                condition_only=step.type == "cond",
            )
            state["tasks"].append(task.task_id)
        state.update({"status": "running", "started_at": utc_now().isoformat()})
        return

    def _fail(self, run: CanvasRun, index: int, error: str) -> None:
        """Apply stop, two retries, or skip without losing earlier results."""
        step, state = (
            CanvasPlan.model_validate(run.plan).steps[index],
            run.states[index],
        )
        self._cancel_tasks([state], run)
        if step.fail == "retry" and state["attempt"] < 3:
            state.update(
                {"status": "queued", "tasks": [], "attempt": state["attempt"] + 1}
            )
        elif step.fail == "skip":
            state.update(
                {
                    "status": "skipped",
                    "output": error,
                    "finished_at": utc_now().isoformat(),
                }
            )
        else:
            state.update(
                {
                    "status": "failed",
                    "output": error,
                    "finished_at": utc_now().isoformat(),
                }
            )
            run.status, run.error, run.finished_at = "failed", error, utc_now()
            for later in run.states[index + 1 :]:
                later["status"] = "skipped"
