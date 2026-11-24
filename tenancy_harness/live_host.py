"""Opt-in live-provider orchestration; credentials never enter command arguments."""

import json
import shlex
import sqlite3


def load_config(path):
    """Read only the explicitly supplied local test key, without shell evaluation."""
    if path is None:
        return None
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, raw = line.split("=", 1)
        parts = shlex.split(raw, comments=True)
        values[name.strip()] = parts[0] if parts else ""
    if values.get("WEAVES_TEST_PROVIDER") != "anthropic":
        raise ValueError(
            "Live smoke currently requires the explicit Anthropic provider"
        )
    key = values.get("WEAVES_TEST_API_KEY", "")
    if not key or key == "your_actual_key":
        raise ValueError("Local live testing key is missing")
    model = values.get("WEAVES_TEST_MODEL")
    return {
        "api_key": key,
        "solo_model": model or "claude-haiku-5-5",
        "workflow_model": model or "claude-sonnet-5-5",
        "max_model_calls": 3,
    }


def prepare_credentials(private, config, run_id, envfile, budget_directory):
    """Pass live credentials only through the private disposable seed mount."""
    path = private / "live-config.json"
    path.write_text(json.dumps({**config, "run_id": run_id}))
    path.chmod(0o600)
    with envfile.open("a") as output:
        output.write("\nTEST_BUDGET_DIR=" + json.dumps(str(budget_directory)) + "\n")


def prepare_flow(compose):
    """Create bounded live work using the real API and independent worker."""
    compose.run(
        "run",
        "--rm",
        "--no-deps",
        "seed",
        "python",
        "-m",
        "tenancy_harness.live",
        "prepare",
    )


def browser_outcomes(browser_report):
    """Reject missing, skipped, duplicate or failed workflow browser cases."""
    from tenancy_harness.report import specs

    cases = specs(browser_report.get("suites", []))
    expected = {"LIVE-I01", "LIVE-I02", "LIVE-I03", "LIVE-I04"}
    observed = {}
    for case in cases:
        results = [r for test in case["tests"] for r in test.get("results", [])]
        observed[case["title"].split()[0]] = bool(results) and all(
            result["status"] == "passed" for result in results
        )
    if len(cases) != 4 or set(observed) != expected or not all(observed.values()):
        raise RuntimeError("Live browser verification failed; inspect private report")
    return observed


def verify_flow(compose, private, browser_report):
    """Require successful browser actions and independently verify durable evidence."""
    observed = browser_outcomes(browser_report)
    compose.run(
        "run",
        "--rm",
        "--no-deps",
        "seed",
        "python",
        "-m",
        "tenancy_harness.live",
        "verify",
    )
    evidence = json.loads((private / "live-evidence.json").read_text())
    evidence["browser_cases"] = observed
    return evidence


def relay_matches_ledger(evidence, private, directory):
    """The budget relay's recorded usage equals the product usage ledger, per call.

    The relay settles each real request from the provider's own usage block; the
    product settles each run from the same response. Both prices are functions of
    those tokens, so the relay's settled amounts must equal the amounts computed
    from the ledger's tokens (the relay applies its conservative margin).
    """
    from tenancy_harness.budget import Ledger, usage_cost

    run_id = json.loads((private / "live-config.json").read_text())["run_id"]
    budget = Ledger(directory)
    with sqlite3.connect(budget.path) as db:
        relay = sorted(
            (model, actual)
            for model, actual in db.execute(
                "SELECT model, actual_nanos FROM requests "
                "WHERE run_id=? AND status='settled'",
                (run_id,),
            )
        )
    calls = evidence["verified_model_calls"]
    expected = sorted(
        (
            call["model"],
            usage_cost(
                budget.config,
                call["model"],
                {
                    "input_tokens": call["ledger_input_tokens"],
                    "output_tokens": call["ledger_output_tokens"],
                },
            ),
        )
        for call in calls
    )
    if relay != expected:
        raise AssertionError("Relay usage differs from the usage ledger")
    margin = int(budget.config["accounting_multiplier"])
    for call in calls:
        # The ledger's own cost estimate is the provider price; the relay's is
        # the same price times its accounting margin (rounded up).
        if call["ledger_cost_nano_usd"] is None or not (
            0
            <= usage_cost(
                budget.config,
                call["model"],
                {
                    "input_tokens": call["ledger_input_tokens"],
                    "output_tokens": call["ledger_output_tokens"],
                },
            )
            - call["ledger_cost_nano_usd"] * margin
            <= margin
        ):
            raise AssertionError("Ledger cost is not the relay's cost without margin")
    return {
        "relay_settled_calls": len(relay),
        "relay_equals_ledger": True,
        "calls": [
            {
                "model": call["model"],
                "ledger_input_tokens": call["ledger_input_tokens"],
                "ledger_output_tokens": call["ledger_output_tokens"],
                "ledger_cost_nano_usd": call["ledger_cost_nano_usd"],
            }
            for call in calls
        ],
    }
