"""Organization URL slugs: validation, global uniqueness and suggestions.

A slug is a short lowercase identifier shown as ``weaves.app/<slug>``. It is an
identifier only: nothing is routed by it, and it grants nothing. It lives on the
tenant-owned ``organization_settings`` record; a unique index on its lowercase
form enforces global uniqueness. A slug stays reserved while its settings row
exists, so it is freed only when deletion has purged the organization.

Answers here never name the organization that holds a slug.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import OrganizationSettings
from shared.organization_naming import RESERVED_SLUGS
from shared.request_context import system_scope

SLUG_MIN, SLUG_MAX = 3, 40
_SHAPE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_NOT_SLUG = re.compile(r"[^a-z0-9]+")
_NUMBERED_SUGGESTIONS = 50
INVALID_DETAIL = (
    "slug must be 3 to 40 lowercase letters, digits or single hyphens, "
    "and must not start or end with a hyphen"
)


class SlugRejected(ValueError):
    """A requested slug is malformed or reserved."""

    def __init__(self, slug: str, reason: str) -> None:
        """Keep the cleaned input and why it was refused."""
        detail = INVALID_DETAIL if reason == "invalid" else "slug is reserved"
        super().__init__(detail)
        self.slug = slug
        self.reason = reason
        self.suggestion: str | None = None


class SlugTaken(Exception):
    """A requested slug already belongs to an organization."""

    def __init__(self, slug: str, suggestion: str | None) -> None:
        """Keep the slug and the first free alternative."""
        super().__init__("slug is already taken")
        self.slug = slug
        self.reason = "taken"
        self.suggestion = suggestion


def clean_slug(value: str) -> str:
    """Trim and lowercase raw input without judging it."""
    return (value or "").strip().lower()


def normalize_slug(value: str) -> str:
    """Return the canonical slug or raise ``SlugRejected``."""
    slug = clean_slug(value)
    if not SLUG_MIN <= len(slug) <= SLUG_MAX or not _SHAPE.match(slug):
        raise SlugRejected(slug[:64], "invalid")
    if slug in RESERVED_SLUGS:
        raise SlugRejected(slug, "reserved")
    return slug


def slugify(text: str) -> str:
    """Derive a well-formed slug from free text (migration 23 uses the same rule)."""
    base = _NOT_SLUG.sub("-", (text or "").lower()).strip("-")[:SLUG_MAX].strip("-")
    if len(base) < SLUG_MIN:
        base = f"{base}-org".strip("-")
    return base


def _candidates(base: str) -> Iterator[str]:
    """``<base>-team``, then ``<base>-2``, ``-3`` and so on, each within length."""
    suffixes = ["-team", *(f"-{n}" for n in range(2, _NUMBERED_SUGGESTIONS + 2))]
    for suffix in suffixes:
        yield base[: SLUG_MAX - len(suffix)].strip("-") + suffix


class OrganizationSlugs:
    """Check, suggest and reserve globally unique organization slugs."""

    def __init__(
        self,
        settings: Repository[OrganizationSettings],
        lock: Callable[[str], None] | None = None,
    ) -> None:
        """Bind the settings records and the transaction-scoped lock."""
        self._settings = settings
        self._lock = lock

    def _free(self, slug: str, own_org_id: str | None = None) -> bool:
        if slug in RESERVED_SLUGS:
            return False
        with system_scope("lifecycle"):
            holders = self._settings.list_by_field("slug", slug, casefold=True)
        return all(holder.id == own_org_id for holder in holders)

    def suggestion(self, text: str, own_org_id: str | None = None) -> str | None:
        """The first free alternative for a refused slug, or None."""
        candidates = _candidates(slugify(text))
        return next((c for c in candidates if self._free(c, own_org_id)), None)

    def check(self, raw: str) -> dict[str, Any]:
        """Report whether a slug could be used, naming no organization."""
        try:
            slug = normalize_slug(raw)
        except SlugRejected as exc:
            return self._answer(exc.slug, False, exc.reason, self._alternative(raw))
        if self._free(slug):
            return self._answer(slug, True, None, None)
        return self._answer(slug, True, "taken", self.suggestion(slug))

    @staticmethod
    def _answer(
        slug: str, valid: bool, reason: str | None, suggestion: str | None
    ) -> dict[str, Any]:
        return {
            "slug": slug,
            "valid": valid,
            "available": valid and reason is None,
            "suggestion": suggestion,
            "reason": reason,
        }

    def _alternative(self, raw: str, own_org_id: str | None = None) -> str | None:
        """A usable slug close to malformed or reserved input."""
        base = slugify(raw)
        if self._free(base, own_org_id):
            return base
        return self.suggestion(base, own_org_id)

    def explain(self, exc: SlugRejected, own_org_id: str | None = None) -> SlugRejected:
        """Attach a suggestion to a rejection before it is reported."""
        exc.suggestion = self._alternative(exc.slug, own_org_id)
        return exc

    def reserve(self, slug: str, own_org_id: str | None = None) -> str:
        """Hold an exact slug until the transaction ends or raise ``SlugTaken``."""
        if not self._hold(slug, own_org_id):
            raise SlugTaken(slug, self.suggestion(slug, own_org_id))
        return slug

    def reserve_derived(self, name: str) -> str:
        """Hold the slug derived from a name, or its first free alternative."""
        base = slugify(name)
        for candidate in (base, *_candidates(base)):
            if self._hold(candidate, None):
                return candidate
        raise SlugTaken(base, None)

    def _hold(self, slug: str, own_org_id: str | None) -> bool:
        """Lock one slug for this transaction, then report whether it is free.

        Every writer locks the exact slug before checking it, so two
        transactions cannot both see it free; the unique index is the backstop.
        """
        if self._lock is not None:
            self._lock(f"organization-slug:{slug}")
        return self._free(slug, own_org_id)
