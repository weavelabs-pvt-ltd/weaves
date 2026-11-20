"""Authenticated tenant identity for one API request or worker call."""

from contextvars import ContextVar, Token
from dataclasses import dataclass

from shared.errors import TenantContextRequired


@dataclass(frozen=True)
class RequestIdentity:
    principal_id: str
    org_id: str
    workspace_id: str


_require_context = False


def require_tenant_context(enabled: bool = True) -> None:
    """Make missing tenant context an error instead of selecting local defaults.

    Production processes enable this at startup. Development and test runtimes
    keep their explicit local bootstrap scope.
    """
    global _require_context
    _require_context = enabled


def tenant_context_required() -> bool:
    """Report whether this process refuses implicit local tenant scope."""
    return _require_context


_request_identity: ContextVar[RequestIdentity | None] = ContextVar(
    "weaves_request_identity", default=None
)


def set_request_identity(
    principal_id: str, org_id: str, workspace_id: str
) -> Token[RequestIdentity | None]:
    """Bind the principal and its authorized tenant scope for one request."""
    return _request_identity.set(
        RequestIdentity(
            principal_id=principal_id, org_id=org_id, workspace_id=workspace_id
        )
    )


def set_request_principal(principal_id: str) -> Token[RequestIdentity | None]:
    """Compatibility helper for local callers that rely on seeded scope."""
    if _require_context:
        raise TenantContextRequired("tenant context is required in this process")
    return set_request_identity(principal_id, "local-org", "local-workspace")


def reset_request_principal(token: Token[RequestIdentity | None]) -> None:
    """Restore the prior request identity after the API request completes."""
    _request_identity.reset(token)


def effective_principal_id(default: str) -> str:
    """Use the authenticated request identity, retaining local-call defaults."""
    identity = _request_identity.get()
    if identity is None and _require_context:
        raise TenantContextRequired("tenant context is required in this process")
    return identity.principal_id if identity is not None else default


def effective_org_id(default: str = "local-org") -> str:
    identity = _request_identity.get()
    if identity is None and _require_context:
        raise TenantContextRequired("tenant context is required in this process")
    return identity.org_id if identity is not None else default


def effective_workspace_id(default: str = "local-workspace") -> str:
    identity = _request_identity.get()
    if identity is None and _require_context:
        raise TenantContextRequired("tenant context is required in this process")
    return identity.workspace_id if identity is not None else default
