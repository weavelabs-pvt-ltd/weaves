"""Real-stack control T4-I02: organization lifecycle, export and deletion.

Runs in the seed container after the native worker is up. HTTP calls go through
the real API as a signed-in browser would; operator actions run the real
``organization_lifecycle.py`` script (mounted from the backend checkout) against
the disposable database with the owner connection, exactly as it is streamed
into the container on the VM. Nothing here uses a testing endpoint.
"""

import contextlib
import importlib.util
import io
import json
import os
import time
from pathlib import Path

import psycopg

from tenancy_harness.client import account_session, request, source_for
from tenancy_harness.seed import require_disposable_database

OWNER = "lifecycle.owner@tenancy.test"
SECOND = "lifecycle.second@tenancy.test"
SCRIPT = Path("/suite/operator/organization_lifecycle.py")
OP = ["--operator", "harness@tenancy.test", "--reason", "T4-I02 control"]
RETAINED = {"organization_lifecycle", "audit_events"}
UNAVAILABLE = (403, {"detail": "Organization not available"})


def operator(*argv):
    """Run the real operator script; return (exit status, JSON stdout)."""
    spec = importlib.util.spec_from_file_location("organization_lifecycle", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = module.run(list(argv))
    text = out.getvalue().strip()
    return status, json.loads(text) if text else None


def act(command, org_id, *extra):
    """One operator action with the mandatory operator and reason."""
    return operator(command, "--organization-id", org_id, *OP, *extra)[0]


def create_organization(email, name):
    """Create an organization through the account API; return its actor headers."""
    session = account_session(email)
    response = request(
        "POST",
        "/account/browser/organizations",
        {**session, "Idempotency-Key": "t4i02-" + name, **source_for(email)},
        {"name": name},
    )
    if response.status_code != 201:
        return {"status": response.status_code}
    body = response.json()
    org, workspace = body["organization"]["id"], body["workspace"]["id"]
    return {
        "status": 201,
        "org_id": org,
        "name": name,
        "session": session,
        "headers": {**session, "X-Organization-Id": org, "X-Workspace-ID": workspace},
    }


def state_of(org_id):
    """The stored lifecycle state, read with the disposable owner connection."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        row = connection.execute(
            "SELECT payload->>'state' FROM product_records "
            "WHERE collection = 'organization_lifecycle' AND record_id = %s",
            (org_id,),
        ).fetchone()
    return row[0] if row else "active"


def owned_rows(org_id):
    """Collections that still hold rows for one organization, plus secret count."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        rows = connection.execute(
            "SELECT collection, count(*) FROM product_records "
            "WHERE org_id = %s GROUP BY collection",
            (org_id,),
        ).fetchall()
        secrets = connection.execute(
            "SELECT count(*) FROM product_secrets WHERE org_id = %s", (org_id,)
        ).fetchone()[0]
    return dict(rows), secrets


