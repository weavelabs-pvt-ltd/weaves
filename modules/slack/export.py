"""What the owner export says about Slack installations.

Settings and allowlists are product-record collections in the export. The
installation rows add which workspaces are connected and in what state. The bot
token and its secret reference are never exported, and neither are message
texts: the inbox is a transient queue, not tenant data worth exporting.
"""

from __future__ import annotations

from typing import Any

from application.ports.slack import SlackStore


def installation_rows(store: SlackStore) -> list[dict[str, Any]]:
    """The current organization's installations without any credential."""
    return [
        {
            "installation_id": row.installation_id,
            "team_id": row.team_id,
            "team_name": row.team_name,
            "enterprise_id": row.enterprise_id,
            "app_id": row.app_id,
            "bot_user_id": row.bot_user_id,
            "scopes": list(row.scopes),
            "status": row.status,
            "installed_by_principal_id": row.installed_by_principal_id,
            "installed_by_name": row.installed_by_name,
            "installed_by_slack_user": row.installed_by_slack_user,
            "installed_at": row.installed_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        }
        for row in store.list_for_org()
    ]
