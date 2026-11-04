"""Native connector OAuth entrypoint contracts."""

from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.support.runtime import LocalPlatformRuntime


def test_native_connectors_start_provider_authorization(monkeypatch) -> None:
    """Start GitHub, Slack, and Jira consent using their configured OAuth apps."""
    monkeypatch.setenv("WEAVES_PUBLIC_API_BASE_URL", "https://api.weaves.test")
    monkeypatch.setenv("WEAVES_GITHUB_APP_CLIENT_ID", "github-client")
    monkeypatch.setenv("WEAVES_GITHUB_APP_CLIENT_SECRET", "github-secret")
    monkeypatch.setenv("WEAVES_GITHUB_APP_SLUG", "weaves-app")
    monkeypatch.setenv("WEAVES_GITHUB_APP_ID", "1234")
    monkeypatch.setenv("WEAVES_GITHUB_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("WEAVES_SLACK_OAUTH_CLIENT_ID", "slack-client")
    monkeypatch.setenv("WEAVES_SLACK_OAUTH_CLIENT_SECRET", "slack-secret")
    monkeypatch.setenv("WEAVES_ATLASSIAN_OAUTH_CLIENT_ID", "jira-client")
    monkeypatch.setenv("WEAVES_ATLASSIAN_OAUTH_CLIENT_SECRET", "jira-secret")
    runtime = LocalPlatformRuntime(postgres_store=None)
    client = TestClient(create_app(runtime))
    client.__enter__()
    expected_hosts = {
        "github": "github.com",
        "slack": "slack.com",
        "jira": "auth.atlassian.com",
    }
    try:
        for provider, host in expected_hosts.items():
            response = client.post(
                f"/api/v0/oauth/{provider}/start",
                json={"display_name": f"{provider.title()} workspace"},
            )
            assert response.status_code == 200, response.text
            authorization = urlsplit(response.json()["authorization_url"])
            assert authorization.hostname == host
            assert parse_qs(authorization.query)["state"]
            assert parse_qs(authorization.query)["redirect_uri"] == [
                f"https://api.weaves.test/api/v0/oauth/{provider}/callback"
            ]
    finally:
        client.close()
        runtime.close()
