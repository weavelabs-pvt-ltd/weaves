"""Seed only an explicitly named disposable PostgreSQL tenancy database."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from tenancy_harness.client import wait_for_services
from tenancy_harness.diagnostics import seed_diagnostics
from tenancy_harness.resources import member, resources, secondary, tenant


def require_disposable_database():
    """Reject production URLs before any fixture mutation or privileged import."""
    parsed = urlsplit(os.environ.get("DATABASE_URL", ""))
    if (
        parsed.scheme not in {"postgresql", "postgres"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Tenancy fixture rejects database URL overrides")
    if parsed.hostname != "postgres" or parsed.path != "/weaves_tenancy_test":
        raise RuntimeError("Tenancy fixture requires its isolated Compose database")
    if parsed.username != "tenancy_test":
        raise RuntimeError("Tenancy fixture requires its dedicated test identity")


def main():
    """Provision real users and resources before the independent worker starts."""
    require_disposable_database()
    wait_for_services()
    fixtures = {"version": 1, "tenants": {}}
    for label in ("a", "b"):
        t = tenant(label)
        t["builder"] = member(t, "builder")
        t["user"] = member(t, "user")
        t["shared_person"] = member(
            t, "builder" if label == "a" else "user", "shared.person@tenancy.test"
        )
        resources(t)
        secondary(t)
        fixtures["tenants"][label] = t
    fixtures["diagnostics"] = seed_diagnostics(fixtures)
    if os.environ.get("TENANCY_CATALOG_READER_IMAGE"):
        from tenancy_harness.catalog_ownership import migrate_catalog

        fixtures["catalog_ownership"] = migrate_catalog(fixtures)
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    print("Private tenancy fixtures created; no credentials emitted.")


if __name__ == "__main__":
    main()
