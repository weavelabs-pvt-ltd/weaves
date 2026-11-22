"""Organization administration metadata, separate from the stable identity payload."""

from typing import Annotated

from pydantic import Field, StringConstraints

from contracts.v1.base import MutableOrgScopedContract, OpaqueId

OrganizationSlug = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", min_length=3, max_length=40),
]
EmailDomain = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9.-]+$", min_length=3, max_length=253),
]


class OrganizationSettings(MutableOrgScopedContract):
    """One tenant's preferences, explicit human ownership and URL slug.

    ``slug`` is a globally unique lowercase identifier (an identifier only:
    nothing is routed by it). It is None only for an organization whose
    settings predate slugs and were never stored. ``email_domain`` is the
    custom email domain the organization claimed at creation, unique among
    organizations; None when the creator used a public mailbox provider.
    """

    id: OpaqueId
    owner_principal_id: OpaqueId
    timezone: str = Field(default="Asia/Kolkata", min_length=1, max_length=64)
    locale: str = Field(default="en", min_length=2, max_length=35)
    slug: OrganizationSlug | None = None
    email_domain: EmailDomain | None = None
    revision: int = Field(default=1, ge=1)
