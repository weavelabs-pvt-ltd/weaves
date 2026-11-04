"""Evaluation ReportRuntime runtime operations."""

from __future__ import annotations

from contracts.v1 import (
    AgentRun,
    Artifact,
    AuditEvent,
    AuditTargetScope,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationReport,
    EvaluationReportRecord,
    EvaluationSuite,
    Permission,
    RunStatus,
    TaskTurnStatus,
    ToolInvocation,
)
from modules.evaluations.runtime_host import EvaluationRuntimeHost
from modules.evaluations.scoring import score_evaluation_case
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class EvaluationReportRuntimeMixin(EvaluationRuntimeHost):
    """Provide one cohesive set of evaluation runtime operations."""

    def store_evaluation_report(
        self,
        definition: EvaluationSuite,
        report: EvaluationReport,
        requested_by_principal_id: str = "local-developer",
    ) -> EvaluationReportRecord:
        """Store runner results after checking suite and referenced run scope."""
        if not self._is_bootstrapped():
            self.bootstrap()

        actor = effective_principal_id(requested_by_principal_id)

        self.authorize(actor, Permission.AGENTS_RUN)

        suite = self.evaluation_suites.get_scoped(
            report.suite_id, effective_org_id(), effective_workspace_id()
        )

        if definition != suite.definition:
            raise ValueError("evaluation suite changed while the report was running")

        self._validate_report_cases(actor, suite.definition, definition, report)

        timestamp = _now()

        record = EvaluationReportRecord(
            evaluation_id=report.evaluation_id,
            suite_id=report.suite_id,
            agent_id=suite.definition.agent_id,
            definition=definition,
            report=report,
            org_id=suite.org_id,
            workspace_id=suite.workspace_id,
            submitted_by_principal_id=actor,
            created_at=timestamp,
        )

        with self._unit_of_work():
            self.evaluation_reports.create(record)

            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=record.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=record.workspace_id,
                    actor_principal_id=actor,
                    action="evaluation.report.submitted",
                    target_type="evaluation_report",
                    target_id=record.evaluation_id,
                    summary="Evaluation report submitted",
                    request_id=record.evaluation_id,
                    metadata={
                        "suite_id": record.suite_id,
                        "passed_cases": report.passed_cases,
                        "total_cases": report.total_cases,
                    },
                    created_at=timestamp,
                )
            )

        return record

    def _validate_report_cases(
        self,
        actor: str,
        suite_definition: EvaluationSuite,
        definition: EvaluationSuite,
        report: EvaluationReport,
    ) -> None:
        """Validate result coverage, unique runs, and persisted case evidence."""
        result_case_ids = tuple(result.case_id for result in report.results)
        case_ids = {case.case_id for case in definition.cases}
        if (
            len(set(result_case_ids)) != len(result_case_ids)
            or set(result_case_ids) != case_ids
        ):
            raise ValueError("report results must match the suite cases exactly")

        run_ids = tuple(
            result.run_id for result in report.results if result.run_id is not None
        )
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("each evaluation case must reference a distinct run")

        cases = {case.case_id: case for case in definition.cases}
        agent_runs = {
            run.run_id: run
            for run in self.agent_runs.list_scoped(
                effective_org_id(), effective_workspace_id()
            )
            if run.agent_id == suite_definition.agent_id
        }
        artifacts: dict[str, list[Artifact]] = {}
        for artifact in self.artifacts.list_scoped(
            effective_org_id(), effective_workspace_id()
        ):
            artifacts.setdefault(artifact.run_id, []).append(artifact)
        invocations: dict[str, list[ToolInvocation]] = {}
        for invocation in self.tool_invocations.list_scoped(
            effective_org_id(), effective_workspace_id()
        ):
            invocations.setdefault(invocation.run_id, []).append(invocation)

        for result in report.results:
            self._validate_report_result(
                actor,
                suite_definition.agent_id,
                cases[result.case_id],
                result,
                agent_runs,
                artifacts,
                invocations,
            )

    def _validate_report_result(
        self,
        actor: str,
        suite_agent_id: str,
        case: EvaluationCase,
        result: EvaluationCaseResult,
        agent_runs: dict[str, AgentRun],
        artifacts: dict[str, list[Artifact]],
        invocations: dict[str, list[ToolInvocation]],
    ) -> None:
        """Compare one result with the saved run and artifact evidence."""
        if result.run_id is None:
            if result.passed:
                raise ValueError("a passing result must reference a completed run")
            return

        try:
            run = self.runs.get_scoped(
                result.run_id, effective_org_id(), effective_workspace_id()
            )
        except KeyError:
            return self._validate_task_report_result(
                actor,
                suite_agent_id,
                case,
                result,
                agent_runs,
                artifacts,
                invocations,
            )
        if run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
            raise ValueError("evaluation reports require completed runs")
        if run.requested_by_principal_id != actor:
            self.authorize(actor, Permission.USERS_MANAGE)

        agent_run = agent_runs.get(result.run_id)
        if agent_run is None:
            raise ValueError(
                "evaluation results may reference only runs for the suite agent"
            )
        run_artifacts = artifacts.get(result.run_id, [])
        run_invocations = invocations.get(result.run_id, [])
        expected = score_evaluation_case(
            case,
            status=run.status.value,
            run_id=result.run_id,
            summaries=tuple(item.summary for item in run_artifacts),
            cited_sources=tuple(
                provenance.source_ref
                for item in run_artifacts
                for provenance in item.provenance
            ),
            tool_call_count=len(run_invocations),
            input_tokens=agent_run.input_tokens,
            output_tokens=agent_run.output_tokens,
            estimated_cost_usd=agent_run.estimated_cost_usd,
            duration_ms=result.duration_ms,
        )
        if _result_differs(result, expected):
            raise ValueError(
                "submitted result does not match the persisted run evidence"
            )

    def _validate_task_report_result(
        self,
        actor: str,
        suite_agent_id: str,
        case: EvaluationCase,
        result: EvaluationCaseResult,
        agent_runs: dict[str, AgentRun],
        artifacts: dict[str, list[Artifact]],
        invocations: dict[str, list[ToolInvocation]],
    ) -> None:
        """Validate evidence from a completed task turn using the task ID."""
        task = self.tasks.get_scoped(
            result.run_id or "", effective_org_id(), effective_workspace_id()
        )
        if task.agent_id != suite_agent_id:
            raise ValueError(
                "evaluation results may reference only tasks for the suite agent"
            )
        if task.latest_turn_id is None:
            raise ValueError("evaluation reports require completed task turns")
        turn = self.task_turns.get_scoped(
            task.latest_turn_id, task.org_id, task.workspace_id
        )
        if turn.status is not TaskTurnStatus.SUCCEEDED or turn.agent_run_id is None:
            raise ValueError("evaluation reports require completed task turns")
        if task.requested_by_principal_id != actor:
            self.authorize(actor, Permission.USERS_MANAGE)
        agent_run = next(
            (
                item
                for item in agent_runs.values()
                if item.agent_run_id == turn.agent_run_id
            ),
            None,
        )
        if agent_run is None:
            raise ValueError("evaluation task is missing its saved agent evidence")
        run_id = agent_run.run_id
        expected = score_evaluation_case(
            case,
            status=turn.status.value,
            run_id=task.task_id,
            summaries=tuple(item.summary for item in artifacts.get(run_id, [])),
            cited_sources=tuple(
                provenance.source_ref
                for item in artifacts.get(run_id, [])
                for provenance in item.provenance
            ),
            tool_call_count=len(invocations.get(run_id, [])),
            input_tokens=agent_run.input_tokens,
            output_tokens=agent_run.output_tokens,
            estimated_cost_usd=agent_run.estimated_cost_usd,
            duration_ms=result.duration_ms,
        )
        if _result_differs(result, expected):
            raise ValueError(
                "submitted result does not match the persisted task evidence"
            )

    def list_evaluation_reports(self) -> tuple[EvaluationReportRecord, ...]:
        """Return recent reports visible in the active workspace."""
        if not self._is_bootstrapped():
            self.bootstrap()

        self.authorize(
            effective_principal_id("local-developer"), Permission.ARTIFACTS_READ
        )

        records = self.evaluation_reports.list_scoped(
            effective_org_id(), effective_workspace_id()
        )

        return tuple(sorted(records, key=lambda item: item.created_at, reverse=True))

    def get_evaluation_report(self, evaluation_id: str) -> EvaluationReportRecord:
        """Load one report from the active workspace."""
        if not self._is_bootstrapped():
            self.bootstrap()

        self.authorize(
            effective_principal_id("local-developer"), Permission.ARTIFACTS_READ
        )

        return self.evaluation_reports.get_scoped(
            evaluation_id, effective_org_id(), effective_workspace_id()
        )


def _result_differs(
    submitted: EvaluationCaseResult, expected: EvaluationCaseResult
) -> bool:
    """Compare submitted score fields against persisted run evidence."""
    return any(
        (
            submitted.passed != expected.passed,
            submitted.checks != expected.checks,
            submitted.input_tokens != expected.input_tokens,
            submitted.output_tokens != expected.output_tokens,
            submitted.estimated_cost_usd != expected.estimated_cost_usd,
        )
    )
