"""Hold API capability dependencies and process lifecycle callbacks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from apps.api.authentication import AuthenticationServices
from apps.api.route_services import ApiRouteServices


@dataclass(frozen=True)
class ApiProcessServices:
    """Expose only the services and lifecycle actions used by the API app."""

    routes: ApiRouteServices
    authentication: AuthenticationServices
    bootstrap: Callable[[], None]
    purge_expired_memory: Callable[[], tuple[str, ...]]
    close: Callable[[], None]
