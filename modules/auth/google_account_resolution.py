"""Resolve a verified Google identity to exactly one account, or refuse."""

from __future__ import annotations

from contextlib import suppress

from contracts.v1 import (
    Account,
    AccountIdentity,
    AccountIdentityKind,
    AccountStatus,
)
from modules.auth.account_sign_in import AccountSignInService
from modules.auth.google_oidc import GoogleClaims
from shared.clock import new_id, utc_now


class SignInRefused(Exception):
    """Sign-in must end with one fixed, detail-free error code."""

    def __init__(self, code: str) -> None:
        """Keep the code that the callback redirects with."""
        super().__init__(code)
        self.code = code


class _LostRace(Exception):
    """Another request created one of the rows first; resolve again."""


class GoogleAccountResolver:
    """Match by OIDC subject, then by verified email, else create the account."""

    def __init__(self, service: AccountSignInService) -> None:
        """Share the account directory repositories."""
        self._service = service

    def resolve(self, claims: GoogleClaims) -> Account:
        """Return the account, with both a verified OIDC and email identity."""
        for _ in range(3):
            try:
                return self._resolve_once(claims)
            except _LostRace:
                continue
        raise SignInRefused("unavailable")

    def _oidc_identity(self, claims: GoogleClaims) -> AccountIdentity | None:
        """Find the verified OIDC identity for this issuer and subject."""
        for identity in self._service.identities.list_by_field(
            "subject", claims.subject
        ):
            if (
                identity.kind is AccountIdentityKind.OIDC
                and identity.issuer == claims.issuer
                and identity.verified_at is not None
            ):
                return identity
        return None

    def _resolve_once(self, claims: GoogleClaims) -> Account:
        """Plan without writing, refuse conflicts, then apply in one unit."""
        service = self._service
        oidc = self._oidc_identity(claims)
        by_email = service.verified_email_identity(claims.email)
        if oidc and by_email and oidc.account_id != by_email.account_id:
            raise SignInRefused("conflict")
        owner = oidc or by_email
        if owner is None:
            return self._create(claims)
        try:
            account = service.accounts.get(owner.account_id)
        except KeyError as exc:
            raise SignInRefused("unavailable") from exc
        if account.status is not AccountStatus.ACTIVE:
            raise SignInRefused("disabled")
        missing = [
            self._identity(account.id, kind, claims)
            for kind, present in (
                (AccountIdentityKind.OIDC, oidc),
                (AccountIdentityKind.EMAIL, by_email),
            )
            if present is None
        ]
        self._store(missing)
        return account

    def _create(self, claims: GoogleClaims) -> Account:
        """Create a new account together with both of its identities."""
        now = utc_now()
        account = Account(
            id=new_id(),
            display_name=(claims.name or claims.email.partition("@")[0])[:160]
            or claims.email[:160],
            status=AccountStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        self._store(
            [
                self._identity(account.id, AccountIdentityKind.OIDC, claims),
                self._identity(account.id, AccountIdentityKind.EMAIL, claims),
            ],
            new_account=account,
        )
        return account

    @staticmethod
    def _identity(
        account_id: str, kind: AccountIdentityKind, claims: GoogleClaims
    ) -> AccountIdentity:
        """Build a verified identity row of one kind."""
        now = utc_now()
        oidc = kind is AccountIdentityKind.OIDC
        return AccountIdentity(
            id=new_id(),
            account_id=account_id,
            kind=kind,
            issuer=claims.issuer if oidc else None,
            subject=claims.subject if oidc else claims.email,
            verified_at=now,
            created_at=now,
            updated_at=now,
        )

    def _store(
        self, identities: list[AccountIdentity], new_account: Account | None = None
    ) -> None:
        """Write the rows atomically; undo and retry when another request won."""
        service = self._service
        written: list[AccountIdentity] = []
        try:
            with service.unit_of_work():
                if new_account is not None:
                    service.accounts.create(new_account)
                for identity in identities:
                    service.identities.create(identity)
                    written.append(identity)
        except ValueError as exc:  # unique index: a concurrent sign-in won
            for identity in written:
                with suppress(KeyError):
                    service.identities.delete(identity.id)
            if new_account is not None:
                with suppress(KeyError):
                    service.accounts.delete(new_account.id)
            raise _LostRace from exc
