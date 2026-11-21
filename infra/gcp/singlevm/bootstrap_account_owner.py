r"""Operator-only: create the first account owner and its organization.

Run on the VM with database owner access (it opens the owner connection), for
example from the backend image:

    docker compose exec -T product-api python - \
        --email owner@example.com --organization-name "Weaves" \\
        < bootstrap_account_owner.py

Idempotent: it creates the Account, a VERIFIED email identity, the organization
(through ``provision_organization``, with an ACTIVE owner and no API
credential) and the membership. Running it again changes nothing. It never
prints or logs a secret and is not reachable over HTTP.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from typing import Any

from apps.cli.operator_runtime import emit, open_owner_runtime
from modules.auth.account_organization_input import AccountOrganizationCommand
from shared.request_context import system_scope


def bootstrap(runtime: Any, email: str, organization_name: str) -> dict[str, Any]:
    """Ensure the account, its verified email, organization and membership."""
    normalized = email.strip().casefold()
    if "@" not in normalized or len(normalized) > 320:
        raise ValueError("--email must be a valid email address")
    command = AccountOrganizationCommand.build(
        name=organization_name, owner_email=normalized
    )
    key = (
        "operator-bootstrap:"
        + hashlib.sha256(command.fingerprint().encode()).hexdigest()
    )
    with system_scope("account"):
        account = runtime.account_sign_in_service.find_or_create_account(normalized)
        existing = _existing_organization_id(runtime, account.id, command.name)
        if existing is not None:
            result = {"account_id": account.id, "organization_id": existing}
            result["created"] = False
        else:
            created = runtime.account_organization_service.create(
                account, command, key, None
            )
            result = {
                "account_id": account.id,
                "organization_id": created.organization.id,
                "created": created.created,
            }
    emit("operator.bootstrap_account_owner", **result)
    return result


def _existing_organization_id(runtime: Any, account_id: str, name: str) -> str | None:
    """Return the organization of that name the account already belongs to."""
    for membership in runtime.memberships.list_by_field("account_id", account_id):
        if runtime.organizations.get(membership.org_id).name == name:
            return str(membership.org_id)
    return None


def run(argv: list[str], runtime: Any | None = None) -> int:
    """Parse arguments, bootstrap, and return a process exit status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--organization-name", required=True)
    args = parser.parse_args(argv)
    owned = runtime is None
    active = open_owner_runtime() if runtime is None else runtime
    try:
        bootstrap(active, args.email, args.organization_name)
    except (ValueError, PermissionError) as exc:
        emit("operator.bootstrap_account_owner.refused", reason=str(exc))
        return 2
    finally:
        if owned:
            active.close()
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
