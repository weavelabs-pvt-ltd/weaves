"""Control T7-I03: the Slack bot lifecycle on the real stack (slice S7).

Two organizations each install the bot into their own fake workspace. Checked
against the running API, the independent worker, the real operator scripts and
PostgreSQL: ``app_uninstalled`` ends the installation (token deleted, the
workspace's pending inbox rows dropped without text, later events are unknown
team drops, a repeat changes nothing) and frees the workspace for another
organization; bot-token revocation ends it while a user-token revocation does
not, and a clean reinstall follows; ``quarantine`` suspends the installation
(events dropped, nothing answered, the other tenant untouched) and
``release-quarantine`` restores it, while quarantine with credential revocation
ends the token for good; deleting the organization revokes the token at Slack,
removes the installation, inbox rows and secret and leaves the other tenant's
installation, inbox and secret as they were. No token appears in any table,
audit row or ciphertext.
"""

from tenancy_harness.client import request
from tenancy_harness.lifecycle_checks import state_of, wait_for
from tenancy_harness.phase6.controls_checks import lifecycle
from tenancy_harness.phase7.slack_event_checks import acked, next_ts
from tenancy_harness.phase7.slack_install_checks import no_token_anywhere
from tenancy_harness.phase7.slack_support import (
    deliver,
    fake,
    fake_calls,
    install,
    mention,
    record,
    reset_fake,
    rows,
    status,
)
from tenancy_harness.resources import tenant
from tenancy_harness.seed import require_disposable_database

CONTROL_ID = "T7-I03"
SLICE = "S7"
FIXTURE = "slack_lifecycle"
TEAM_A, TEAM_B = "T0LIFEA01", "T0LIFEB01"
OP = ["--operator", "harness@tenancy.test", "--reason", "T7-I03 control"]
CODES = {
    code: {"team": team}
    for code, team in (
        ("life-a", TEAM_A),
        ("life-a2", TEAM_A),
        ("life-a3", TEAM_A),
        ("life-a4", TEAM_A),
        ("life-a5", TEAM_A),
        ("life-b", TEAM_B),
    )
}


DETAILS = {}


def inbox(event_id):
    """One inbox row as the owner sees it: state, reason, text, organization."""
    found = rows(
        "SELECT state, drop_reason, text, org_id FROM slack_event_inbox "
        "WHERE event_id = %s",
        event_id,
    )
    return found[0] if found else None


def installations(team):
    """(organization, status, has token reference) of every row of one workspace."""
    return rows(
        "SELECT org_id, status, bot_secret_ref IS NOT NULL FROM slack_installations "
        "WHERE team_id = %s ORDER BY installed_at",
        team,
    )


def mine(org_id, team):
    """(status, has token reference) of one organization's row for a workspace."""
    found = rows(
        "SELECT status, bot_secret_ref IS NOT NULL FROM slack_installations "
        "WHERE team_id = %s AND org_id = %s",
        team,
        org_id,
    )
    return found[0] if found else None


def secrets_of(org_id):
    """How many bot tokens an organization has in the secret store."""
    return rows(
        "SELECT count(*) FROM product_secrets WHERE purpose = 'slack_bot_token' "
        "AND org_id = %s",
        org_id,
    )[0][0]


def post(event_id, team):
    """Deliver a mention for ``team``; returns the delivery report."""
    return deliver(mention(event_id, next_ts(), team=team))


def lifecycle_event(kind, team, event_id, **event):
    """An ``app_uninstalled`` or ``tokens_revoked`` callback."""
    return deliver(
        {
            "type": "event_callback",
            "team_id": team,
            "event_id": event_id,
            "event": {"type": kind, **event},
        }
    )


def audit_count(org_id, action):
    """Audit events of one action in one organization."""
    return rows(
        "SELECT count(*) FROM product_records WHERE org_id = %s "
        "AND collection = 'audit_events' AND payload->>'action' = %s",
        org_id,
        action,
    )[0][0]


