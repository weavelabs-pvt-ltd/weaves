"""Fail when the data-model documentation no longer matches the code."""

import importlib.util
import re
from pathlib import Path

from infrastructure.persistence.product_repositories import REPOSITORY_SPECS

ROOT = Path(__file__).resolve().parents[2]


def _generator():
    spec = importlib.util.spec_from_file_location(
        "export_data_model", ROOT / "scripts" / "export_data_model.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_data_model_is_current():
    generator = _generator()
    assert generator.OUTPUT.read_text() == generator.render(), (
        "docs/data-model/generated.md is stale; run scripts/export_data_model.py"
    )


def test_handwritten_catalogue_names_every_collection():
    text = (ROOT / "docs" / "DATA_MODEL.md").read_text()
    documented = set(re.findall(r"`([a-z_]+)`", text))
    missing = {spec[1] for spec in REPOSITORY_SPECS} - documented
    assert not missing, f"DATA_MODEL.md does not mention: {sorted(missing)}"
