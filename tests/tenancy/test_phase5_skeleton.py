"""Phase 5 control ids are declared as planned, never silently required."""

import importlib
import json
from pathlib import Path

from tenancy_harness.phase5 import PLANNED

PAIR = json.loads(
    (Path(__file__).resolve().parents[2] / "tenancy_harness/pair.json").read_text()
)


def test_planned_controls_are_declared_but_not_required() -> None:
    # A control is planned until its slice lands, then it is required instead.
    landed = set(PAIR["required_controls"]) & set(PLANNED)
    # Phase 7 (Slack) declares its own planned controls; see test_slack_selftest.
    phase5_planned = {
        cid: slice_ for cid, (slice_, _) in PLANNED.items() if cid not in landed
    }
    assert {
        cid: slice_
        for cid, slice_ in PAIR["planned_controls"].items()
        if not cid.startswith("T7-")
    } == phase5_planned
    assert "T5-I02" in landed  # slice 5.2
    assert not set(PAIR["planned_controls"]) & set(PAIR["required_controls"])
    assert not set(PAIR["planned_controls"]) & set(PAIR["known_defects"])


def test_each_planned_control_has_one_owning_module() -> None:
    modules = [module for _, module in PLANNED.values()]
    assert len(set(modules)) == len(modules)
    for cid, (slice_, module) in PLANNED.items():
        loaded = importlib.import_module(f"tenancy_harness.phase5.{module}")
        assert (cid, slice_) == (loaded.CONTROL_ID, loaded.SLICE)


def test_t5_i05_is_required_and_its_module_loads() -> None:
    assert "T5-I05" in PAIR["required_controls"]
    assert "T5-I05" not in PAIR["planned_controls"]
    loaded = importlib.import_module("tenancy_harness.phase5.correlation_checks")
    assert (loaded.CONTROL_ID, loaded.SLICE) == ("T5-I05", "5.5")
