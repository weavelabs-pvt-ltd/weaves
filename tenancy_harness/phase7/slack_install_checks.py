"""Control T7-I01: the Slack bot install flow on the real stack (slice S1).

Organization A's admin starts the install and completes it through the product's
own callback against the fake Slack server; organization B then tries the same
workspace. Checked: bot scopes only, single-use state, the bot token only in the
encrypted secret store (never in a response, a Slack table or an audit row), one
workspace bound to one organization (the refused install's token is revoked),
reinstall rotating the token in place, Enterprise Grid org-wide installs refused,
row-level security on the new table, and disconnect revoking and deleting the
token and freeing the workspace.
"""

import json

from tenancy_harness.client import request
from tenancy_harness.phase7.slack_support import (
    BOT_SCOPES,
    TEAM,
    TOKEN_MARK,
    fake,
    fake_calls,
    install,
    record,
    reset_fake,
    rows,
    scoped,
    start_install,
    status,
)
from tenancy_harness.resources import tenant
from tenancy_harness.seed import require_disposable_database

CONTROL_ID = "T7-I01"
SLICE = "S1"
FIXTURE = "slack_install"
GRID_TEAM = "T0GRID001"


def secret_rows():
    """The bot-token rows of the secret store, ciphertext only."""
    return rows(
        "SELECT secret_ref, org_id, owner_ref, ciphertext FROM product_secrets "
        "WHERE purpose = 'slack_bot_token' ORDER BY created_at"
    )


def installation_rows():
    """The installations table as the owner sees it."""
    return rows(
        "SELECT installation_id, org_id, status, bot_secret_ref FROM "
        "slack_installations WHERE team_id = %s ORDER BY installed_at",
        TEAM,
    )


def no_token_anywhere(*views):
    """True when no fixture bot token appears in any given view or table."""
    tables = rows("SELECT t::text FROM slack_installations t") + rows(
        "SELECT t::text FROM slack_event_inbox t"
    )
    audits = rows(
        "SELECT payload::text FROM product_records WHERE collection = 'audit_events'"
    )
    ciphertext = [bytes(row[3]) for row in secret_rows()]
    texts = [json.dumps(view) for view in views] + [r[0] for r in tables + audits]
    return not any(TOKEN_MARK in text for text in texts) and not any(
        TOKEN_MARK.encode() in blob for blob in ciphertext
    )


def first_install(a):
    """Start (scopes, state) and complete A's install; replay the state."""
    refused = request("GET", "/oauth/slack/install")
    started, query = start_install(a)
    scopes = set(query.get("scope", "").split(","))
    done = request(
        "GET",
        "/oauth/slack/install/callback",
        {},
        params={"code": "fixture-code", "state": query["state"]},
    )
    replay = request(
        "GET",
        "/oauth/slack/install/callback",
        {},
        params={"code": "fixture-code", "state": query["state"]},
    )
    return {
        "start_refused_without_a_session": refused.status_code in (401, 403),
        "start_returns_the_bot_scopes_only": started.status_code == 200
        and scopes == BOT_SCOPES
        and "user_scope" not in query,
        "install_completes_and_redirects_ok": done.status_code == 303
        and "slack_install=ok" in done.headers["location"],
        "state_is_single_use": "invalid_state" in replay.headers.get("location", "")
        and len(fake_calls("oauth.v2.access")) == 1,
    }


def stored_results(a, view):
    """What the first install left behind, and what it must not have."""
    (inst,) = view["installations"]
    held = installation_rows()
    secrets = secret_rows()
    audit = rows(
        "SELECT payload->>'action' FROM product_records WHERE org_id = %s "
        "AND collection = 'audit_events' AND payload->>'action' LIKE 'slack.%%'",
        a["org_id"],
    )
    return {
        "status_shows_workspace_installer_and_state": inst["team_id"] == TEAM
        and inst["status"] == "active"
        and bool(inst["installed_by"])
        and inst["missing_scopes"] == [],
        "token_is_only_in_the_secret_store": len(held) == 1
        and held[0][3] == secrets[0][0]
        and secrets[0][1] == a["org_id"]
        and secrets[0][2] == TEAM,
        "no_token_in_responses_tables_audit_or_ciphertext": no_token_anywhere(view),
        "install_is_audited": audit == [("slack.installation.created",)],
    }


