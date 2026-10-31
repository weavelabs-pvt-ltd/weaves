"""Workflow, schedule, and webhook API routes."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from modules.workflows.automation_service import WorkflowAutomationService
from modules.workflows.definition_service import WorkflowDefinitionService
from modules.workflows.execution_service import WorkflowExecutionService
from modules.workflows.http.automation_routes import register_automation_routes
from modules.workflows.http.definition_routes import register_definition_routes
from modules.workflows.http.execution_routes import register_execution_routes


def register_workflow_routes(
    app: FastAPI,
    definition_service: WorkflowDefinitionService,
    automation_service: WorkflowAutomationService,
    execution_service: WorkflowExecutionService,
    durable_execution_enabled: bool,
    dump: Callable[[BaseModel], dict[str, Any]],
    run_view: Callable[[Any], dict[str, Any]],
) -> None:
    """Register workflow definitions, automation, and execution endpoints."""
    register_definition_routes(app, definition_service, dump, run_view)
    register_automation_routes(
        app, automation_service, durable_execution_enabled, dump, run_view
    )
    register_execution_routes(app, execution_service, dump, run_view)
