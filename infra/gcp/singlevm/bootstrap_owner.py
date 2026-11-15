"""Create the initial owner internally after a deliberate fresh database reset."""

from __future__ import annotations

import hashlib
import json
import sys

from apps.runtime_wiring import open_process_runtime


def main() -> None:
    """Accept owner credentials on stdin without opening public onboarding."""
    incoming = json.load(sys.stdin)
    runtime = open_process_runtime(process="cli")
    try:
        if runtime.seed_local_defaults:
            raise RuntimeError("Local example seeding must be disabled")
        runtime.bootstrap()
        if runtime.organizations.list() or runtime.users.list():
            raise RuntimeError("Owner initialization requires an empty database")
        organization, _, _, principal, token = (
            runtime.organization_service.create_organization(
                name="Weaves Personal",
                email=incoming["email"],
                display_name="Shivam Sawarn",
            )
        )
        runtime.authentication_service.set_user_password(
            incoming["password"], requested_by_principal_id=principal.id
        )
        digest = hashlib.sha256(token.encode()).hexdigest()
        for credential in runtime.api_credentials.list_scoped(organization.id):
            if credential.token_digest == digest:
                runtime.api_credentials.delete(credential.id)
        print("Initial administrator created; public onboarding remains disabled.")
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
