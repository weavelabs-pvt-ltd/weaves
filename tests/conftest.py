"""Suite-wide defaults."""

import pytest


@pytest.fixture(autouse=True)
def fixture_email_domains_are_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat the shared fixture domain as a public mailbox provider.

    Most tests create several organizations from ``@example.test`` accounts.
    Without this, the first would claim the domain and refuse the rest. Tests
    of the domain rule itself use other, custom domains.
    """
    monkeypatch.setenv("WEAVES_PUBLIC_EMAIL_DOMAINS", "example.test")
