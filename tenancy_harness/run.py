"""Run Phase 0 against a pinned pair of real application Docker images."""

import argparse
import json
import os
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
    relay_matches_ledger,
    verify_flow,
)
from tenancy_harness.live_review import review_flow
from tenancy_harness.report import (
    failure_messages,
    specs,
    summarize,
    verify_bundle,
    write_report,
)
from tenancy_harness.run_support import (
    Compose,
    execute,
    setup_private,
    validate_pair,
)

# Controls that need the signed-in frontend itself; --api-only leaves them out.
BROWSER_CONTROLS = "T0-I10|T1-I05|T1-I10|T5-I04"


def browser(
    frontend, harness, private, port, live=False, api_only=False, only=None, repeat=1
):
    """Run actual Chromium against the same-origin disposable application."""
    cli = frontend / "node_modules/@playwright/test/cli.js"
    if not cli.is_file():
        raise RuntimeError("Run npm ci in the frontend checkout first")
    env = {
        "NODE_PATH": str(frontend / "node_modules"),
        "TENANCY_FIXTURE_FILE": str(private / "fixtures.json"),
        "WEAVES_E2E_APP_URL": f"http://localhost:{port}",
        "TENANCY_GOOGLE_DIR": str(private / "google"),
        "TENANCY_BROWSER_OUTPUT": str(private / "browser"),
    }
    extra = []
    if api_only:
        env["TENANCY_API_ONLY"] = "1"
        extra = ["--grep-invert", BROWSER_CONTROLS]
    if only:
        extra += ["--grep", only, "--repeat-each", str(repeat)]
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
                *extra,
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
        compose.run(
            "run",
            "--rm",
            "--no-deps",
            "seed",
            "python",
            "-m",
            "tenancy_harness.canvas_probe",
            "verify",
        )
        report["fake_canvas_provenance"] = True
    if live and report["foundation_passed"] and args.live_slack_review:
        report["live_ai"] = {"passed": False, "stage": "slack_review"}
        try:
            report["live_ai"] = review_flow(
                compose, private, Path.home() / ".config/weaves-testing"
            )
        finally:
            report["testing_budget"] = Ledger(
                Path.home() / ".config/weaves-testing"
            ).snapshot()
            write_report(args.output.resolve(), report)
    elif live and report["foundation_passed"]:
        report["live_ai"] = {"passed": False, "stage": "preparing"}
        try:
            prepare_flow(compose)
            report["live_ai"]["stage"] = "browser"
            raw = browser(frontend, harness, private, port, live=True)
            report["live_ai"].update(
                {"stage": "provenance", "browser_cases": browser_outcomes(raw)}
            )
            report["live_ai"] = verify_flow(compose, private, raw)
            report["live_ai"]["usage_ledger"] = relay_matches_ledger(
                report["live_ai"], private, Path.home() / ".config/weaves-testing"
            )
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


