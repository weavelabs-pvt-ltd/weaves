"""Shared serialization helpers for identity HTTP responses."""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

DumpModel = Callable[[BaseModel], dict[str, Any]]


def credential_view(credential: Any) -> dict[str, Any]:
    """Serialize credential metadata without exposing its token digest."""
    return {
        "id": credential.id,
        "principal_id": credential.principal_id,
        "status": credential.status.value,
        "expires_at": (
            credential.expires_at.isoformat() if credential.expires_at else None
        ),
        "revoked_at": (
            credential.revoked_at.isoformat() if credential.revoked_at else None
        ),
        "created_at": credential.created_at.isoformat(),
        "updated_at": credential.updated_at.isoformat(),
    }


def invitation_view(invitation: Any, dump: DumpModel) -> dict[str, Any]:
    """Serialize invitation data without exposing its token digest."""
    result = dump(invitation)
    result.pop("token_digest", None)
    return result
