"""Environment-backed settings for the Slack bot (dark until later slices use them).

``WEAVES_SLACK_SIGNING_SECRET`` is read by the API only and is optional: when it
is absent nothing in the product changes. ``WEAVES_SLACK_ENABLED`` defaults to
false. Enabling the bot without a signing secret is a start-up error, so a
misconfigured deployment fails loudly instead of accepting unsigned requests.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

DEFAULT_API_BASE_URL = "https://slack.com/api"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"", "0", "false", "no", "off"}


class SlackConfigError(ValueError):
    """A Slack setting is malformed or incomplete."""


def _enabled(raw: str | None) -> bool:
    value = (raw or "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise SlackConfigError("WEAVES_SLACK_ENABLED must be true or false")


def _api_base_url(raw: str | None) -> str:
    value = (raw or "").strip().rstrip("/")
    if not value:
        return DEFAULT_API_BASE_URL
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SlackConfigError("WEAVES_SLACK_API_BASE_URL must be an http(s) URL")
    return value


def slack_enabled_from_environment(env: Mapping[str, str] | None = None) -> bool:
    """Only the kill switch, for processes that never see the signing secret."""
    source = os.environ if env is None else env
    return _enabled(source.get("WEAVES_SLACK_ENABLED"))


@dataclass(frozen=True)
class SlackConfig:
    """Signing secret, kill switch and API origin; the secret never prints."""

    signing_secret: bytes = field(default=b"", repr=False)
    enabled: bool = False
    api_base_url: str = DEFAULT_API_BASE_URL

    def __post_init__(self) -> None:
        """Refuse an enabled bot with no signing secret."""
        if self.enabled and not self.signing_secret:
            raise SlackConfigError(
                "WEAVES_SLACK_SIGNING_SECRET is required when "
                "WEAVES_SLACK_ENABLED is true"
            )

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> SlackConfig:
        """Read the settings; absent values mean the bot is off."""
        source = os.environ if env is None else env
        secret = source.get("WEAVES_SLACK_SIGNING_SECRET", "").strip()
        return cls(
            signing_secret=secret.encode("utf-8"),
            enabled=_enabled(source.get("WEAVES_SLACK_ENABLED")),
            api_base_url=_api_base_url(source.get("WEAVES_SLACK_API_BASE_URL")),
        )
