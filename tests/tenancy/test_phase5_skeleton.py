"""Phase 5 control ids are declared as planned, never silently required."""

import importlib
import json
from pathlib import Path

from tenancy_harness.phase5 import PLANNED

PAIR = json.loads(
    (Path(__file__).resolve().parents[2] / "tenancy_harness/pair.json").read_text()
)


def test_planned_controls_are_declared_but_not_required() -> None:
    assert PAIR["planned_controls"] == {
        cid: slice_ for cid, (slice_, _) in PLANNED.items()
    }
    assert not set(PAIR["planned_controls"]) & set(PAIR["required_controls"])
    assert not set(PAIR["planned_controls"]) & set(PAIR["known_defects"])


def test_each_planned_control_has_one_owning_module() -> None:
    modules = [module for _, module in PLANNED.values()]
    assert len(set(modules)) == len(modules)
    for cid, (slice_, module) in PLANNED.items():
        loaded = importlib.import_module(f"tenancy_harness.phase5.{module}")
        assert (cid, slice_) == (loaded.CONTROL_ID, loaded.SLICE)
