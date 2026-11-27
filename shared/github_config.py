"""The GitHub REST API origin (``WEAVES_GITHUB_API_BASE_URL``, harness use only).

Production leaves it unset and talks to ``https://api.github.com``. The real-stack
test harness points it at its own fake GitHub server, the same way
``WEAVES_SLACK_API_BASE_URL`` points the bot at the fake Slack. A value that is
not an http(s) URL is ignored in favour of the default (never a start-up error:
the connector must keep working).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"


def github_api_base_url_from_environment(env: Mapping[str, str] | None = None) -> str:
    """The configured GitHub API origin, or GitHub's own."""
    source = os.environ if env is None else env
    value = (source.get("WEAVES_GITHUB_API_BASE_URL") or "").strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme in {"http", "https"} and parts.hostname:
        return value
    return DEFAULT_GITHUB_API_BASE_URL
