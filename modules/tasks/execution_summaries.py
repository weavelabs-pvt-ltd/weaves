"""Build task execution summaries from durable turns and runs."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import AgentRun, Task, TaskTurn
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class TaskExecutionSummaries:
    """Read execution metadata without exposing private task records."""

    def __init__(
        self,
        *,
        turns: Repository[TaskTurn],
        agent_runs: Repository[AgentRun] | None,
        authorize_view: Callable[[], None],
    ) -> None:
        """Bind repositories and the task visibility policy."""
        self._turns = turns
        self._agent_runs = agent_runs
        self._authorize_view = authorize_view

    def build(self, tasks: tuple[Task, ...]) -> dict[str, dict[str, Any]]:
        """Expose metering and sources for visible tasks without raw inputs."""
        self._authorize_view()
        org, workspace = effective_org_id(), effective_workspace_id()
        if any(task.org_id != org or task.workspace_id != workspace for task in tasks):
            raise PermissionError("Task execution metadata is workspace scoped")
        actor = effective_principal_id("local-developer")
        if any(
            task.audience.value == "private"
            and task.audience_owner_principal_id != actor
            for task in tasks
        ):
            raise PermissionError("Task execution metadata is private to its owner")
        turns = self._visible_turns(tasks, org, workspace)
        runs = self._visible_runs(turns, org, workspace)
        return {task.task_id: self._summary(task, turns, runs) for task in tasks}

    def _visible_turns(
        self, tasks: tuple[Task, ...], org: str, workspace: str
    ) -> list[TaskTurn]:
        """Load turns only for the requested visible tasks."""
        task_ids = {task.task_id for task in tasks}
        return [
            turn
            for turn in self._turns.list_scoped(org, workspace)
            if turn.task_id in task_ids
        ]

    def _visible_runs(
        self, turns: list[TaskTurn], org: str, workspace: str
    ) -> dict[str, AgentRun]:
        """Load agent runs referenced by the selected turns."""
        run_ids = {turn.agent_run_id for turn in turns if turn.agent_run_id}
        if self._agent_runs is None:
            return {}
        return {
            run.agent_run_id: run
            for run in self._agent_runs.list_scoped(org, workspace)
            if run.agent_run_id in run_ids
        }

    @staticmethod
    def _summary(
        task: Task, turns: list[TaskTurn], runs: dict[str, AgentRun]
    ) -> dict[str, Any]:
        """Aggregate turn status, cost and sources for one task."""
        history = sorted(
            (turn for turn in turns if turn.task_id == task.task_id),
            key=lambda turn: turn.created_at,
        )
        rows = [TaskExecutionSummaries._turn_row(turn, runs) for turn in history]
        latest = next(
            (turn for turn in history if turn.turn_id == task.latest_turn_id), None
        )
        latest_run = (
            runs.get(latest.agent_run_id)
            if latest and latest.agent_run_id is not None
            else None
        )
        costs = [
            Decimal(row["estimated_cost_usd"])
            for row in rows
            if row["estimated_cost_usd"] is not None
        ]
        return {
            "estimated_cost_usd": str(sum(costs)) if costs else None,
            "started_at": latest.started_at if latest else None,
            "finished_at": latest.finished_at if latest else None,
            "agent_run_id": latest.agent_run_id if latest else None,
            "source_ids": (latest_run.output_summary or {}).get("source_ids", [])
            if latest_run
            else [],
            "turns": rows,
        }

    @staticmethod
    def _turn_row(turn: TaskTurn, runs: dict[str, AgentRun]) -> dict[str, Any]:
        """Project one turn and its usage cost into a public summary row."""
        run = runs.get(turn.agent_run_id) if turn.agent_run_id is not None else None
        return {
            "turn_id": turn.turn_id,
            "agent_run_id": turn.agent_run_id,
            "status": str(turn.status),
            "created_at": turn.created_at,
            "started_at": turn.started_at,
            "finished_at": turn.finished_at,
            "estimated_cost_usd": str(run.estimated_cost_usd)
            if run and run.estimated_cost_usd is not None
            else None,
        }
