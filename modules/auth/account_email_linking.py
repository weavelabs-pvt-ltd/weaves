"""Attach an additional verified email to the signed-in account.

An address is attached only after the account proves control of it, and an
address already verified on another account is never moved or merged here: that
needs a deliberate, audited merge outside this flow.
"""

from __future__ import annotations

from contracts.v1 import Account, AccountIdentity, AccountIdentityKind
from modules.auth.account_sign_in import CODE_MINUTES, AccountSignInService
from shared.clock import new_id, utc_now
from shared.errors import EmailDeliveryUnavailable


class EmailAlreadyAttached(Exception):
    """The proven address is verified on a different account."""


class LastVerifiedIdentity(Exception):
    """Removing this identity would leave the account unable to sign in."""


class AccountEmailLinking:
    """Add and remove verified email identities for one authenticated account."""

    def __init__(self, sign_in: AccountSignInService) -> None:
        """Share the sign-in service's repositories, throttles and email port."""
        self._sign_in = sign_in

    def start(self, account: Account, email: str, client_address: str | None) -> str:
        """Send a proof code for an address to attach; reveal nothing about it."""
        service = self._sign_in
        if (
            len(service.email_login_hmac_key.encode("utf-8")) < 32
            or not service.email_sender.configured
        ):
            raise EmailDeliveryUnavailable("Email sign-in is not configured")
        normalized = email.strip().casefold()
        service._consume_limits(normalized, client_address, include_source=True)
        if "@" not in normalized or not 3 <= len(normalized) <= 320:
            return new_id()
        challenge, code = service._new_challenge(normalized)
        challenge = challenge.model_copy(update={"account_id": account.id})
        service._replace_challenges(challenge)
        try:
            service.email_sender.send(
                normalized,
                "Confirm this email for your Weaves account",
                f"Your Weaves confirmation code is {code}. It expires in "
                f"{CODE_MINUTES} minutes. "
                "If you did not request it, you can ignore this email.",
            )
        except Exception:
            service.challenges.delete(challenge.challenge_id)
            raise
        return challenge.challenge_id

    def verify(self, account: Account, challenge_id: str, code: str) -> AccountIdentity:
        """Attach the proven address and link the memberships it carries."""
        service = self._sign_in
        challenge = service.redeem(challenge_id, code, account_id=account.id)
        identity = self._attach(account, challenge.email)
        service._linker.link(account, challenge.email)
        return identity

    def _attach(self, account: Account, email: str) -> AccountIdentity:
        """Create or confirm the verified identity; refuse another account's."""
        service = self._sign_in
        existing = service._verified_identity(email)
        if existing is not None:
            if existing.account_id != account.id:
                raise EmailAlreadyAttached
            return existing
        now = utc_now()
        identity = AccountIdentity(
            id=new_id(),
            account_id=account.id,
            kind=AccountIdentityKind.EMAIL,
            subject=email,
            verified_at=now,
            created_at=now,
            updated_at=now,
        )
        try:
            return service.identities.create(identity)
        except ValueError:  # lost a race on the verified-identity index
            winner = service._verified_identity(email)
            if winner is not None and winner.account_id == account.id:
                return winner
            raise EmailAlreadyAttached from None

    def remove(self, account: Account, identity_id: str) -> None:
        """Detach one email, keeping at least one verified identity."""
        service = self._sign_in
        try:
            identity = service.identities.get(identity_id)
        except KeyError as exc:
            raise PermissionError("identity not found") from exc
        if identity.account_id != account.id:
            raise PermissionError("identity not found")
        verified = [
            item
            for item in service.identities.list()
            if item.account_id == account.id and item.verified_at is not None
        ]
        if identity.verified_at is not None and len(verified) <= 1:
            raise LastVerifiedIdentity
        service.identities.delete(identity.id)
