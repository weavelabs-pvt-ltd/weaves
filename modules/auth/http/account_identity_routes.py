"""Authenticated route that detaches a login identity from an account."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, Response

from contracts.v1 import Account
from modules.auth.account_identity_removal import LastVerifiedIdentity, remove_identity
from modules.auth.account_sign_in import AccountSignInService
from shared.request_context import system_scoped


def _current_account(request: Request) -> Account:
    """Return the account the middleware authenticated for this request."""
    account = getattr(request.state, "account", None)
    if not isinstance(account, Account):
        raise HTTPException(status_code=401, detail="Account session required")
    return account


def register_account_identity_routes(
    app: FastAPI, service: AccountSignInService
) -> None:
    """Register removal of one identity for the signed-in account."""

    @app.delete("/api/v0/account/identities/{identity_id}", status_code=204)
    @system_scoped("account")
    def delete_identity(identity_id: str, request: Request) -> Response:
        account = _current_account(request)
        try:
            remove_identity(service.identities, account, identity_id)
        except PermissionError as exc:
            raise HTTPException(status_code=404, detail="Identity not found") from exc
        except LastVerifiedIdentity as exc:
            raise HTTPException(
                status_code=409,
                detail="An account must keep at least one verified identity",
            ) from exc
        return Response(status_code=204)