def uninstall_results(a, b):
    """``app_uninstalled``: token gone, pending dropped, unknown team, repeat safe."""
    post("Ev7LA0001", TEAM_A)
    post("Ev7LB0001", TEAM_B)
    gone = lifecycle_event("app_uninstalled", TEAM_A, "Ev7LAUN01")
    ended = installations(TEAM_A)
    later = post("Ev7LA0002", TEAM_A)
    again = lifecycle_event("app_uninstalled", TEAM_A, "Ev7LAUN02")
    return {
        "app_uninstalled_ends_it_and_deletes_the_token": acked(gone)
        and ended == [(a["org_id"], "uninstalled", False)]
        and secrets_of(a["org_id"]) == 0
        and status(a)["installations"][0]["status"] == "uninstalled",
        "pending_events_of_the_workspace_are_dropped_without_text": inbox("Ev7LA0001")[
            :3
        ]
        == ("dropped", "installation_ended", None),
        "the_other_tenants_installation_and_inbox_are_untouched": installations(TEAM_B)
        == [(b["org_id"], "active", True)]
        and inbox("Ev7LB0001")[0] == "pending"
        and inbox("Ev7LB0001")[2] is not None,
        "events_for_an_ended_workspace_are_acked_and_unknown": acked(later)
        and inbox("Ev7LA0002") is None,
        "a_repeated_uninstall_changes_nothing": acked(again)
        and audit_count(a["org_id"], "slack.installation.uninstalled") == 1,
    }


def takeover_results(a, b):
    """Another organization installs the freed workspace, then releases it."""
    outcome = install(b, code="life-a2")
    held = installations(TEAM_A)
    acked_mention = post("Ev7LA0003", TEAM_A)
    (mine,) = [i for i in status(b)["installations"] if i["team_id"] == TEAM_A]
    request(
        "DELETE",
        "/organization/slack/" + mine["installation_id"],
        b["admin"]["headers"],
    )
    return {
        "another_organization_installs_the_freed_workspace": outcome.get(
            "slack_install"
        )
        == "ok"
        and [(r[0], r[1]) for r in held]
        == [(a["org_id"], "uninstalled"), (b["org_id"], "active")]
        and acked(acked_mention)
        and inbox("Ev7LA0003")[3] == b["org_id"],
        "the_new_owner_can_release_it_again": installations(TEAM_A)[-1][1]
        == "uninstalled",
    }


def revocation_results(a):
    """User-token revocation is ignored; a bot-token one ends it; reinstall is clean."""
    back = install(a, code="life-a3")
    user_only = lifecycle_event(
        "tokens_revoked", TEAM_A, "Ev7LATK01", tokens={"oauth": ["U0X"], "bot": []}
    )
    still = mine(a["org_id"], TEAM_A)[0]
    bot = lifecycle_event(
        "tokens_revoked", TEAM_A, "Ev7LATK02", tokens={"bot": ["U0FAKEBOT"]}
    )
    revoked = mine(a["org_id"], TEAM_A)
    token_left = secrets_of(a["org_id"])
    reinstalled = install(a, code="life-a4")
    DETAILS["revocation"] = [acked(bot), revoked, token_left]
    return {
        "a_clean_reinstall_follows_an_uninstall": back.get("slack_install") == "ok"
        and mine(a["org_id"], TEAM_A)[0] == "active",
        "revoking_only_user_tokens_leaves_the_bot_alone": acked(user_only)
        and still == "active",
        "bot_token_revocation_ends_it_and_deletes_the_token": acked(bot)
        and revoked == ("revoked", False)
        and token_left == 0,
        "a_revoked_workspace_reinstalls_in_place": reinstalled.get("slack_install")
        == "ok"
        and len(installations(TEAM_A)) == 2
        and mine(a["org_id"], TEAM_A)[0] == "active",
    }


