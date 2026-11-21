"""CSRF tokens are compared safely for the account browser cookie."""

import pytest

from modules.auth.http.browser_sessions import csrf_valid


@pytest.mark.parametrize("supplied", ["é" * 64, "", "wrong", "0" * 65])
def test_malformed_csrf_is_rejected_without_exception(monkeypatch, supplied):
    monkeypatch.setenv("WEAVES_BROWSER_SESSION_SECRET", "browser-secret-" * 4)
    assert not csrf_valid("session", supplied)
