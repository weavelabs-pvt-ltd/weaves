"""Host half of T5-I05: read the API and worker log stream for the probe.

Stdlib only: it runs in the host runner, which has no application dependencies.
The probe (request id, turn id, tenant ids, prompt sentinel and credential
values) was written by ``correlation_checks`` in the seed container.
"""

import json
from pathlib import Path


def parse_lines(text):
    """JSON objects from Compose log text; non-JSON lines are returned apart."""
    parsed, other = [], []
    for raw in text.splitlines():
        line = raw.split("|", 1)[1].strip() if " | " in raw else raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            other.append(line)
            continue
        (parsed if isinstance(value, dict) else other).append(
            value if isinstance(value, dict) else line
        )
    return parsed, other


def log_results(text, probe):
    """Named booleans for the structured-log half of the control."""
    lines, other = parse_lines(text)
    rid, org = probe["request_id"], probe["org_id"]
    correlated = [line for line in lines if line.get("request_id") == rid]
    api = [
        line
        for line in correlated
        if line.get("service") == "api"
        and line.get("event") == "http_request"
        and line.get("route") == "/api/v0/tasks"
        and line.get("org_id") == org
    ]
    worker = [
        line
        for line in correlated
        if line.get("service") == "worker"
        and line.get("event") == "turn_finished"
        and line.get("turn_id") == probe["turn_id"]
        and line.get("org_id") == org
    ]
    forbidden = [probe["prompt"], *probe["secrets"]]
    return {
        "every_log_line_is_structured_json": not other
        and all({"ts", "level", "message"} <= set(line) for line in lines),
        "api_line_carries_the_request_id": bool(api),
        "worker_line_carries_the_request_id": bool(worker),
        "correlated_lines_stay_in_one_tenant": bool(correlated)
        and all(line.get("org_id", org) == org for line in correlated)
        and probe["other_org_id"] not in json.dumps(correlated),
        "no_prompt_or_credential_in_any_log_line": not any(
            value in text for value in forbidden
        ),
    }


def record(log_path, fixture_path):
    """Merge the log results into the private fixture the browser suite reads."""
    path = Path(fixture_path)
    fixtures = json.loads(path.read_text())
    control = fixtures["correlation"]
    control.update(log_results(Path(log_path).read_text(), control["probe"]))
    del control["probe"]
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
