"""Settings and constants for the Slack bot install flow (section 5.1 of the design)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from shared.slack_config import SlackConfig

# Bot scopes only: the user scopes belong to the read connector and are never
# merged into the bot install.
BOT_SCOPES: tuple[str, ...] = (
    "app_mentions:read",
    "chat:write",
    "channels:history",
    "groups:history",
    "channels:read",
    "groups:read",
    "reactions:read",
    "reactions:write",
)
# An install without these cannot work at all, so it is refused (the rest only
# narrows features, and the status view reports what is missing).
REQUIRED_SCOPES: frozenset[str] = frozenset({"app_mentions:read", "chat:write"})
AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
CALLBACK_PATH = "/api/v0/oauth/slack/install/callback"
DEFAULT_FRONTEND = "https://app.weave-studio.in"


@dataclass(frozen=True)
class SlackInstallSettings:
    """What the install flow needs from the environment; secrets never print."""

    config: SlackConfig
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    api_base: str = ""
    frontend: str = DEFAULT_FRONTEND

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str] | None = None
    ) -> SlackInstallSettings:
        """Read the Slack app credentials shared with the read connector."""
        source = os.environ if env is None else env

        def read(name: str) -> str:
            return source.get(name, "").strip()

        return cls(
            config=SlackConfig.from_environment(source),
            client_id=read("WEAVES_SLACK_OAUTH_CLIENT_ID"),
            client_secret=read("WEAVES_SLACK_OAUTH_CLIENT_SECRET"),
            api_base=read("WEAVES_PUBLIC_API_BASE_URL").rstrip("/"),
            frontend=(read("WEAVES_PUBLIC_FRONTEND_URL") or DEFAULT_FRONTEND).rstrip(
                "/"
            ),
        )

    @property
    def install_available(self) -> bool:
        """Whether an admin may start an install (events also need the secret)."""
        return bool(
            self.config.enabled
            and self.config.signing_secret
            and self.client_id
            and self.client_secret
            and self.api_base
        )

    @property
    def redirect_uri(self) -> str:
        """The bot install callback registered with the Slack app."""
        return f"{self.api_base}{CALLBACK_PATH}"
