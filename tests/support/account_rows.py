"""Build account sign-in state and session rows with chosen expiry for hygiene tests."""

from __future__ import annotations

import hashlib
from datetime import datetime

from contracts.v1 import AccountOAuthState, AccountSession
from shared.clock import new_id, utc_now


def state_row(label: str, expires_at: datetime) -> AccountOAuthState:
    """One Google sign-in state, distinguishable by label, expiring as given."""
    now = utc_now()
    digest = hashlib.sha256(label.encode()).hexdigest()
    return AccountOAuthState(
        id=new_id(),
        state_digest=digest,
        binding_digest=digest,
        nonce="n" * 24,
        code_verifier="v" * 43,
        return_to=f"/{label}",
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )


def session_row(
    account_id: str, expires_at: datetime, revoked_at: datetime | None = None
) -> AccountSession:
    """One session for an account, optionally already revoked."""
    now = utc_now()
    return AccountSession(
        id=new_id(),
        account_id=account_id,
        token_digest=new_id().replace("-", "").ljust(64, "0")[:64],
        expires_at=expires_at,
        revoked_at=revoked_at,
        created_at=now,
        updated_at=now,
    )
