"""Waitlist email normalization and the company-or-public domain type."""

from __future__ import annotations

import hashlib
import re

from contracts.v1 import WaitlistDomainType
from modules.waitlist.errors import InvalidInput
from shared.organization_naming import public_email_domains

MAX_EMAIL_LENGTH = 254
_LOCAL = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}$")
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_email(raw: str) -> str:
    """Trim and lower-case an address, or raise ``ValueError`` when malformed.

    The check is only a shape check (one ``@``, a dotted ASCII domain, a sane
    local part). Nothing is sent to the address, so deliverability is not tested.
    """
    email = raw.strip().lower()
    if not email or len(email) > MAX_EMAIL_LENGTH:
        raise InvalidInput("Enter a valid email address")
    local, separator, domain = email.rpartition("@")
    labels = domain.split(".")
    valid = (
        separator == "@"
        and _LOCAL.match(local) is not None
        and not local.startswith(".")
        and not local.endswith(".")
        and ".." not in local
        and len(labels) >= 2
        and all(_LABEL.match(label) for label in labels)
        and not labels[-1].isdigit()
        and len(labels[-1]) >= 2
    )
    if not valid:
        raise InvalidInput("Enter a valid email address")
    return email


def domain_of(email: str) -> str:
    """The domain part of a normalized address."""
    return email.rpartition("@")[2]


def domain_type(domain: str) -> WaitlistDomainType:
    """Public mailbox provider (shared list plus operator extras) or company."""
    if domain in public_email_domains():
        return WaitlistDomainType.PUBLIC
    return WaitlistDomainType.COMPANY


def entry_id(email: str) -> str:
    """The entry identifier: derived from the email, so one email is one record."""
    digest = hashlib.sha256(f"weaves-waitlist-entry-v1\0{email}".encode())
    return "wle_" + digest.hexdigest()[:40]
