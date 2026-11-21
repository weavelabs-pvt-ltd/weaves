r"""Operator-only break-glass: issue an account bearer session for an account.

Run on the VM with database owner access, for example from the backend image:

    docker compose exec -T product-api python - \
        --email owner@example.com --minutes 60 < issue_account_session.py

The token is printed once on stdout and is never logged; the structured log line
on stderr carries only identifiers. Unknown and disabled accounts are refused.
This is not reachable over HTTP.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from typing import Any

from apps.cli.operator_runtime import emit, open_owner_runtime
from shared.request_context import system_scope

MAX_MINUTES = 1440


def issue(runtime: Any, email: str, minutes: int) -> str:
    """Return a bearer token for an existing active account, else raise."""
    if not 1 <= minutes <= MAX_MINUTES:
        raise ValueError(f"--minutes must be between 1 and {MAX_MINUTES}")
    service = runtime.account_sign_in_service
    with system_scope("account"):
        account = service.find_account(email)
        if account is None:
            raise PermissionError("no active account for that email")
        session, token = service.sessions.issue(
            account, "bearer", timedelta(minutes=minutes)
        )
    emit(
        "operator.issue_account_session",
        account_id=account.id,
        session_id=session.id,
        expires_at=session.expires_at.isoformat(),
    )
    return str(token)


def run(argv: list[str], runtime: Any | None = None) -> int:
    """Parse arguments, issue the session and print its token to stdout."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--minutes", type=int, default=60)
    args = parser.parse_args(argv)
    owned = runtime is None
    active = open_owner_runtime() if runtime is None else runtime
    try:
        token = issue(active, args.email, args.minutes)
    except (ValueError, PermissionError) as exc:
        emit("operator.issue_account_session.refused", reason=str(exc))
        return 2
    finally:
        if owned:
            active.close()
    sys.stdout.write(token + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
