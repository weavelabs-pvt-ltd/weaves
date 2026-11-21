"""Fixture for T4-I05: one person with two active organizations and a suspended one.

The browser control signs this person in, sees the chooser with a suspended card, then
exports and requests deletion of one organization and cancels it. The suspension is done
by the real operator script, exactly as an operator would on the VM.
"""

from tenancy_harness.lifecycle_checks import act, create_organization

UI_OWNER = "lifecycle.ui@tenancy.test"


def lifecycle_ui_fixture():
    """Create three organizations, suspend the last, return what the browser needs."""
    orgs = [
        create_organization(UI_OWNER, name)
        for name in ("Lifecycle UI Alpha", "Lifecycle UI Beta", "Lifecycle UI Gamma")
    ]
    if any(org["status"] != 201 for org in orgs):
        raise AssertionError("Lifecycle UI organizations were not created")
    if act("suspend", orgs[2]["org_id"]) != 0:
        raise AssertionError("The operator could not suspend the third organization")
    return {
        "owner_email": UI_OWNER,
        "alpha": {"org_id": orgs[0]["org_id"], "name": orgs[0]["name"]},
        "beta": {"org_id": orgs[1]["org_id"], "name": orgs[1]["name"]},
        "gamma": {"org_id": orgs[2]["org_id"], "name": orgs[2]["name"]},
    }
