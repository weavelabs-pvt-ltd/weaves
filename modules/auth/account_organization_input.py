"""Normalized, validated input for account-created organizations."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LOCALE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$")


def _clean(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


@dataclass(frozen=True)
class AccountOrganizationCommand:
    """What the caller asked for, normalized so equal requests compare equal."""

    name: str
    owner_email: str | None = None
    display_name: str | None = None
    timezone: str = "UTC"
    locale: str = "en"

    @classmethod
    def build(
        cls,
        *,
        name: str,
        owner_email: str | None = None,
        display_name: str | None = None,
        timezone: str | None = None,
        locale: str | None = None,
    ) -> AccountOrganizationCommand:
        """Validate and normalize raw input; raise ValueError when unusable."""
        raw_name = (name or "").strip()
        clean_name = " ".join(part for part in raw_name.split(" ") if part)
        if not 1 <= len(clean_name) <= 160 or not raw_name.isprintable():
            raise ValueError("organization name must be 1 to 160 printable characters")
        email = _clean(owner_email)
        email = email.casefold() if email else None
        if email is not None and ("@" not in email or len(email) > 320):
            raise ValueError("owner_email must be a valid email address")
        shown = _clean(display_name)
        if shown is not None and (len(shown) > 160 or not shown.isprintable()):
            raise ValueError("display_name must be at most 160 printable characters")
        zone = _clean(timezone) or "UTC"
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError("timezone must be an IANA time zone name") from exc
        tag = _clean(locale) or "en"
        if len(tag) > 35 or not _LOCALE.match(tag):
            raise ValueError("locale must be a language tag such as en or en-US")
        return cls(clean_name, email, shown, zone, tag)

    def fingerprint(self) -> str:
        """Hash the normalized body; the raw request is never stored."""
        body = json.dumps(
            {
                "name": self.name,
                "owner_email": self.owner_email,
                "display_name": self.display_name,
                "timezone": self.timezone,
                "locale": self.locale,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()
