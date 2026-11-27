"""T7-I01 to T7-I04 (install, events, lifecycle, Ask) are required controls, wired."""

import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tenancy_harness"
PAIR = json.loads((HARNESS / "pair.json").read_text())
CONTROLS = (
    ("T7-I01", "S1", "slack_install_checks", "slack_install"),
    ("T7-I02", "S2", "slack_event_checks", "slack_events"),
    ("T7-I03", "S7", "slack_lifecycle_checks", "slack_lifecycle"),
    ("T7-I04", "S4", "slack_ask_checks", "slack_ask"),
)


@pytest.mark.parametrize(("control", "slice_", "module_name", "fixture"), CONTROLS)
def test_the_control_is_required_and_wired_end_to_end(
    control: str, slice_: str, module_name: str, fixture: str
) -> None:
    assert control in PAIR["required_controls"]
    assert control not in PAIR["planned_controls"]
    module = importlib.import_module(f"tenancy_harness.phase7.{module_name}")
    assert (
        control,
        slice_,
        fixture,
    ) == (module.CONTROL_ID, module.SLICE, module.FIXTURE)
    assert f"test('{control} " in (HARNESS / "controls.spec.cjs").read_text()
    assert (
        f'seed_module(compose, "phase7.{module_name}")'
        in (HARNESS / "run.py").read_text()
    )


@pytest.mark.parametrize(("control", "slice_", "module_name", "fixture"), CONTROLS)
def test_the_spec_asserts_every_check_the_module_records(
    control: str, slice_: str, module_name: str, fixture: str
) -> None:
    spec = (HARNESS / "controls.spec.cjs").read_text().split(f"test('{control} ")[1]
    spec = spec.split("\ntest(")[0]
    source = (HARNESS / f"phase7/{module_name}.py").read_text()
    asserted = {
        part.split("'")[1]
        for part in spec.replace("\n", " ").split(",")
        if part.count("'") >= 2 and "expect" not in part and "test" not in part
    }
    assert len(asserted) >= 15
    for key in asserted:
        assert key in source, f"{key} is asserted by the spec but never recorded"


def test_the_stack_gives_the_api_the_fake_slack_with_made_up_credentials() -> None:
    compose = (HARNESS / "compose.yml").read_text()
    for line in (
        'WEAVES_SLACK_ENABLED: "true"',
        "WEAVES_SLACK_API_BASE_URL: http://provider:8081/slack/api",
        "WEAVES_SLACK_SIGNING_SECRET: fixture-slack-signing-secret",
        "WEAVES_SLACK_OAUTH_CLIENT_ID: fixture-slack-client-id",
    ):
        assert line in compose
    assert "TENANCY_SLACK_SIGNING_SECRET: fixture-slack-signing-secret" in compose
