"""The foundation must fail on absent fixtures, skipped cases or unsafe targets."""

import copy
import json
import shutil
from pathlib import Path

import pytest

from tenancy_harness.report import failure_locations, summarize, verify_bundle
from tenancy_harness.run_support import Compose, validate_pair
from tenancy_harness.seed import require_disposable_database

HARNESS = Path(__file__).resolve().parents[2] / "tenancy_harness"
PAIR = json.loads((HARNESS / "pair.json").read_text())


def report_inputs():
    cases = PAIR["required_controls"] + list(PAIR["known_defects"])
    raw = {
        "suites": [
            {
                "specs": [
                    {
                        "title": case + " fixture",
                        "tests": [{"results": [{"status": "passed"}]}],
                    }
                    for case in cases
                ]
            }
        ]
    }
    fixture = {"diagnostics": dict.fromkeys(PAIR["known_defects"], True)}
    return raw, fixture


def test_inventory_distinguishes_known_defects_from_saas_readiness():
    raw, fixture = report_inputs()
    report = summarize(raw, PAIR, {}, "fixture-digest", fixture)
    assert report["foundation_passed"]
    assert not report["saas_ready"]
    assert set(report["known_defects"]) == set(PAIR["known_defects"])
    assert "Bearer" not in json.dumps(report)


def test_failure_diagnostics_never_publish_error_text_or_private_paths():
    errors = [
        {
            "status": "failed",
            "errors": [
                {
                    "message": "Bearer private-token with fixture credentials",
                    "location": {
                        "file": "/private/fixture/controls.spec.cjs",
                        "line": 42,
                        "column": 7,
                    },
                }
            ],
        }
    ]
    assert failure_locations(errors) == [
        {"file": "controls.spec.cjs", "line": 42, "column": 7}
    ]
    assert "private" not in json.dumps(failure_locations(errors))


@pytest.mark.parametrize("status", ["skipped", "failed", "timedOut", "interrupted"])
def test_any_nonpassing_required_case_fails_the_inventory(status):
    raw, fixture = report_inputs()
    raw["suites"][0]["specs"][0]["tests"][0]["results"][0]["status"] = status
    assert not summarize(raw, PAIR, {}, "fixture-digest", fixture)["foundation_passed"]


def test_missing_or_duplicate_required_case_cannot_silently_pass():
    raw, fixture = report_inputs()
    missing = copy.deepcopy(raw)
    missing["suites"][0]["specs"].pop()
    assert not summarize(missing, PAIR, {}, "digest", fixture)["foundation_passed"]
    raw["suites"][0]["specs"].append(raw["suites"][0]["specs"][0])
    with pytest.raises(RuntimeError, match="duplicate"):
        summarize(raw, PAIR, {}, "digest", fixture)


def test_missing_browser_results_and_unexpected_diagnostic_behavior_fail():
    raw, fixture = report_inputs()
    raw["suites"][0]["specs"][0]["tests"][0]["results"] = []
    assert not summarize(raw, PAIR, {}, "digest", fixture)["foundation_passed"]
    raw, fixture = report_inputs()
    for item in raw["suites"][0]["specs"]:
        if item["title"].startswith("T1-I04"):
            item["tests"][0]["results"][0]["status"] = "failed"
    assert not summarize(raw, PAIR, {}, "digest", fixture)["foundation_passed"]


@pytest.mark.parametrize(
    "url",
    [
        "",
        "postgresql://x@production/weaves",
        "postgresql://x@postgres/weaves",
        "postgresql://x@postgres/weaves_tenancy_test",
        "postgresql://tenancy_test@postgres/weaves_tenancy_test?host=production",
    ],
)
def test_privileged_fixture_rejects_any_nonfixture_database(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(RuntimeError, match="fixture"):
        require_disposable_database()


def test_disposable_database_guard_accepts_only_its_explicit_identity(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://tenancy_test:fake@postgres/weaves_tenancy_test"
    )
    require_disposable_database()


@pytest.mark.parametrize(
    "project", ["weaves-single", "weaves", "", "weaves-tenancy-prod"]
)
def test_cleanup_cannot_target_the_actual_deployment(tmp_path, project):
    with pytest.raises(ValueError, match="Unsafe"):
        Compose(project, tmp_path, tmp_path / "env")


def test_portable_bundle_lock_catches_missing_and_modified_sources(tmp_path):
    assert verify_bundle(HARNESS)
    (tmp_path / "bundle.lock.json").write_text(
        (HARNESS / "bundle.lock.json").read_text()
    )
    with pytest.raises(RuntimeError, match="inventory"):
        verify_bundle(tmp_path)
    for source in HARNESS.iterdir():
        if source.is_file():
            shutil.copy2(source, tmp_path / source.name)
    (tmp_path / "pair.json").write_text("{}")
    with pytest.raises(RuntimeError, match="digest"):
        verify_bundle(tmp_path)


def test_ci_cannot_bypass_pinned_clean_sources(monkeypatch):
    monkeypatch.setenv("CI", "true")
    with pytest.raises(RuntimeError, match="CI cannot bypass"):
        validate_pair(Path("."), Path("."), "backend", True, PAIR)


def test_counterpart_cannot_float_to_an_unpinned_revision(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(
        "tenancy_harness.run_support.revision",
        lambda path: {"revision": "0" * 40, "dirty": False},
    )
    with pytest.raises(RuntimeError, match="counterpart must match"):
        validate_pair(Path("backend"), Path("frontend"), "backend", False, PAIR)
