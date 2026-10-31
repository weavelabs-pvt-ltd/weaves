"""Evaluation suite and report HTTP routes."""

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, ValidationError

from application import IdempotencyConflict
from contracts.v1 import EvaluationReport, EvaluationSuite
from modules.evaluations.service import EvaluationService


class EvaluationSuiteWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]


class EvaluationReportSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]
    report: dict[str, Any]


def _parse_evaluation_suite(payload: dict[str, Any]) -> EvaluationSuite:
    try:
        return EvaluationSuite.model_validate_json(json.dumps(payload))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc


def _parse_evaluation_report(payload: dict[str, Any]) -> EvaluationReport:
    try:
        return EvaluationReport.model_validate_json(json.dumps(payload))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc


def register_evaluation_routes(
    app: FastAPI,
    evaluation_service: EvaluationService,
    durable_execution_enabled: bool,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register evaluation routes on the product API application."""
    _register_evaluation_routes_group_1(
        app, evaluation_service, durable_execution_enabled, dump
    )
    _register_evaluation_routes_group_2(
        app, evaluation_service, durable_execution_enabled, dump
    )
    _register_evaluation_routes_group_3(
        app, evaluation_service, durable_execution_enabled, dump
    )


def _register_evaluation_routes_group_1(
    app: FastAPI,
    evaluation_service: EvaluationService,
    durable_execution_enabled: bool,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register evaluation routes on the product API application."""
    _dump = dump

    def list_evaluation_suites() -> list[dict[str, Any]]:
        try:
            return [_dump(item) for item in evaluation_service.list_evaluation_suites()]
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def create_evaluation_suite(
        request: EvaluationSuiteWriteRequest,
    ) -> dict[str, Any]:
        try:
            definition = _parse_evaluation_suite(request.definition)
            return _dump(evaluation_service.create_evaluation_suite(definition))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Agent not found") from exc
        except ValueError as exc:
            status_code = 409 if "already exists" in str(exc) else 422
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    def get_evaluation_suite(suite_id: str) -> dict[str, Any]:
        try:
            return _dump(evaluation_service.get_evaluation_suite(suite_id))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Evaluation suite not found"
            ) from exc

    def update_evaluation_suite(
        suite_id: str, request: EvaluationSuiteWriteRequest
    ) -> dict[str, Any]:
        try:
            definition = _parse_evaluation_suite(request.definition)
            return _dump(
                evaluation_service.update_evaluation_suite(suite_id, definition)
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Evaluation suite not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def list_evaluation_reports() -> list[dict[str, Any]]:
        try:
            return [
                _dump(item) for item in evaluation_service.list_evaluation_reports()
            ]
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    app.get("/api/v0/evaluation-suites")(list_evaluation_suites)
    app.post("/api/v0/evaluation-suites", status_code=201)(create_evaluation_suite)
    app.get("/api/v0/evaluation-suites/{suite_id}")(get_evaluation_suite)
    app.put("/api/v0/evaluation-suites/{suite_id}")(update_evaluation_suite)
    app.get("/api/v0/evaluation-reports")(list_evaluation_reports)


def _register_evaluation_routes_group_2(
    app: FastAPI,
    evaluation_service: EvaluationService,
    durable_execution_enabled: bool,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register evaluation routes on the product API application."""
    _dump = dump

    def submit_evaluation_report(
        request: EvaluationReportSubmitRequest,
    ) -> dict[str, Any]:
        try:
            definition = _parse_evaluation_suite(request.definition)
            report = _parse_evaluation_report(request.report)
            return _dump(evaluation_service.store_evaluation_report(definition, report))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Evaluation suite or referenced run not found"
            ) from exc
        except ValueError as exc:
            status_code = 409 if "already exists" in str(exc) else 422
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    def get_evaluation_report(evaluation_id: str) -> dict[str, Any]:
        try:
            return _dump(evaluation_service.get_evaluation_report(evaluation_id))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Evaluation report not found"
            ) from exc

    def enqueue_evaluation_run(
        suite_id: str,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=1, max_length=128
        ),
    ) -> dict[str, Any]:
        if not durable_execution_enabled:
            raise HTTPException(
                status_code=503,
                detail="Queued evaluations require PostgreSQL-backed storage",
            )
        try:
            evaluation, replayed = evaluation_service.enqueue_evaluation_run(
                suite_id, idempotency_key=idempotency_key
            )
            return {"evaluation": _dump(evaluation), "replayed": replayed}
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Evaluation suite not found"
            ) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    app.post("/api/v0/evaluation-reports", status_code=201)(submit_evaluation_report)
    app.get("/api/v0/evaluation-reports/{evaluation_id}")(get_evaluation_report)
    app.post("/api/v0/evaluation-suites/{suite_id}/runs", status_code=202)(
        enqueue_evaluation_run
    )


def _register_evaluation_routes_group_3(
    app: FastAPI,
    evaluation_service: EvaluationService,
    durable_execution_enabled: bool,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register evaluation routes on the product API application."""
    _dump = dump

    def list_evaluation_runs(
        limit: int = Query(default=50, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        try:
            return [
                _dump(item)
                for item in evaluation_service.list_evaluation_executions(limit=limit)
            ]
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def get_evaluation_run(evaluation_id: str) -> dict[str, Any]:
        try:
            return _dump(evaluation_service.get_evaluation_execution(evaluation_id))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Evaluation run not found"
            ) from exc

    def cancel_evaluation_run(evaluation_id: str) -> dict[str, Any]:
        try:
            return _dump(evaluation_service.cancel_evaluation_run(evaluation_id))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Evaluation run not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    app.get("/api/v0/evaluation-runs")(list_evaluation_runs)
    app.get("/api/v0/evaluation-runs/{evaluation_id}")(get_evaluation_run)
    app.post("/api/v0/evaluation-runs/{evaluation_id}/cancel")(cancel_evaluation_run)
