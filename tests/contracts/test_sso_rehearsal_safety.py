"""Rollback rehearsals must never connect to the production database."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra/gcp/singlevm"))
import verify_sso_rollback as rehearsal


@pytest.mark.parametrize("url", ["postgresql://localhost/weaves", "dbname=postgres"])
def test_rehearsal_rejects_non_disposable_databases_before_opening_them(
    monkeypatch, tmp_path, url
):
    def unexpected_database(*args, **kwargs):
        pytest.fail("A non-disposable database must never be opened")

    monkeypatch.setattr(rehearsal, "ProductPostgresStore", unexpected_database)
    with pytest.raises(ValueError, match="disposable weaves_rollback_test"):
        rehearsal.rehearse(tmp_path, url)
