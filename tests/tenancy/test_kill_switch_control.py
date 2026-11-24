"""T6-I03 is a required real-stack control with its module, spec and runner hook."""

import importlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tenancy_harness"
PAIR = json.loads((HARNESS / "pair.json").read_text())


def test_t6_i03_is_required() -> None:
    assert "T6-I03" in PAIR["required_controls"]
    assert "T6-I03" not in PAIR["planned_controls"]


def test_control_module_spec_and_runner_are_wired() -> None:
    module = importlib.import_module("tenancy_harness.phase6.controls_checks")
    assert (module.CONTROL_ID, module.SLICE) == ("T6-I03", "6.4")
    spec = (HARNESS / "controls.spec.cjs").read_text()
    assert "test('T6-I03 " in spec and f"fixture.{module.FIXTURE}" in spec
    assert (
        'seed_module(compose, "phase6.controls_checks")'
        in (HARNESS / "run.py").read_text()
    )


def test_operator_scripts_are_mounted_into_the_seed_container() -> None:
    compose = (HARNESS / "compose.yml").read_text()
    assert "/suite/operator/platform_control.py:ro" in compose
    assert "/suite/operator/organization_lifecycle.py:ro" in compose
    assert "TEST_PLATFORM_CONTROL_SCRIPT" in (HARNESS / "run_support.py").read_text()


def test_the_spec_asserts_the_key_results_the_module_records() -> None:
    source = (HARNESS / "phase6/controls_checks.py").read_text()
    spec = (HARNESS / "controls.spec.cjs").read_text().split("T6-I03")[1]
    for key in (
        "read_only_refuses_writes_with_a_clear_503",
        "audit_is_append_only",
        "b_rows_untouched",
        "switches_need_no_restart",
    ):
        assert f"'{key}'" in spec
        assert key in source
