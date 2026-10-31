"""Authenticated tenant identity for one API request or worker call."""

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestIdentity:
    principal_id: str
    org_id: str
    workspace_id: str


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
    return set_request_identity(principal_id, "local-org", "local-workspace")


def reset_request_principal(token: Token[RequestIdentity | None]) -> None:
    """Restore the prior request identity after the API request completes."""
    _request_identity.reset(token)


def effective_principal_id(default: str) -> str:
    """Use the authenticated request identity, retaining local-call defaults."""
    identity = _request_identity.get()
    return identity.principal_id if identity is not None else default


def effective_org_id(default: str = "local-org") -> str:
    identity = _request_identity.get()
    return identity.org_id if identity is not None else default


def effective_workspace_id(default: str = "local-workspace") -> str:
    identity = _request_identity.get()
    return identity.workspace_id if identity is not None else default
