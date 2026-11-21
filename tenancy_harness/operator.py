"""Operator-only account bearer sessions for controls HTTP cannot mint.

Bearer account sessions are never issued over HTTP. Like the break-glass script
in ``infra/gcp/singlevm``, the fixture opens the runtime on the disposable
database and issues one for an existing account. Fixtures only.
"""

from datetime import timedelta

from apps.runtime_wiring import open_process_runtime
from shared.request_context import system_scope


def operator_bearer(email, minutes=30):
    """Return a bearer header for the account owning ``email``, or None if absent."""
    runtime = open_process_runtime(process="cli")
    try:
        service = runtime.account_sign_in_service
        with system_scope("account"):
            account = service.find_account(email)
            if account is None:
                return None
            _, token = service.sessions.issue(
                account, "bearer", timedelta(minutes=minutes)
            )
    finally:
        runtime.close()
    return {"Authorization": "Bearer " + token}
