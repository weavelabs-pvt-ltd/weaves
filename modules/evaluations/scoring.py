"""Contracts and scoring helpers for repeatable agent evaluations."""

from decimal import Decimal

from contracts.v1.evaluations import (
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationCheck,
    EvaluationReport,
    EvaluationSuite,
)
from contracts.v1.executions import RunStatus


def score_evaluation_case(
    case: EvaluationCase,
    *,
    status: str | None,
    run_id: str | None,
    summaries: tuple[str, ...] = (),
    cited_sources: tuple[str, ...] = (),
    tool_call_count: int = 0,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    estimated_cost_usd: Decimal | None = None,
    duration_ms: int | None = None,
    request_error: str | None = None,
) -> EvaluationCaseResult:
    """Score a completed API response without retaining its full model output."""
    combined_output = "\n".join(summaries).casefold()
    checks = [_status_check(status, request_error)]
    checks.extend(_content_checks(case, combined_output))
    checks.append(_source_check(case.required_sources, cited_sources))
    checks.append(_tool_check(tool_call_count, case.minimum_tool_calls))
    return EvaluationCaseResult(
        case_id=case.case_id,
        passed=all(check.passed for check in checks),
        checks=tuple(checks),
        run_id=run_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost_usd,
        duration_ms=duration_ms,
    )


def _status_check(status: str | None, request_error: str | None) -> EvaluationCheck:
    """Create the check for successful agent run completion."""
    succeeded = status == RunStatus.SUCCEEDED.value
    detail = (
        "Run completed successfully."
        if succeeded
        else request_error
        or f"Expected a succeeded run; received {status or 'no response'}."
    )
    return EvaluationCheck(name="run_succeeded", passed=succeeded, detail=detail)


def _content_checks(case: EvaluationCase, output: str) -> list[EvaluationCheck]:
    """Check required and forbidden phrases against bounded artifact text."""
    required = [
        EvaluationCheck(
            name=f"contains:{phrase[:64]}",
            passed=phrase.casefold() in output,
            detail=(
                f"Expected phrase was present: {phrase[:120]}"
                if phrase.casefold() in output
                else f"Expected phrase was missing: {phrase[:120]}"
            ),
        )
        for phrase in case.must_contain
    ]
    forbidden = [
        EvaluationCheck(
            name=f"excludes:{phrase[:64]}",
            passed=phrase.casefold() not in output,
            detail=(
                f"Forbidden phrase was absent: {phrase[:120]}"
                if phrase.casefold() not in output
                else f"Forbidden phrase was present: {phrase[:120]}"
            ),
        )
        for phrase in case.must_not_contain
    ]
    return [*required, *forbidden]


def _source_check(
    required_sources: tuple[str, ...], cited_sources: tuple[str, ...]
) -> EvaluationCheck:
    """Check that each required source identifier appears in the citations."""
    missing = tuple(
        source for source in required_sources if source not in cited_sources
    )
    detail = (
        "All required sources were cited."
        if not missing
        else "Missing cited sources: "
        + ", ".join(source[:64] for source in missing[:5])
    )
    return EvaluationCheck(name="required_sources", passed=not missing, detail=detail)


def _tool_check(observed: int, required: int) -> EvaluationCheck:
    """Check whether the run used enough tool calls."""
    passed = observed >= required
    return EvaluationCheck(
        name="minimum_tool_calls",
        passed=passed,
        detail=f"Observed {observed} tool calls; required at least {required}.",
    )


__all__ = [
    "EvaluationCase",
    "EvaluationCaseResult",
    "EvaluationCheck",
    "EvaluationReport",
    "EvaluationSuite",
    "score_evaluation_case",
]
