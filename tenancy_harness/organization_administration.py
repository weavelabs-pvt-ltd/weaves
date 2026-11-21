"""Disposable ownership handoff and cross-organization member fixtures."""

from tenancy_harness.resources import member, tenant


def seed_organization_administration(fixtures):
    """Keep destructive administration cases away from the main task/AI fixtures."""
    primary = tenant("administration")
    email = "ownership.member@tenancy.test"
    candidate = member(primary, "builder", email)
    other = member(fixtures["tenants"]["b"], "user", email)
    return {"primary": primary, "candidate": candidate, "other": other}