def wait_for(org_id, wanted, seconds=120):
    """Poll until the worker has moved the organization to the wanted state."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if state_of(org_id) == wanted:
            return True
        time.sleep(1)
    return False


def get(actor, path="/agents"):
    """Return (status, body) of one authenticated tenant request."""
    response = request("GET", path, actor["headers"])
    return response.status_code, response.json() if response.content else None


def refused(actor, path="/agents"):
    """True when the request got the identical organization-unavailable 403."""
    return get(actor, path) == UNAVAILABLE


def owner_checks(one, two):
    """Export, exact-name deletion request, narrow recovery and owner cancel."""
    exported = request("POST", "/organization/export", one["headers"])
    document = exported.json() if exported.status_code == 200 else {}
    wrong = request(
        "POST", "/organization/deletion-request", one["headers"], {"confirm_name": "x"}
    )
    asked = request(
        "POST",
        "/organization/deletion-request",
        one["headers"],
        {"confirm_name": one["name"], "reason": "control"},
    )
    pending = get(one, "/organization/lifecycle")[1] if asked.status_code == 200 else {}
    other_routes_refused = refused(one)
    cancelled = request("POST", "/organization/deletion-cancel", one["headers"])
    return {
        "owner_export_downloads": exported.status_code == 200
        and exported.headers.get("content-disposition", "").startswith("attachment")
        and exported.headers.get("cache-control") == "no-store"
        and document.get("organization", {}).get("name") == one["name"]
        and two["name"] not in exported.text,
        "deletion_needs_exact_name": wrong.status_code == 422,
        "owner_requests_deletion": asked.status_code == 200
        and pending.get("state") == "deletion_requested",
        "pending_deletion_refuses_other_routes": other_routes_refused,
        "owner_cancels_deletion": cancelled.status_code == 200 and get(one)[0] == 200,
    }


def suspension_checks(one, two):
    """Operator suspend/resume is attributable, refused for the owner, scoped."""
    suspended = act("suspend", one["org_id"]) == 0
    suspended = suspended and state_of(one["org_id"]) == "suspended"
    owner_blocked = refused(one) and refused(one, "/organization/lifecycle")
    cancel = request("POST", "/organization/deletion-cancel", one["headers"])
    other_works = get(two)[0] == 200
    resumed = act("resume", one["org_id"]) == 0
    return {
        "operator_suspends": suspended,
        "suspended_refuses_owner": owner_blocked and cancel.status_code == 403,
        "other_organization_still_works": other_works,
        "operator_resumes": resumed and get(one)[0] == 200,
    }


def deletion_checks(one, two, before):
    """Owner request, grace skipped by purge-now, worker purge, terminal state."""
    asked = request(
        "POST",
        "/organization/deletion-request",
        one["headers"],
        {"confirm_name": one["name"]},
    )
    bad = act("purge-now", one["org_id"], "--confirm-organization-name", "nope")
    good = act("purge-now", one["org_id"], "--confirm-organization-name", one["name"])
    done = wait_for(one["org_id"], "deleted")
    left, secrets = owned_rows(one["org_id"])
    memberships = request("GET", "/account/browser/me", one["session"]).json()
    return {
        "purge_needs_exact_name": asked.status_code == 200 and bad == 2,
        "operator_purge_now_deletes": good == 0 and done,
        "organization_rows_gone": set(left) <= RETAINED and secrets == 0,
        "other_organization_intact": owned_rows(two["org_id"]) == before
        and get(two)[0] == 200,
        "deleted_never_selectable": refused(one)
        and one["org_id"]
        not in {m["organization_id"] for m in memberships["memberships"]},
        "deleted_cannot_resume": act("resume", one["org_id"]) == 2
        and state_of(one["org_id"]) == "deleted",
    }


def worker_and_cap_checks():
    """The worker keeps running after a purge; deleted orgs still count to the cap."""
    three = create_organization(SECOND, "Lifecycle Three")
    ok = three["status"] == 201
    if ok:
        request(
            "POST",
            "/organization/deletion-request",
            three["headers"],
            {"confirm_name": three["name"]},
        )
        act("purge-now", three["org_id"], "--confirm-organization-name", three["name"])
    again = create_organization(OWNER, "Lifecycle Again")
    extra = create_organization(OWNER, "Lifecycle Over Cap")
    return {
        "worker_keeps_running": ok and wait_for(three["org_id"], "deleted"),
        "deleted_organization_counts_toward_cap": again["status"] == 201
        and extra["status"] != 201,
    }


def lifecycle_control():
    """Run the whole T4-I02 scenario and return its named boolean results."""
    one = create_organization(OWNER, "Lifecycle One")
    two = create_organization(OWNER, "Lifecycle Two")
    if one["status"] != 201 or two["status"] != 201:
        raise AssertionError("Lifecycle fixture organizations were not created")
    result = owner_checks(one, two)
    result.update(suspension_checks(one, two))
    before = owned_rows(two["org_id"])
    result.update(deletion_checks(one, two, before))
    result.update(worker_and_cap_checks())
    return result


def main():
    """Record the control in the private fixture file the browser suite reads."""
    require_disposable_database()
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    fixtures = json.loads(path.read_text())
    fixtures["organization_lifecycle"] = lifecycle_control()
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    print("Organization lifecycle control recorded.")


if __name__ == "__main__":
    main()
