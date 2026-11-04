#!/usr/bin/env python3
"""Run a versioned evaluation suite against a running Weaves API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import ValidationError

from modules.evaluations.scoring import (
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationReport,
    EvaluationSuite,
    score_evaluation_case,
)


def _run_case(
    base_url: str,
    token: str | None,
    suite: EvaluationSuite,
    case: EvaluationCase,
    evaluation_id: str,
) -> EvaluationCaseResult:
    body = json.dumps(
        {
            "agent_id": suite.agent_id,
            "title": f"Evaluation {case.case_id}"[:160],
            "initial_message": case.task,
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers["Idempotency-Key"] = f"weaves-eval-{evaluation_id[:16]}-{case.case_id}"
    request = Request(
        f"{base_url.rstrip('/')}/api/v0/tasks",
        data=body,
        headers=headers,
        method="POST",
    )
    started = time.monotonic()
    try:
        with urlopen(request, timeout=180) as response:
            task = json.loads(response.read())
        task_id = task["task_id"]
        deadline = started + 180
        while time.monotonic() < deadline:
            task = _api_json_request(
                base_url,
                token,
                f"/api/v0/tasks/{task_id}",
                method="GET",
            )
            if task.get("latest_turn_status") in {"succeeded", "failed", "cancelled"}:
                messages = _api_json_request(
                    base_url,
                    token,
                    f"/api/v0/tasks/{task_id}/messages",
                    method="GET",
                )
                return score_task_response(
                    case,
                    task,
                    messages,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            time.sleep(0.5)
        return score_evaluation_case(
            case,
            status="timeout",
            run_id=task_id,
            request_error="Task did not finish within the evaluation timeout.",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except HTTPError as exc:
        return score_evaluation_case(
            case,
            status=None,
            run_id=None,
            request_error=f"Task API returned HTTP {exc.code}.",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
        return score_evaluation_case(
            case,
            status=None,
            run_id=None,
            request_error="Task API request failed or returned invalid JSON.",
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def score_task_response(
    case: EvaluationCase,
    task: dict[str, Any],
    messages: dict[str, Any],
    *,
    duration_ms: int,
) -> EvaluationCaseResult:
    """Map a task transcript into stable output and evidence checks."""
    assistant_messages = tuple(
        item["content"]
        for item in messages.get("items", [])
        if item.get("role") == "assistant" and isinstance(item.get("content"), str)
    )
    text = "\n".join(assistant_messages)
    source_text = text.partition("Retrieved sources:")[2]
    cited_sources = tuple(
        token.strip(" ,;[]()")
        for token in source_text.split()
        if ":" in token and token.strip(" ,;[]()")
    )
    return score_evaluation_case(
        case,
        status=task.get("latest_turn_status"),
        run_id=task.get("task_id"),
        summaries=assistant_messages,
        cited_sources=cited_sources,
        duration_ms=duration_ms,
    )


def _api_json_request(
    base_url: str,
    token: str | None,
    path: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers: dict[str, str] = {"Accept": "application/json"}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    with urlopen(request, timeout=30) as response:
        result = json.loads(response.read())
    if not isinstance(result, dict):
        raise ValueError("product API returned an invalid JSON object")
    return result


def _load_suite(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> EvaluationSuite | None:
    """Load a local or saved suite, reporting input and API errors."""
    if (args.suite is None) == (args.suite_id is None):
        parser.error("provide exactly one suite file or --suite-id")
    try:
        if args.suite_id:
            saved = _api_json_request(
                args.base_url,
                args.token,
                f"/api/v0/evaluation-suites/{args.suite_id}",
                method="GET",
            )
            suite = EvaluationSuite.model_validate_json(json.dumps(saved["definition"]))
        else:
            suite = EvaluationSuite.model_validate_json(args.suite.read_text())
    except (
        OSError,
        ValidationError,
        HTTPError,
        URLError,
        TimeoutError,
        ValueError,
    ) as exc:
        print(f"Could not load evaluation suite: {exc}", file=sys.stderr)
        return None

    return suite


def _persist_report(
    args: argparse.Namespace, suite: EvaluationSuite, report: EvaluationReport
) -> bool:
    """Persist the completed report when requested and return success."""
    if not args.persist_report:
        return True
    try:
        _api_json_request(
            args.base_url,
            args.token,
            "/api/v0/evaluation-reports",
            method="POST",
            payload={
                "definition": suite.model_dump(mode="json"),
                "report": report.model_dump(mode="json"),
            },
        )
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, ValueError) as exc:
        print(f"Could not persist evaluation report: {exc}", file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "suite",
        type=Path,
        nargs="?",
        help="Path to a version 1 JSON suite (use this or --suite-id)",
    )
    parser.add_argument("--suite-id", help="Run a suite saved in the product API")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("WEAVES_API_URL", "http://localhost:8001"),
    )
    parser.add_argument("--token", default=os.environ.get("WEAVES_API_TOKEN"))
    parser.add_argument(
        "--output", type=Path, help="Write the report to this JSON file"
    )
    parser.add_argument(
        "--persist-report",
        action="store_true",
        help="Store the scored report in the product API after the run",
    )
    args = parser.parse_args()

    suite = _load_suite(args, parser)
    if suite is None:
        return 2

    evaluation_id = uuid4().hex[:16]
    results = tuple(
        _run_case(args.base_url, args.token, suite, case, evaluation_id)
        for case in suite.cases
    )
    report = EvaluationReport(
        suite_id=suite.suite_id,
        evaluation_id=evaluation_id,
        passed_cases=sum(result.passed for result in results),
        total_cases=len(results),
        pass_rate=sum(result.passed for result in results) / len(results),
        results=results,
    )
    serialized = report.model_dump_json(indent=2)
    if args.output:
        args.output.write_text(serialized + "\n")
    else:
        print(serialized)
    print(
        f"{report.passed_cases}/{report.total_cases} cases passed "
        f"({report.pass_rate:.1%}).",
        file=sys.stderr,
    )
    if not _persist_report(args, suite, report):
        return 2
    return 0 if report.passed_cases == report.total_cases else 1


if __name__ == "__main__":
    raise SystemExit(main())