def binding_results(a, b):
    """B cannot take A's workspace; its freshly issued token is revoked."""
    revokes = len(fake_calls("auth.revoke"))
    outcome = install(b)
    held = installation_rows()
    return {
        "second_organization_is_refused_with_a_clear_reason": outcome.get("slack_error")
        == "workspace_already_connected",
        "refused_install_token_is_revoked": len(fake_calls("auth.revoke"))
        == revokes + 1,
        "workspace_stays_bound_to_one_organization": len(held) == 1
        and held[0][1] == a["org_id"]
        and status(b)["installations"] == [],
    }


def reinstall_results(a, before):
    """Reinstalling by A updates the row in place and rotates the secret."""
    old_ref = before["installations"][0]
    old_secret = installation_rows()[0][3]
    outcome = install(a)
    after = status(a)["installations"]
    held = installation_rows()
    live_refs = {row[0] for row in secret_rows()}
    return {
        "reinstall_updates_in_place_and_rotates_the_token": outcome.get("slack_install")
        == "ok"
        and len(after) == 1
        and after[0]["installation_id"] == old_ref["installation_id"]
        and held[0][3] != old_secret
        and old_secret not in live_refs
        and held[0][3] in live_refs,
    }


def grid_results(a):
    """An Enterprise Grid org-wide install is refused and its token revoked."""
    fake(
        "POST",
        "/_control/configure",
        {"codes": {"grid-code": {"team": GRID_TEAM, "enterprise": True}}},
    )
    revokes = len(fake_calls("auth.revoke"))
    outcome = install(a, code="grid-code")
    return {
        "enterprise_grid_org_wide_install_is_refused": outcome.get("slack_error")
        == "enterprise_install_unsupported"
        and not rows("SELECT 1 FROM slack_installations WHERE team_id = %s", GRID_TEAM)
        and len(fake_calls("auth.revoke")) == revokes + 1,
    }


def isolation_results(a, b):
    """Row-level security on the new table, as the restricted application role."""
    count = "SELECT count(*) FROM slack_installations"
    return {
        "rls_hides_the_installation_from_other_tenants_and_no_scope": scoped(
            count, b["org_id"]
        )[0][0]
        == 0
        and scoped(count)[0][0] == 0
        and scoped(count, a["org_id"])[0][0] == 1
        and scoped(count, purpose="slack")[0][0] == 1,
    }


def disconnect_results(a, b):
    """Disconnect revokes and deletes the token, then frees the workspace."""
    (inst,) = status(a)["installations"]
    secret_before = installation_rows()[0][3]
    revokes = len(fake_calls("auth.revoke"))
    other = request(
        "DELETE",
        "/organization/slack/" + inst["installation_id"],
        b["admin"]["headers"],
    )
    gone = request(
        "DELETE",
        "/organization/slack/" + inst["installation_id"],
        a["admin"]["headers"],
    )
    revoked_once = len(fake_calls("auth.revoke")) == revokes + 1
    deleted = secret_before not in {row[0] for row in secret_rows()}
    moved = install(b)
    held = installation_rows()
    (mine,) = [i for i in status(b)["installations"] if i["status"] == "active"]
    request(
        "DELETE",
        "/organization/slack/" + mine["installation_id"],
        b["admin"]["headers"],
    )
    free = not rows(
        "SELECT 1 FROM slack_installations WHERE team_id = %s "
        "AND status IN ('active', 'suspended')",
        TEAM,
    )
    return {
        "the_workspace_is_free_for_the_next_control": free,
        "another_organization_cannot_disconnect_it": other.status_code == 404,
        "disconnect_revokes_and_deletes_the_token": gone.status_code == 200
        and gone.json()["status"] == "uninstalled"
        and revoked_once
        and deleted,
        "freed_workspace_can_be_claimed_by_another_organization": moved.get(
            "slack_install"
        )
        == "ok"
        and [row[2] for row in held] == ["uninstalled", "active"]
        and held[1][1] == b["org_id"],
    }


def main():
    """Run the control and record it in the private fixture file."""
    require_disposable_database()
    reset_fake()
    a, b = tenant("p7a"), tenant("p7b")
    results = first_install(a)
    results.update(stored_results(a, status(a)))
    results.update(binding_results(a, b))
    results.update(reinstall_results(a, status(a)))
    results.update(grid_results(a))
    results.update(isolation_results(a, b))
    results.update(disconnect_results(a, b))
    results["no_token_leaked_after_the_whole_flow"] = no_token_anywhere(
        status(a), status(b)
    )
    reset_fake()
    record(FIXTURE, results, CONTROL_ID)


if __name__ == "__main__":
    main()
