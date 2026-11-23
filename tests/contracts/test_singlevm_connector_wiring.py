"""The single-VM compose file must forward every connector setting that is read."""

from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "infra/gcp/singlevm/compose.yaml"

WORKER_SETTINGS = {
    "WEAVES_GITHUB_APP_ID",
    "WEAVES_GITHUB_APP_PRIVATE_KEY",
    "WEAVES_SLACK_OAUTH_CLIENT_ID",
    "WEAVES_SLACK_OAUTH_CLIENT_SECRET",
    "WEAVES_ATLASSIAN_OAUTH_CLIENT_ID",
    "WEAVES_ATLASSIAN_OAUTH_CLIENT_SECRET",
    "WEAVES_MCP_ALLOWED_HOSTS",
}
API_ONLY_SETTINGS = {
    "WEAVES_GITHUB_APP_CLIENT_ID",
    "WEAVES_GITHUB_APP_CLIENT_SECRET",
    "WEAVES_GITHUB_APP_SLUG",
}


def _environment(service: str) -> dict[str, str]:
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    return services[service]["environment"]


def test_api_receives_every_connector_setting_with_an_empty_default() -> None:
    environment = _environment("product-api")
    for name in WORKER_SETTINGS | API_ONLY_SETTINGS:
        assert environment[name] == "${" + name + ":-}"
    assert environment["WEAVES_PUBLIC_API_BASE_URL"] == "https://api.weave-studio.in"


def test_worker_receives_the_credentials_it_uses_to_refresh_and_mint_tokens() -> None:
    environment = _environment("product-worker")
    for name in WORKER_SETTINGS:
        assert environment[name] == "${" + name + ":-}"
