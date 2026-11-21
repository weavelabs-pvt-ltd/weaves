"""Detach one login identity from the signed-in account."""

from __future__ import annotations

import logging

from application.ports.repository import Repository
from contracts.v1 import Account, AccountIdentity

logger = logging.getLogger("product.account")


class LastVerifiedIdentity(Exception):
    """Removing this identity would leave the account unable to sign in."""


def remove_identity(
    identities: Repository[AccountIdentity], account: Account, identity_id: str
) -> None:
    """Delete one of the account's own identities, keeping a verified one."""
    try:
        identity = identities.get(identity_id)
    except KeyError as exc:
        raise PermissionError("identity not found") from exc
    if identity.account_id != account.id:
        raise PermissionError("identity not found")
    verified = [
        item
        for item in identities.list_by_field("account_id", account.id)
        if item.verified_at is not None
    ]
    if identity.verified_at is not None and len(verified) <= 1:
        raise LastVerifiedIdentity
    identities.delete(identity.id)
    logger.info(
        "account identity removed account_id=%s identity_id=%s kind=%s",
        account.id,
        identity.id,
        identity.kind.value,
    )
