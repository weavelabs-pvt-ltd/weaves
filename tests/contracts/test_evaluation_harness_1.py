import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.app import create_app
from modules.evaluations.scoring import (
    EvaluationCase,
    EvaluationReport,
    EvaluationSuite,
    score_evaluation_case,
)
from scripts import run_evaluations
from tests.support.runtime import LocalPlatformRuntime


def test_evaluation_suite_requires_unique_case_ids():
    with pytest.raises(ValidationError, match="case IDs must be unique"):
        EvaluationSuite(
            suite_id="engineering-smoke",
            agent_id="assistant",
            cases=(
                EvaluationCase(case_id="case-a", task="Find the release owner."),
                EvaluationCase(case_id="case-a", task="Find the incident owner."),
            ),
        )


def test_checked_in_local_evaluation_example_matches_the_contract():
    suite_path = (
        Path(__file__).resolve().parents[2] / "examples" / "local-smoke-evaluation.json"
    )
    suite = EvaluationSuite.model_validate_json(suite_path.read_text())
    assert suite.agent_id == "local-assistant"
    assert suite.cases[0].must_contain == ("LOCAL-SMOKE-OK",)


def test_local_smoke_suite_runs_through_product_api_and_scoring():
    suite_path = (
        Path(__file__).resolve().parents[2] / "examples" / "local-smoke-evaluation.json"
    )
    suite = EvaluationSuite.model_validate_json(suite_path.read_text())
    case = suite.cases[0]
    with TestClient(create_app(LocalPlatformRuntime())) as client:
        response = client.post(
            "/api/v0/runs", json={"agent_id": suite.agent_id, "task": case.task}
        )

    assert response.status_code == 201, response.text
    result = run_evaluations.score_run_response(case, response.json(), duration_ms=10)
    assert result.passed
    assert result.run_id == response.json()["run"]["run_id"]


def test_evaluation_rejects_conflicting_phrase_assertions():
    with pytest.raises(ValidationError, match="both required and forbidden"):
        EvaluationCase(
            case_id="conflict",
            task="Summarize the release.",
            must_contain=("Ready",),
            must_not_contain=("ready",),
        )


def test_evaluation_case_scores_output_sources_and_tool_usage():
    case = EvaluationCase(
        case_id="release-status",
        task="Find the release status.",
        must_contain=("Ready for launch",),
        must_not_contain=("blocked",),
        required_sources=("jira:REL-42",),
        minimum_tool_calls=1,
    )
    result = score_evaluation_case(
        case,
        status="succeeded",
        run_id="run-123",
        summaries=("The release is READY for launch.",),
        cited_sources=("jira:REL-42",),
        tool_call_count=1,
        input_tokens=220,
        output_tokens=80,
        estimated_cost_usd=Decimal("0.00042"),
        duration_ms=1700,
    )

    assert result.passed
    assert result.run_id == "run-123"
    assert result.input_tokens == 220
    assert result.estimated_cost_usd == Decimal("0.00042")
    assert all(check.passed for check in result.checks)


def test_evaluation_case_reports_missing_requirements_without_output_capture():
    case = EvaluationCase(
        case_id="release-status",
        task="Find the release status.",
        must_contain=("Ready for launch",),
        required_sources=("jira:REL-42",),
        minimum_tool_calls=1,
    )
    result = score_evaluation_case(
        case,
        status="failed",
        run_id="run-456",
        summaries=("Temporary provider response text",),
        cited_sources=(),
        tool_call_count=0,
    )

    assert not result.passed
    assert {check.name for check in result.checks if not check.passed} == {
        "run_succeeded",
        "contains:Ready for launch",
        "required_sources",
        "minimum_tool_calls",
    }
    assert "Temporary provider response text" not in result.model_dump_json()


def test_evaluation_report_counts_must_match_case_results():
    result = score_evaluation_case(
        EvaluationCase(case_id="healthy", task="Check the service."),
        status="succeeded",
        run_id="run-789",
    )
    with pytest.raises(ValidationError, match="passed_cases must match"):
        EvaluationReport(
            suite_id="health-suite",
            evaluation_id="evaluation-1",
            passed_cases=0,
            total_cases=1,
            pass_rate=0.0,
            results=(result,),
        )