def quarantine_results(a, b):
    """Quarantine suspends and drops; release restores; revocation is for good."""
    held = lifecycle("quarantine", "--organization-id", a["org_id"], *OP)
    suspended = mine(a["org_id"], TEAM_A)[0]
    ts_a = next_ts()
    deliver(mention("Ev7LAQ001", ts_a, team=TEAM_A))
    post("Ev7LBQ001", TEAM_B)
    quarantined = inbox("Ev7LAQ001")
    other = inbox("Ev7LBQ001")
    sent = [
        c for c in fake_calls("reactions.add") if c["params"].get("timestamp") == ts_a
    ]
    freed = lifecycle("release-quarantine", "--organization-id", a["org_id"], *OP)
    restored = mine(a["org_id"], TEAM_A)[0]
    post("Ev7LAQ002", TEAM_A)
    queued = inbox("Ev7LAQ002")[0]
    revokes = len(fake_calls("auth.revoke"))
    final = lifecycle(
        "quarantine", "--organization-id", a["org_id"], *OP, "--revoke-credentials"
    )
    ended = mine(a["org_id"], TEAM_A)
    token_left = secrets_of(a["org_id"])
    revoked_calls = len(fake_calls("auth.revoke")) - revokes
    lifecycle("release-quarantine", "--organization-id", a["org_id"], *OP)
    after = mine(a["org_id"], TEAM_A)
    reinstalled = install(a, code="life-a5")
    DETAILS["quarantine"] = [
        freed,
        restored,
        queued,
        final,
        ended,
        token_left,
        revoked_calls,
        after,
    ]
    return {
        "quarantine_suspends_the_installation": held[0] == 0
        and suspended == "suspended"
        and held[1]["quarantine"]["slack_suspended"] == 1,
        "a_quarantined_organizations_events_are_dropped_without_text": quarantined[:3]
        == ("dropped", "installation_suspended", None)
        and not sent,
        "quarantine_leaves_the_other_tenant_working": other[0] == "pending"
        and installations(TEAM_B) == [(b["org_id"], "active", True)],
        "release_restores_the_suspended_installation": freed[0] == 0
        and restored == "active"
        and queued == "pending",
        "quarantine_with_revocation_ends_the_token_for_good": final[0] == 0
        and ended == ("revoked", False)
        and token_left == 0
        and revoked_calls == 1
        and after[0] == "revoked",
        "the_admin_can_reinstall_after_a_quarantine": reinstalled.get("slack_install")
        == "ok",
    }


def purge_results(a, b):
    """Deleting the organization revokes, removes and leaves the other tenant."""
    post("Ev7LAP001", TEAM_A)
    before_b = (
        installations(TEAM_B),
        rows("SELECT count(*) FROM slack_event_inbox WHERE org_id = %s", b["org_id"]),
        secrets_of(b["org_id"]),
    )
    revokes = len(fake_calls("auth.revoke"))
    asked = lifecycle("request-deletion", "--organization-id", a["org_id"], *OP)
    purged = lifecycle(
        "purge-now",
        "--organization-id",
        a["org_id"],
        *OP,
        "--confirm-organization-name",
        a["marker"],
    )
    done = wait_for(a["org_id"], "deleted")
    after_b = (
        installations(TEAM_B),
        rows("SELECT count(*) FROM slack_event_inbox WHERE org_id = %s", b["org_id"]),
        secrets_of(b["org_id"]),
    )
    return {
        "organization_deletion_removes_the_slack_data": asked[0] == 0
        and purged[0] == 0
        and done
        and state_of(a["org_id"]) == "deleted"
        and not rows("SELECT 1 FROM slack_installations WHERE org_id = %s", a["org_id"])
        and not rows("SELECT 1 FROM slack_event_inbox WHERE org_id = %s", a["org_id"])
        and secrets_of(a["org_id"]) == 0,
        "organization_deletion_revokes_the_bot_token_at_slack": len(
            fake_calls("auth.revoke")
        )
        == revokes + 1,
        "organization_deletion_leaves_the_other_tenant_as_it_was": after_b == before_b
        and after_b[0] == [(b["org_id"], "active", True)],
    }


def main():
    """Run the control and record it in the private fixture file."""
    require_disposable_database()
    reset_fake()
    fake("POST", "/_control/configure", {"codes": CODES})
    a, b = tenant("p7la"), tenant("p7lb")
    if install(a, code="life-a").get("slack_install") != "ok":
        raise AssertionError("the install for the lifecycle control did not complete")
    if install(b, code="life-b").get("slack_install") != "ok":
        raise AssertionError("the second install did not complete")
    try:
        results = uninstall_results(a, b)
        results.update(takeover_results(a, b))
        results.update(revocation_results(a))
        results.update(quarantine_results(a, b))
        results.update(purge_results(a, b))
        results["no_token_in_any_table_audit_or_ciphertext"] = no_token_anywhere(
            status(b)
        )
    finally:
        reset_fake()
    results["details"] = DETAILS
    record(FIXTURE, results, CONTROL_ID)


if __name__ == "__main__":
    main()
