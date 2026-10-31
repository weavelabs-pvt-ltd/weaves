"""Versioned contracts for representative agent evaluations."""

from decimal import Decimal

from pydantic import (
    AwareDatetime,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from contracts.v1.base import (
    ImmutableWorkspaceVersionContract,
    MutableWorkspaceScopedContract,
    OpaqueId,
    ProductContract,
    StrEnum,
)
from contracts.v1.executions import ErrorSummary


class EvaluationCase(ProductContract):
    """One task and its deterministic acceptance checks."""

    case_id: str = Field(min_length=1, max_length=96, pattern=r"^[a-zA-Z0-9_.-]+$")
    task: str = Field(min_length=1, max_length=12_000)
    must_contain: tuple[str, ...] = Field(default=(), max_length=50)
    must_not_contain: tuple[str, ...] = Field(default=(), max_length=50)
    required_sources: tuple[str, ...] = Field(default=(), max_length=50)
    minimum_tool_calls: int = Field(default=0, ge=0, le=100)

    @field_validator("task")
    @classmethod
    def task_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task must not be blank")
        return value

    @field_validator("must_contain", "must_not_contain", "required_sources")
    @classmethod
    def assertions_are_bounded_nonblank(
        cls, values: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("evaluation assertions must not be blank")
        limit = 256 if info.field_name == "required_sources" else 500
        if any(len(value) > limit for value in normalized):
            raise ValueError(
                f"evaluation assertions must not exceed {limit} characters"
            )
        return normalized

    @model_validator(mode="after")
    def assertions_are_consistent(self) -> "EvaluationCase":
        required = {phrase.casefold() for phrase in self.must_contain}
        forbidden = {phrase.casefold() for phrase in self.must_not_contain}
        if required.intersection(forbidden):
            raise ValueError("a phrase cannot be both required and forbidden")
        if len(self.required_sources) != len(set(self.required_sources)):
            raise ValueError("required source references must be unique")
        return self


class EvaluationSuite(ProductContract):
    """Versioned, portable set of representative agent tasks."""

    suite_id: str = Field(min_length=1, max_length=96, pattern=r"^[a-zA-Z0-9_.-]+$")
    agent_id: str = Field(min_length=1, max_length=128)
    cases: tuple[EvaluationCase, ...] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_case_ids(self) -> "EvaluationSuite":
        case_ids = tuple(case.case_id for case in self.cases)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation case IDs must be unique")
        return self


class EvaluationCheck(ProductContract):
    name: str = Field(min_length=1, max_length=96)
    passed: bool
    detail: str = Field(min_length=1, max_length=500)


class EvaluationCaseResult(ProductContract):
    case_id: str
    passed: bool
    checks: tuple[EvaluationCheck, ...]
    run_id: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: Decimal | None = Field(
        default=None, ge=Decimal("0"), max_digits=18, decimal_places=12
    )
    duration_ms: int | None = Field(default=None, ge=0)


class EvaluationReport(ProductContract):
    suite_id: str
    evaluation_id: str
    passed_cases: int = Field(ge=0)
    total_cases: int = Field(ge=1)
    pass_rate: float = Field(ge=0.0, le=1.0)
    results: tuple[EvaluationCaseResult, ...]

    @model_validator(mode="after")
    def counts_match_results(self) -> "EvaluationReport":
        if len(self.results) != self.total_cases:
            raise ValueError("total_cases must match the number of results")
        if sum(result.passed for result in self.results) != self.passed_cases:
            raise ValueError("passed_cases must match the passed results")
        if self.pass_rate != self.passed_cases / self.total_cases:
            raise ValueError("pass_rate must match passed_cases and total_cases")
        return self


class EvaluationExecutionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvaluationExecution(MutableWorkspaceScopedContract):
    """Durable progress for a queued evaluation suite execution."""

    evaluation_id: OpaqueId
    suite_id: OpaqueId
    agent_id: OpaqueId
    execution_job_id: OpaqueId
    requested_by_principal_id: OpaqueId
    definition: EvaluationSuite
    status: EvaluationExecutionStatus
    results: tuple[EvaluationCaseResult, ...] = ()
    error: ErrorSummary | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def execution_fields_match_status(self) -> "EvaluationExecution":
        if self.definition.suite_id != self.suite_id:
            raise ValueError("suite_id must match the saved definition")
        if self.definition.agent_id != self.agent_id:
            raise ValueError("agent_id must match the saved definition")
        result_ids = tuple(result.case_id for result in self.results)
        case_ids = {case.case_id for case in self.definition.cases}
        if len(set(result_ids)) != len(result_ids) or not set(result_ids) <= case_ids:
            raise ValueError("execution results must uniquely match suite cases")
        if self.status is EvaluationExecutionStatus.QUEUED:
            if (
                self.started_at is not None
                or self.finished_at is not None
                or self.error
            ):
                raise ValueError("queued evaluations cannot have execution state")
            if self.results:
                raise ValueError("queued evaluations cannot have case results")
        elif self.status in {
            EvaluationExecutionStatus.RUNNING,
            EvaluationExecutionStatus.CANCEL_REQUESTED,
        }:
            if self.started_at is None or self.finished_at is not None or self.error:
                raise ValueError("active evaluations require only a start timestamp")
        elif self.status is EvaluationExecutionStatus.SUCCEEDED:
            if self.started_at is None or self.finished_at is None or self.error:
                raise ValueError("successful evaluations require terminal timestamps")
            if set(result_ids) != case_ids:
                raise ValueError("successful evaluations require every case result")
        elif self.status is EvaluationExecutionStatus.FAILED:
            if (
                self.started_at is None
                or self.finished_at is None
                or self.error is None
            ):
                raise ValueError("failed evaluations require timestamps and an error")
        elif self.status is EvaluationExecutionStatus.CANCELLED:
            if self.finished_at is None or self.error is not None:
                raise ValueError("cancelled evaluations require a finish time only")
            if self.started_at is None and self.results:
                raise ValueError("queued evaluations cannot have case results")
        return self


class EvaluationSuiteRecord(MutableWorkspaceScopedContract):
    """Workspace-owned, editable evaluation definition."""

    suite_id: OpaqueId
    definition: EvaluationSuite
    created_by_principal_id: OpaqueId
    updated_by_principal_id: OpaqueId

    @model_validator(mode="after")
    def definition_id_matches_record(self) -> "EvaluationSuiteRecord":
        if self.definition.suite_id != self.suite_id:
            raise ValueError("suite_id must match the evaluation definition")
        return self


class EvaluationReportRecord(ImmutableWorkspaceVersionContract):
    """Workspace-scoped record submitted by an evaluation runner."""

    evaluation_id: OpaqueId
    suite_id: OpaqueId
    agent_id: OpaqueId
    definition: EvaluationSuite
    report: EvaluationReport
    submitted_by_principal_id: OpaqueId

    @model_validator(mode="after")
    def report_ids_match_record(self) -> "EvaluationReportRecord":
        if self.report.evaluation_id != self.evaluation_id:
            raise ValueError("evaluation_id must match the report")
        if self.report.suite_id != self.suite_id:
            raise ValueError("suite_id must match the report")
        if self.definition.suite_id != self.suite_id:
            raise ValueError("suite_id must match the saved definition")
        if self.definition.agent_id != self.agent_id:
            raise ValueError("agent_id must match the saved definition")
        expected_cases = {case.case_id for case in self.definition.cases}
        result_cases = tuple(result.case_id for result in self.report.results)
        if (
            len(set(result_cases)) != len(result_cases)
            or set(result_cases) != expected_cases
        ):
            raise ValueError("report results must match the saved definition cases")
        return self


__all__ = [
    "EvaluationCase",
    "EvaluationCaseResult",
    "EvaluationCheck",
    "EvaluationExecution",
    "EvaluationExecutionStatus",
    "EvaluationReport",
    "EvaluationReportRecord",
    "EvaluationSuite",
    "EvaluationSuiteRecord",
]
