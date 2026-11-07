"""Mount product capability routes under /api/v0."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from apps.api.route_services import ApiRouteServices
from modules.agents.http.routes import register_agent_routes
from modules.auth.http.routes import register_identity_routes
from modules.evaluations.http.routes import register_evaluation_routes
from modules.governance.http.audit_routes import register_audit_routes
from modules.governance.http.routes import register_governance_routes
from modules.integrations.http.oauth import register_oauth_routes
from modules.integrations.http.routes import register_plugin_routes
from modules.models.http.routes import register_model_routes
from modules.skills.http.routes import register_skill_routes
from modules.tasks.http.routes import register_task_routes
from modules.workflows.http.routes import register_workflow_routes
from modules.workflows.http.studio_routes import register_studio_routes


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def register_v0_routes(app: FastAPI, services: ApiRouteServices) -> None:
    """Register the local v0 HTTP surface. Route functions stay transport-only."""

    @app.get("/", include_in_schema=False)
    def api_root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get("/api/v0/health")
    def health() -> dict[str, str]:
        try:
            services.check_storage()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="storage unavailable") from exc
        return {"status": "ok", "storage": services.storage_mode}

    dump: Callable[[BaseModel], dict[str, Any]] = _dump
    register_identity_routes(
        app,
        services.organization,
        services.workspace_access,
        services.user_administration,
        services.authentication,
        services.credential,
        dump,
    )
    register_model_routes(app, services.models, dump)
    register_plugin_routes(
        app,
        services.integration_admin,
        services.integration_connection,
        dump,
    )
    register_oauth_routes(app, services.oauth)
    register_agent_routes(app, services.agents, services.integration_admin, dump)
    register_evaluation_routes(
        app, services.evaluations, services.storage_mode == "postgres", dump
    )
    register_workflow_routes(
        app,
        services.workflow_definition,
        services.workflow_automation,
        services.storage_mode == "postgres",
        dump,
    )
    register_studio_routes(app, services.workflow_studio, services.workflow_automation)
    register_task_routes(app, services.tasks, dump)
    register_skill_routes(app, services.skills, dump)
    register_audit_routes(app, services.governance, dump)
    register_governance_routes(app, services.governance, dump)
