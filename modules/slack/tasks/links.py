"""Links of a Slack-created task: the page in Weaves and the thread in Slack.

* The Weaves page is ``{WEAVES_PUBLIC_FRONTEND_URL}/#tasks/{task_id}`` (the app's
  own route); without the variable no link is built and the messages leave the
  button out.
* The Slack permalink needs the workspace address (``https://acme.slack.com/``).
  It comes from ``auth.test`` with the bot token, once per workspace, and is
  cached on the organization's Slack settings row (a fact fetched from Slack, so
  no settings version bump). It is accepted only when it is an https
  ``*.slack.com`` address; anything else (or a failed call) means no permalink.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from urllib.parse import quote, urlsplit

from application.ports.secrets import SecretStore
from application.ports.slack import SlackInstallation
from clients.slack.client import SlackApiClient
from modules.slack.access.service import SlackAccessService

logger = logging.getLogger("product.slack")
_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}(\.enterprise)?\.slack\.com$")
_TS = re.compile(r"^\d{1,12}\.\d{1,8}$")


def workspace_origin(url: object) -> str | None:
    """``https://acme.slack.com/`` when ``url`` is a Slack workspace address."""
    if not isinstance(url, str):
        return None
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.port:
        return None
    return f"https://{host}/" if _HOST.fullmatch(host) else None


def permalink(origin: str, channel_id: str, thread_ts: str) -> str | None:
    """The link to a thread's first message (opens the thread in Slack)."""
    if not _TS.fullmatch(thread_ts) or not re.fullmatch(r"[A-Z0-9]{2,32}", channel_id):
        return None
    stamp = thread_ts.replace(".", "")
    return (
        f"{origin}archives/{channel_id}/p{stamp}"
        f"?thread_ts={quote(thread_ts)}&cid={channel_id}"
    )


class TaskLinks:
    """Build the two links of a task (never raises: a link is only a convenience)."""

    def __init__(
        self,
        *,
        access: SlackAccessService,
        api: SlackApiClient,
        secrets: SecretStore,
        app_url: Callable[[], str | None],
    ) -> None:
        """Bind the settings cache, the Slack client and the app address."""
        self._access = access
        self._api = api
        self._secrets = secrets
        self._app_url = app_url

    def task_url(self, task_id: str) -> str | None:
        """The task's page in the Weaves app, or ``None`` without an app address."""
        base = self._app_url()
        if not base:
            return None
        return f"{base.rstrip('/')}/#tasks/{quote(task_id, safe='')}"

    def thread_permalink(
        self, installation: SlackInstallation, channel_id: str, thread_ts: str
    ) -> str | None:
        """The Slack permalink of a thread, fetching the workspace address once."""
        origin = workspace_origin(self._access.workspace_url(installation.team_id))
        if origin is None:
            origin = self._fetch(installation)
        return permalink(origin, channel_id, thread_ts) if origin else None

    def _fetch(self, installation: SlackInstallation) -> str | None:
        if installation.bot_secret_ref is None:
            return None
        try:
            token = self._secrets.get(installation.bot_secret_ref)
            origin = workspace_origin(self._api.auth_test(token).get("url"))
            if origin is not None:
                self._access.remember_workspace_url(installation.team_id, origin)
            return origin
        except Exception as error:  # a permalink is optional: never fail a task for it
            logger.warning("slack_workspace_url_failed kind=%s", type(error).__name__)
            return None
