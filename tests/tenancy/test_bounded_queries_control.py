"""T5-I04 is a required real-stack control with its module, spec and runner hook."""

import importlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tenancy_harness"
PAIR = json.loads((HARNESS / "pair.json").read_text())


def test_t5_i04_is_required_and_no_longer_planned() -> None:
    assert "T5-I04" in PAIR["required_controls"]
    assert "T5-I04" not in PAIR["planned_controls"]


def test_control_module_spec_and_runner_are_wired() -> None:
    module = importlib.import_module("tenancy_harness.phase5.bounded_queries_checks")
    assert (module.CONTROL_ID, module.SLICE) == ("T5-I04", "5.4")
    spec = (HARNESS / "controls.spec.cjs").read_text()
    assert "test('T5-I04 " in spec and "bounded_queries_ui.cjs" in spec
    assert (
        'seed_module(compose, "phase5.bounded_queries_checks")'
        in (HARNESS / "run.py").read_text()
    )
    assert "T5-I04" in (HARNESS / "run.py").read_text().split("BROWSER_CONTROLS")[1]
