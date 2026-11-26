"""Who may install, see and disconnect the bot, and across organizations."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from tests.slack.support import CALLBACK, START, STATUS, World
from tests.support.account_session import account_headers


def _member(world: World, role_key: str) -> dict[str, str]:
    owner = world.alpha.headers
    roles = world.client.get("/api/v0/roles", headers=owner).json()
    role_id = next(r["id"] for r in roles if r["system_role_key"] == role_key)
    created = world.client.post(
        "/api/v0/users",
        headers=owner,
        json={
            "email": f"{role_key}@alpha.test",
            "display_name": role_key.title(),
            "role_id": role_id,
        },
    )
    assert created.status_code == 201, created.text
    return account_headers(
        world.client,
        f"{role_key}@alpha.test",
        world.alpha.org_id,
        world.alpha.workspace_id,
    )


def test_only_an_organization_admin_may_start_see_or_disconnect(world: World) -> None:
    world.install()
    installation_id = world.status()["installations"][0]["installation_id"]
    for role in ("user", "builder"):
        member = _member(world, role)
        assert world.client.get(START, headers=member).status_code == 403
        assert world.client.get(STATUS, headers=member).status_code == 403
        deleted = world.client.delete(f"{STATUS}/{installation_id}", headers=member)
        assert deleted.status_code == 403
    assert world.status()["installations"][0]["status"] == "active"


def test_unauthenticated_callers_get_nothing(world: World) -> None:
    assert world.client.get(START).status_code in {401, 403}
    assert world.client.get(STATUS).status_code in {401, 403}
    assert world.client.delete(f"{STATUS}/anything").status_code in {401, 403}


def test_the_bound_organization_is_the_states_never_one_named_by_slack(
    world: World,
) -> None:
    started = world.client.get(START, headers=world.alpha.headers)
    state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
    # The state belongs to alpha's admin; another org cannot use it as its own,
    # and the installed org is always the state's, never one named by Slack.
    response = world.client.get(
        CALLBACK, params={"code": "c", "state": state}, follow_redirects=False
    )
    assert response.status_code == 303
    holder = world.runtime.slack_store.find_by_team("T0ACME001")
    assert holder is not None and holder.org_id == world.alpha.org_id
    assert holder.installed_by_principal_id


def test_an_organization_cannot_see_or_disconnect_anothers_installation(
    world: World,
) -> None:
    world.install()
    installation_id = world.status()["installations"][0]["installation_id"]
    assert world.status(world.beta)["installations"] == []
    deleted = world.client.delete(
        f"{STATUS}/{installation_id}", headers=world.beta.headers
    )
    assert deleted.status_code == 404
    assert world.status()["installations"][0]["status"] == "active"
    assert world.api.methods() == ["oauth.v2.access"]  # nothing was revoked


def test_disconnecting_twice_is_harmless(world: World) -> None:
    world.install()
    installation_id = world.status()["installations"][0]["installation_id"]
    for _ in range(2):
        response = world.client.delete(
            f"{STATUS}/{installation_id}", headers=world.alpha.headers
        )
        assert response.status_code == 200
        assert response.json()["status"] == "uninstalled"
    assert world.api.methods().count("auth.revoke") == 1
