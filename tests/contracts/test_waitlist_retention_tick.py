"""The worker's waitlist retention tick, its setting and the client-address rule."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from apps.worker.main import _purge_waitlist
from modules.waitlist.service import DEFAULT_RETENTION_DAYS, retention_days
from tests.support.waitlist_world import client_for, memory_runtime


def test_retention_days_defaults_and_reads_the_environment(monkeypatch):
    monkeypatch.delenv("WEAVES_WAITLIST_RETENTION_DAYS", raising=False)
    assert retention_days() == DEFAULT_RETENTION_DAYS == 365
    monkeypatch.setenv("WEAVES_WAITLIST_RETENTION_DAYS", "30")
    assert retention_days() == 30
    for bad in ("soon", "0", "-5", ""):
        monkeypatch.setenv("WEAVES_WAITLIST_RETENTION_DAYS", bad)
        assert retention_days() in (1, 365)


def test_a_failing_tick_is_logged_and_never_raises(caplog):
    def broken() -> int:
        raise RuntimeError("database went away")

    with caplog.at_level(logging.ERROR, logger="product.worker"):
        _purge_waitlist(SimpleNamespace(purge_waitlist=broken))
    assert "waitlist retention tick failed" in caplog.text
    _purge_waitlist(SimpleNamespace(purge_waitlist=None))
    _purge_waitlist(SimpleNamespace(purge_waitlist=lambda: 3))


def test_a_client_supplied_forwarded_header_does_not_choose_the_rate_limit_source():
    runtime = memory_runtime()
    client = client_for(runtime, "192.0.2.77")
    codes = [
        client.post(
            "/api/v0/waitlist/submissions",
            json={
                "email": f"spoof{index}@acme-corp.test",
                "consent": True,
                "consent_version": "v1",
            },
            headers={"X-Forwarded-For": f"203.0.113.{index}", "X-Real-IP": "9.9.9.9"},
        ).status_code
        for index in range(11)
    ]
    assert codes[:10] == [201] * 10 and codes[10] == 429
    runtime.close()
