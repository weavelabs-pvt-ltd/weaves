"""Run Phase 0 against a pinned pair of real application Docker images."""

import argparse
import json
import secrets
import subprocess
from contextlib import suppress
from pathlib import Path

from tenancy_harness.budget import BudgetExhausted, Ledger
from tenancy_harness.live_host import (
    browser_outcomes,
    load_config,
    prepare_credentials,
    prepare_flow,
    verify_flow,
)
from tenancy_harness.report import summarize, verify_bundle, write_report
from tenancy_harness.run_support import (
    Compose,
    execute,
    setup_private,
    validate_pair,
)


def browser(frontend, harness, private, port, live=False):
    """Run actual Chromium against the same-origin disposable application."""
    cli = frontend / "node_modules/@playwright/test/cli.js"
    if not cli.is_file():
        raise RuntimeError("Run npm ci in the frontend checkout first")
    env = {
        "NODE_PATH": str(frontend / "node_modules"),
        "TENANCY_FIXTURE_FILE": str(private / "fixtures.json"),
        "WEAVES_E2E_APP_URL": f"http://127.0.0.1:{port}",
        "TENANCY_BROWSER_OUTPUT": str(private / "browser"),
    }
    with (
        (private / ("live-browser.json" if live else "browser.json")).open(
            "w"
        ) as output,
        suppress(subprocess.CalledProcessError),
    ):
        execute(
            [
                "node",
                str(cli),
                "test",
                "--config",
                str(harness / ("live.config.cjs" if live else "playwright.config.cjs")),
                "--reporter=json",
            ],
            env=env,
            stdout=output,
        )
    return json.loads(
        (private / ("live-browser.json" if live else "browser.json")).read_text()
    )


def complete_checks(args, compose, frontend, harness, private, port, report, live):
    """Run optional paid checks only through the persistent lifetime-budget relay."""
    if args.canvas_probe:
        compose.run(
            "run",
            "--rm",
            "--no-deps",
            "seed",
            "python",
            "-m",
            "tenancy_harness.canvas_probe",
        )
        report["fake_canvas_browser"] = browser_outcomes(
            browser(frontend, harness, private, port, live=True)
        )
    if live and report["foundation_passed"]:
        report["live_ai"] = {"passed": False, "stage": "preparing"}
        try:
            prepare_flow(compose)
            report["live_ai"]["stage"] = "browser"
            raw = browser(frontend, harness, private, port, live=True)
            report["live_ai"].update(
                {"stage": "provenance", "browser_cases": browser_outcomes(raw)}
            )
            report["live_ai"] = verify_flow(compose, private, raw)
        finally:
            report["testing_budget"] = Ledger(
                Path.home() / ".config/weaves-testing"
            ).snapshot()
            write_report(args.output.resolve(), report)
    write_report(args.output.resolve(), report)
    print(json.dumps(report, indent=2))
    if not report["foundation_passed"]:
        raise RuntimeError(
            "Required tenancy inventory failed; inspect private browser output"
        )


def live_configuration(args, private, project, envfile):
    """Require a funded existing ledger before enabling networked live testing."""
    config = load_config(args.live_anthropic_env)
    if config:
        directory = Path.home() / ".config/weaves-testing"
        ledger = Ledger(directory)
        if float(ledger.snapshot()["available_usd"]) < 1:
            raise BudgetExhausted(
                "Ask the user before increasing the lifetime test budget"
            )
        prepare_credentials(private, config, project, envfile, directory)
    return config


def run(args, harness):
    """Build, seed, execute, report, and always remove this run's containers."""
    backend, frontend = args.backend.resolve(), args.frontend.resolve()
    pair = json.loads((harness / "pair.json").read_text())
    digest = verify_bundle(harness)
    sources = validate_pair(backend, frontend, args.candidate, args.allow_dirty, pair)
    project = "weaves-tenancy-" + secrets.token_hex(6)
    root = args.output.resolve() / project
    root.mkdir(parents=True, mode=0o700)
    private, envfile, port, values = setup_private(root, harness, project)
    live = live_configuration(args, private, project, envfile)
    compose = Compose(project, harness, envfile, live=bool(live))
    write_report(
        args.output.resolve(),
        {
            "phase": 0,
            "foundation_passed": False,
            "saas_ready": False,
            "sources": sources,
            "stage": "build_or_fixture_setup",
        },
    )
    try:
        for component, path in (("BACKEND", backend), ("FRONTEND", frontend)):
            execute(
                ["docker", "build", "-t", values[f"TEST_{component}_IMAGE"], str(path)]
            )
            sources[component.lower()]["image_id"] = execute(
                [
                    "docker",
                    "image",
                    "inspect",
                    values[f"TEST_{component}_IMAGE"],
                    "--format",
                    "{{.Id}}",
                ],
                capture_output=True,
                text=True,
            ).stdout.strip()
        compose.run("up", "-d", "postgres", "provider", "api", "frontend")
        compose.run("run", "--rm", "--no-deps", "seed")
        compose.run("up", "-d", "worker")
        compose.run(
            "run",
            "--rm",
            "--no-deps",
            "seed",
            "python",
            "-m",
            "tenancy_harness.observe",
        )
        raw = browser(frontend, harness, private, port)
        fixture = json.loads((private / "fixtures.json").read_text())
        report = summarize(raw, pair, sources, digest, fixture)
        complete_checks(args, compose, frontend, harness, private, port, report, live)
    finally:
        compose.run("down", "--volumes", "--remove-orphans")


def main():
    """Accept explicit checkouts, never production endpoints or environment files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--candidate", choices=("backend", "frontend"), required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--live-anthropic-env", type=Path)
    parser.add_argument("--canvas-probe", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(".local/tenancy"))
    run(parser.parse_args(), Path(__file__).resolve().parent)


if __name__ == "__main__":
    main()
