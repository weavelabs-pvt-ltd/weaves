"""An organization owns its custom email domain.

The first organization created by a person with a verified address on a custom
domain claims that domain on its ``organization_settings`` record. From then on
nobody holding a verified address on that domain may create another
organization: they are expected to be added to the existing one. Public mailbox
providers (gmail.com and the like) are never claimed.

A unique index on the lowercase domain is the backstop. The claim lasts as long
as the settings row, so it is released only when deletion has purged the
organization. Changing or adding domains later is not supported yet.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from application.ports.repository import Repository
from contracts.v1 import Organization, OrganizationSettings
from shared.organization_naming import PUBLIC_EMAIL_DOMAINS
from shared.request_context import system_scope

PUBLIC_DOMAINS_ENV = "WEAVES_PUBLIC_EMAIL_DOMAINS"
ENFORCE_ENV = "WEAVES_ENFORCE_ORGANIZATION_DOMAINS"
DOMAIN_CODE = "domain_belongs_to_organization"


def enforcement_enabled() -> bool:
    """The domain rule is on unless explicitly switched off."""
    value = os.environ.get(ENFORCE_ENV, "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def public_domains() -> frozenset[str]:
    """The built-in public providers plus the operator's comma-separated extras."""
    extra = os.environ.get(PUBLIC_DOMAINS_ENV, "")
    return PUBLIC_EMAIL_DOMAINS | {
        item.strip().lower() for item in extra.split(",") if item.strip()
    }


def custom_domain(email: str) -> str | None:
    """The lowercase domain of an address, or None for a public provider."""
    _, separator, domain = (email or "").strip().lower().rpartition("@")
    if not separator or not domain or domain in public_domains():
        return None
    return domain


def custom_domains(emails: Iterable[str]) -> list[str]:
    """Distinct custom domains of several addresses, in a stable order."""
    return sorted({d for d in map(custom_domain, emails) if d is not None})


@dataclass(frozen=True)
class DomainClaim:
    """Which organization holds a custom email domain."""

    domain: str
    organization_id: str
    organization_name: str


class DomainBelongsToOrganization(Exception):
    """The caller's verified email domain is already claimed."""

    def __init__(self, claim: DomainClaim) -> None:
        """Keep the claim so the caller can name the organization."""
        super().__init__(DOMAIN_CODE)
        self.claim = claim


class OrganizationEmailDomains:
    """Look up and lock custom email domain claims."""

    def __init__(
        self,
        settings: Repository[OrganizationSettings],
        organizations: Repository[Organization],
        lock: Callable[[str], None] | None = None,
    ) -> None:
        """Bind the settings records, organization names and transaction lock."""
        self._settings = settings
        self._organizations = organizations
        self._lock = lock

    def claim_of(self, domain: str) -> DomainClaim | None:
        """Return the organization holding a domain, if any."""
        with system_scope("lifecycle"):
            holders = self._settings.list_by_field(
                "email_domain", domain.lower(), casefold=True
            )
            if not holders:
                return None
            try:
                name = self._organizations.get(holders[0].id).name
            except KeyError:
                name = ""
        return DomainClaim(domain.lower(), holders[0].id, name)

    def first_claim(self, emails: Iterable[str]) -> DomainClaim | None:
        """The first claimed custom domain among a person's verified addresses."""
        if not enforcement_enabled():
            return None
        for domain in custom_domains(emails):
            claim = self.claim_of(domain)
            if claim is not None:
                return claim
        return None

    def reserve(self, verified_emails: Iterable[str], owner_email: str) -> str | None:
        """Decide the new organization's domain inside its creation transaction.

        Every custom domain of the creator is locked before it is checked, so
        two creators on one fresh domain cannot both pass. Raises
        ``DomainBelongsToOrganization`` when any of them is already claimed.
        Returns the owner address's custom domain to claim, or None.
        """
        wanted = custom_domain(owner_email)
        domains = custom_domains([*verified_emails, owner_email])
        claims = {domain: self._locked_claim(domain) for domain in domains}
        if enforcement_enabled():
            taken = next((c for c in claims.values() if c is not None), None)
            if taken is not None:
                raise DomainBelongsToOrganization(taken)
        if wanted is None or claims.get(wanted) is not None:
            return None
        return wanted

    def _locked_claim(self, domain: str) -> DomainClaim | None:
        if self._lock is not None:
            self._lock(f"organization-email-domain:{domain}")
        return self.claim_of(domain)
