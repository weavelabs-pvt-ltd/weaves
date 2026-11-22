"""Fixed name lists for organization slugs and email domains.

They live here, in one place, because both the auth module and the database
migration that backfills existing organizations must apply the same lists.
"""

from __future__ import annotations

# Slugs no organization may take.
RESERVED_SLUGS = frozenset(
    {
        "admin",
        "api",
        "app",
        "www",
        "weaves",
        "support",
        "help",
        "login",
        "signin",
        "signup",
        "account",
        "settings",
        "static",
        "assets",
        "new",
    }
)

# Mailbox providers anyone can sign up with: owning one claims nothing.
PUBLIC_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "yahoo.co.in",
        "yahoo.in",
        "yahoo.co.uk",
        "yahoo.co.jp",
        "yahoo.ca",
        "yahoo.com.au",
        "yahoo.com.br",
        "yahoo.de",
        "yahoo.fr",
        "yahoo.es",
        "yahoo.it",
        "ymail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "aol.com",
        "zoho.com",
        "zohomail.com",
        "gmx.com",
        "gmx.net",
        "gmx.de",
        "mail.com",
        "yandex.com",
        "yandex.ru",
        "fastmail.com",
        "hey.com",
        "tutanota.com",
        "tuta.io",
        "rediffmail.com",
        "qq.com",
        "163.com",
        "126.com",
        "naver.com",
    }
)