def test_runner_maps_api_response_to_a_privacy_bounded_result(monkeypatch):
    suite = EvaluationSuite(
        suite_id="engineering-smoke",
        agent_id="assistant",
        cases=(
            EvaluationCase(
                case_id="release-ticket",
                task="Summarize release REL-42.",
                must_contain=("release ready",),
                required_sources=("jira:REL-42",),
                minimum_tool_calls=1,
            ),
        ),
    )
    payload = {
        "run": {"run_id": "run-eval-1", "status": "succeeded"},
        "agent_run": {
            "input_tokens": 120,
            "output_tokens": 40,
            "estimated_cost_usd": "0.00025",
        },
        "artifacts": [
            {
                "summary": "Release ready for launch.",
                "provenance": [{"source_ref": "jira:REL-42"}],
            }
        ],
        "tool_invocations": [{"invocation_id": "invocation-1"}],
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(run_evaluations, "urlopen", fake_urlopen)
    result = run_evaluations._run_case(
        "http://localhost:8001", "test-token", suite, suite.cases[0], "eval-123"
    )

    assert result.passed
    assert result.run_id == "run-eval-1"
    assert result.estimated_cost_usd == Decimal("0.00025")
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.get_header("Authorization") == "Bearer test-token"
    assert (
        request.get_header("Idempotency-key") == "weaves-eval-eval-123-release-ticket"
    )
    assert timeout == 180
    assert "Release ready for launch" not in result.model_dump_json()


def test_product_api_persists_suite_and_runner_report_with_run_scope():
    suite = EvaluationSuite(
        suite_id="api-smoke",
        agent_id="local-assistant",
        cases=(
            EvaluationCase(
                case_id="local-marker",
                task="Return the LOCAL-SMOKE-OK marker.",
                must_contain=("LOCAL-SMOKE-OK",),
            ),
        ),
    )
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))

    with client:
        created = client.post(
            "/api/v0/evaluation-suites",
            json={"definition": suite.model_dump(mode="json")},
        )
        assert created.status_code == 201, created.text
        assert created.json()["definition"]["suite_id"] == suite.suite_id

        fetched = client.get(f"/api/v0/evaluation-suites/{suite.suite_id}")
        assert fetched.status_code == 200, fetched.text
        assert (
            EvaluationSuite.model_validate_json(
                json.dumps(fetched.json()["definition"])
            )
            == suite
        )

        run_response = client.post(
            "/api/v0/runs",
            json={"agent_id": suite.agent_id, "task": suite.cases[0].task},
        )
        assert run_response.status_code == 201, run_response.text
        result = run_evaluations.score_run_response(
            suite.cases[0], run_response.json(), duration_ms=12
        )
        report = EvaluationReport(
            suite_id=suite.suite_id,
            evaluation_id="evaluation-api-smoke",
            passed_cases=int(result.passed),
            total_cases=1,
            pass_rate=float(result.passed),
            results=(result,),
        )
        submitted = client.post(
            "/api/v0/evaluation-reports",
            json={
                "definition": suite.model_dump(mode="json"),
                "report": report.model_dump(mode="json"),
            },
        )
        assert submitted.status_code == 201, submitted.text
        assert submitted.json()["report"]["pass_rate"] == 1.0
        assert submitted.json()["definition"]["cases"][0]["case_id"] == (
            suite.cases[0].case_id
        )

        reports = client.get("/api/v0/evaluation-reports")
        assert reports.status_code == 200, reports.text
        assert [item["evaluation_id"] for item in reports.json()] == [
            report.evaluation_id
        ]
        loaded = client.get(f"/api/v0/evaluation-reports/{report.evaluation_id}")
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["submitted_by_principal_id"] == "local-developer"


def test_evaluation_report_must_match_the_saved_suite_cases():
    suite = EvaluationSuite(
        suite_id="scope-smoke",
        agent_id="local-assistant",
        cases=(EvaluationCase(case_id="case-a", task="Check this run."),),
    )
    other_suite = EvaluationSuite(
        suite_id="other-smoke",
        agent_id="local-assistant",
        cases=(EvaluationCase(case_id="case-b", task="Check another run."),),
    )
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))

    with client:
        for definition in (suite, other_suite):
            response = client.post(
                "/api/v0/evaluation-suites",
                json={"definition": definition.model_dump(mode="json")},
            )
            assert response.status_code == 201
        run = client.post(
            "/api/v0/runs",
            json={"agent_id": suite.agent_id, "task": other_suite.cases[0].task},
        )
        assert run.status_code == 201
        result = score_evaluation_case(
            other_suite.cases[0],
            status="succeeded",
            run_id=run.json()["run"]["run_id"],
        )
        report = EvaluationReport(
            suite_id=suite.suite_id,
            evaluation_id="evaluation-invalid-run",
            passed_cases=int(result.passed),
            total_cases=1,
            pass_rate=float(result.passed),
            results=(result,),
        )
        response = client.post(
            "/api/v0/evaluation-reports",
            json={
                "definition": suite.model_dump(mode="json"),
                "report": report.model_dump(mode="json"),
            },
        )

    assert response.status_code == 422
    assert "suite cases" in response.json()["detail"]


def test_cli_fetches_saved_suite_runs_cases_and_persists_report(monkeypatch):
    suite = EvaluationSuite.model_validate_json(
        (
            Path(__file__).resolve().parents[2]
            / "examples"
            / "local-smoke-evaluation.json"
        ).read_text()
    )
    run_payload = {
        "run": {"run_id": "run-cli-eval", "status": "succeeded"},
        "agent_run": {"input_tokens": 5, "output_tokens": 7},
        "artifacts": [
            {
                "summary": "LOCAL-SMOKE-OK",
                "provenance": [{"source_ref": "knowledge:local-smoke"}],
            }
        ],
        "tool_invocations": [],
    }
    requests = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        requests.append(request)
        if request.full_url.endswith("/api/v0/evaluation-suites/local-assistant-smoke"):
            payload = {"definition": suite.model_dump(mode="json")}
        elif request.full_url.endswith("/api/v0/runs"):
            payload = run_payload
        else:
            payload = {"evaluation_id": "saved"}
        return FakeResponse(payload)

    monkeypatch.setattr(run_evaluations, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_evaluations.py",
            "--suite-id",
            suite.suite_id,
            "--base-url",
            "http://localhost:8001",
            "--token",
            "test-token",
            "--persist-report",
        ],
    )

    assert run_evaluations.main() == 0

    assert [request.get_method() for request in requests] == ["GET", "POST", "POST"]
    submitted = json.loads(requests[-1].data)
    assert submitted["definition"]["suite_id"] == suite.suite_id
    assert submitted["report"]["passed_cases"] == 1
    assert submitted["report"]["results"][0]["run_id"] == "run-cli-eval"