def api_only_outcome(raw):
    """Print per-control outcomes of a partial local run; it can never pass the gate."""
    cases, messages = {}, {}
    for item in specs(raw.get("suites", [])):
        results = [r for t in item["tests"] for r in t.get("results", [])]
        name = item["title"].split()[0]
        cases.setdefault(name, []).extend(r["status"] for r in results)
        if any(r["status"] != "passed" for r in results):
            messages.setdefault(name, []).append(failure_messages(item, results))
    print(
        json.dumps(
            {
                "api_only": True,
                "foundation_passed": False,
                "cases": cases,
                "failure_messages": messages,
            }
        )
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
        # Prices for the model profiles, so the usage ledger can estimate cost.
        config["rates"] = ledger.config["rates"]
        prepare_credentials(private, config, project, envfile, directory)
    return config


def build_images(backend, frontend, values, sources):
    """Build both application images and record their content IDs."""
    for component, path in (("BACKEND", backend), ("FRONTEND", frontend)):
        execute(["docker", "build", "-t", values[f"TEST_{component}_IMAGE"], str(path)])
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


def seed_module(compose, module, *args):
    """Run one harness module inside the disposable seed container."""
    compose.run(
        "run",
        "--rm",
        "--no-deps",
        "seed",
        "python",
        "-m",
        "tenancy_harness." + module,
        *args,
    )


def limits_control(compose):
    """T5-I01: race the queue cap with the worker frozen, then let it drain."""
    compose.run("pause", "worker")
    try:
        seed_module(compose, "phase5.limits_checks", "burst")
    finally:
        compose.run("unpause", "worker")
    seed_module(compose, "phase5.limits_checks", "settle")


def record_correlation_logs(compose, private):
    """Read the API/worker log stream for T5-I05 (only the host can read it)."""
    from tenancy_harness.phase5.correlation_logs import record

    log_path = private / "correlation-service.log"
    with log_path.open("w") as log:
        compose.run("logs", "--no-color", "api", "worker", stdout=log)
    record(log_path, private / "fixtures.json")
    log_path.unlink()


def fair_claim_control(compose):
    """T5-I02 runs against two native workers; the other controls keep one.

    Other controls pause or count on a single worker (T5-I01 pauses ``worker``,
    organization purge advances one deletion), so the second worker exists only
    while this control measures.
    """
    profile = ["--profile", "second-worker"]
    compose.run(*profile, "up", "-d", "worker2")
    try:
        seed_module(compose, "phase5.fair_claim_checks")
    finally:
        compose.run(*profile, "rm", "--stop", "--force", "worker2")


def usage_control(compose):
    """T5-I03: hold a model call open, SIGKILL the worker, restart it, verify.

    The kill happens here because only the runner may touch the Compose project.
    """
    seed_module(compose, "phase5.usage_checks", "start")
    compose.run("kill", "-s", "SIGKILL", "worker")
    compose.run("up", "-d", "worker")
    seed_module(compose, "phase5.usage_checks", "finish")


def slack_controls(compose):
    """T7-I01 install, I02 events, I03 lifecycle, I04 Ask, I05 feedback, I06 review."""
    seed_module(compose, "phase7.slack_install_checks")
    seed_module(compose, "phase7.slack_event_checks")
    seed_module(compose, "phase7.slack_lifecycle_checks")
    seed_module(compose, "phase7.slack_ask_checks")
    seed_module(compose, "phase7.slack_feedback_checks")
    seed_module(compose, "phase7.slack_review_checks")


def run(args, harness):
    """Build, seed, execute, report, and always remove this run's containers."""
    partial = args.api_only or bool(args.only_control)
    if args.live_slack_review_only and os.environ.get("CI"):
        raise RuntimeError("--live-slack-review-only is a local aid, never CI")
    if partial and (os.environ.get("CI") or args.live_anthropic_env):
        raise RuntimeError("--api-only/--only-control are local aids, never CI or live")
    backend, frontend = args.backend.resolve(), args.frontend.resolve()
    pair = json.loads((harness / "pair.json").read_text())
    digest = verify_bundle(harness)
    sources = validate_pair(backend, frontend, args.candidate, args.allow_dirty, pair)
    project = "weaves-tenancy-" + secrets.token_hex(6)
    root = args.output.resolve() / project
    root.mkdir(parents=True, mode=0o700)
    private, envfile, port, values = setup_private(root, harness, project, backend)
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
        build_images(backend, frontend, values, sources)
        if 20 in pair.get("fixture_migrations", []):
            with envfile.open("a") as output:
                output.write(
                    "\nTEST_CATALOG_READER_IMAGE="
                    + json.dumps(sources["backend"]["image_id"])
                    + "\n"
                )
        compose.run("up", "-d", "postgres", "provider", "api", "frontend")
        compose.run("run", "--rm", "--no-deps", "seed")
        compose.run("up", "-d", "worker")
        if live and args.live_slack_review_only:
            return live_review_only(args, compose, private)
        seed_module(compose, "observe")
        seed_module(compose, "lifecycle_checks")
        seed_module(compose, "waitlist_checks")
        limits_control(compose)
        fair_claim_control(compose)
        usage_control(compose)
        seed_module(compose, "phase6.controls_checks")
        slack_controls(compose)
        seed_module(compose, "phase5.correlation_checks")
        record_correlation_logs(compose, private)
        seed_module(compose, "phase5.bounded_queries_checks")
        raw = browser(
            frontend,
            harness,
            private,
            port,
            api_only=args.api_only,
            only=args.only_control,
            repeat=args.repeat_each,
        )
        if partial:
            return api_only_outcome(raw)
        if 20 in pair.get("fixture_migrations", []):
            compose.run(
                "run",
                "--rm",
                "--no-deps",
                "seed",
                "python",
                "-m",
                "tenancy_harness.catalog_ownership",
                "verify",
            )
        fixture = json.loads((private / "fixtures.json").read_text())
        report = summarize(raw, pair, sources, digest, fixture)
        complete_checks(args, compose, frontend, harness, private, port, report, live)
    finally:
        # Service logs stay in the private run directory (never uploaded) so a
        # failing control can be diagnosed after the disposable stack is removed.
        with (private / "service.log").open("w") as log:
            compose.run("logs", "--no-color", "api", "worker", stdout=log, check=False)
        print_dispatch_failures(private / "service.log")
        compose.run("down", "--volumes", "--remove-orphans")


def live_review_only(args, compose, private):
    """Local opt-in: just the one-call live review, without the required controls."""
    directory = Path.home() / ".config/weaves-testing"
    try:
        evidence = review_flow(compose, private, directory)
        print(json.dumps({"live_ai": evidence}, indent=2))
    finally:
        print(json.dumps({"testing_budget": Ledger(directory).snapshot()}, indent=2))


def print_dispatch_failures(path):
    """Print only the error kinds of failed Slack dispatches (never message text)."""
    for line in path.read_text(errors="replace").splitlines():
        if "slack_dispatch_failed" in line or "slack_reply_row_failed" in line:
            print(line.split("|", 1)[-1][-260:])


def main():
    """Accept explicit checkouts, never production endpoints or environment files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--candidate", choices=("backend", "frontend"), required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Local debugging only: omit the signed-in-frontend controls. Never passes "
        "the gate, never writes a report, and is refused when CI is set.",
    )
    parser.add_argument(
        "--only-control",
        help="Local debugging only: run just the controls matching this Playwright "
        "--grep pattern (for example T4-I04), optionally many times in one stack "
        "(--repeat-each). Default off; never passes the gate, refused when CI is set.",
    )
    parser.add_argument("--repeat-each", type=int, default=1)
    parser.add_argument("--live-anthropic-env", type=Path)
    parser.add_argument(
        "--live-slack-review-only",
        action="store_true",
        help="With --live-anthropic-env and --live-slack-review: skip the required "
        "controls and run only the one-call live PR review (local, never CI).",
    )
    parser.add_argument(
        "--live-slack-review",
        action="store_true",
        help="With --live-anthropic-env: run only the one-call PR review live "
        "control (T7 live) instead of the standard three-call live smoke.",
    )
    parser.add_argument("--canvas-probe", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(".local/tenancy"))
    run(parser.parse_args(), Path(__file__).resolve().parent)


if __name__ == "__main__":
    main()
