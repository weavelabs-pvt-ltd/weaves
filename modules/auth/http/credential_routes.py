"""Identity API route registrations migrated from the v0 API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from modules.auth.credential_service import CredentialService
from modules.auth.http.responses import DumpModel, credential_view


def _register_list_api_credentials(
    app: FastAPI, credential_service: CredentialService, dump: DumpModel
) -> None:
    """Register the list api credentials endpoint."""
    _dump = dump

    @app.get("/api/v0/api-credentials")
    def list_api_credentials() -> list[dict[str, Any]]:
        credentials = credential_service.list_api_credentials()
        return [credential_view(item) for item in credentials]


def _register_revoke_api_credential(
    app: FastAPI, credential_service: CredentialService, dump: DumpModel
) -> None:
    """Register the revoke api credential endpoint."""
    _dump = dump

    @app.delete("/api/v0/api-credentials/{credential_id}")
    def revoke_api_credential(credential_id: str) -> dict[str, Any]:
        try:
            credential = credential_service.revoke_api_credential(credential_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Credential not found") from exc
        return credential_view(credential)


def _register_rotate_api_credential(
    app: FastAPI, credential_service: CredentialService, dump: DumpModel
) -> None:
    """Register the rotate api credential endpoint."""
    _dump = dump

    @app.put("/api/v0/api-credentials/{credential_id}/rotate")
    def rotate_api_credential(credential_id: str) -> dict[str, Any]:
        try:
            credential, token = credential_service.rotate_api_credential(credential_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Credential not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"credential": credential_view(credential), "token": token}


def register_credential_routes(
    app: FastAPI, credential_service: CredentialService, dump: DumpModel
) -> None:
    """Register credential routes."""
    _register_list_api_credentials(app, credential_service, dump)
    _register_revoke_api_credential(app, credential_service, dump)
    _register_rotate_api_credential(app, credential_service, dump)
