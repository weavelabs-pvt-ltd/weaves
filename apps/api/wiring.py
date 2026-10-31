"""Construct the dependencies for the API process."""

from apps.api.authentication import AuthenticationServices
from apps.api.route_services import build_api_route_services
from apps.api.services import ApiProcessServices
from apps.platform_runtime import LocalPlatformRuntime
from apps.runtime_wiring import open_process_runtime


def build_platform_runtime() -> LocalPlatformRuntime:
    """Open API-owned pools and compose the platform runtime."""
    return open_process_runtime(process="api")


def build_api_process_services(
    runtime: LocalPlatformRuntime | None = None,
) -> ApiProcessServices:
    """Build API-facing services without exposing the process runtime."""
    active_runtime = runtime or build_platform_runtime()
    try:
        return ApiProcessServices(
            routes=build_api_route_services(active_runtime),
            authentication=AuthenticationServices(
                authentication=active_runtime.authentication_service,
                credentials=active_runtime.credential_service,
                organizations=active_runtime.organization_service,
                bearer_required=active_runtime.storage_mode == "postgres",
            ),
            bootstrap=active_runtime.bootstrap,
            purge_expired_memory=(
                active_runtime.memory_service.purge_expired_memory_items
            ),
            close=active_runtime.close,
        )
    except Exception:
        active_runtime.close()
        raise
