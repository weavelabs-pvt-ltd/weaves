"""Fail-closed inventory validation and credential-free CI evidence."""

import hashlib
import json
import re


def write_report(output, report):
    """Publish only the explicitly constructed credential-free report object."""
    artifacts = output / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n")


def verify_bundle(harness):
    """Verify the checked-in portable bundle rather than silently accepting drift."""
    lock = json.loads((harness / "bundle.lock.json").read_text())
    files = {
        p.name
        for p in harness.iterdir()
        if p.is_file() and p.name not in {"bundle.lock.json", "README.md"}
    }
    if files != set(lock["files"]):
        raise RuntimeError("Tenancy bundle inventory differs; regenerate its lock")
    for name, digest in lock["files"].items():
        if hashlib.sha256((harness / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Tenancy bundle digest differs: {name}")
    return hashlib.sha256((harness / "bundle.lock.json").read_bytes()).hexdigest()


def specs(suites):
    """Flatten Playwright's nested JSON without losing tests in child suites."""
    result = []
    for suite in suites:
        result.extend(suite.get("specs", []))
        result.extend(specs(suite.get("suites", [])))
    return result


def failure_locations(results):
    """Keep only fixed source names and numeric positions, never error text/data."""
    allowed = {
        "controls.spec.cjs",
        "browser_account.cjs",
        "organization_administration.cjs",
    }
    failures = []
    for result in results:
        if result.get("status") == "passed":
            continue
        for error in result.get("errors", []) or [result.get("error", {})]:
            location = error.get("location", {})
            filename = (
                str(location.get("file", "")).replace("\\", "/").rsplit("/", 1)[-1]
            )
            line, column = location.get("line", 0), location.get("column", 0)
            failures.append(
                {
                    "file": filename if filename in allowed else "browser assertion",
                    "line": line if isinstance(line, int) else 0,
                    "column": column if isinstance(column, int) else 0,
                }
            )
    return failures


SECRET_KEY = re.compile(r"token|secret|password|cookie|authorization|api[_-]?key", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PATTERNS = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 [redacted]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+){1,2}"), "[redacted-jwt]"),
    (
        re.compile(
            r"(?i)(\b[\w-]*(?:token|secret|password|cookie|authorization|api[_-]?key)"
            r"[\w-]*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+"
        ),
        r"\1[redacted]",
    ),
    (re.compile(r"(?i)(set-cookie|cookie):[^\n]*"), r"\1: [redacted]"),
    (re.compile(r"[A-Za-z0-9_+=-]{32,}"), "[redacted-long]"),
)


def fixture_secrets(value, key="", found=None):
    """Collect exact secret strings from the private fixture so they never print."""
    found = set() if found is None else found
    if isinstance(value, dict):
        for name, item in value.items():
            fixture_secrets(item, key + "/" + str(name), found)
    elif isinstance(value, list):
        for item in value:
            fixture_secrets(item, key, found)
    elif isinstance(value, str) and len(value) >= 8:
        secret_like = SECRET_KEY.search(key) or "headers" in key
        if secret_like:
            found.add(value)
    return found


def scrub(text, secrets=(), limit=700):
    """Redact credentials and truncate; used for every published failure message."""
    text = ANSI.sub("", str(text))
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def failure_messages(item, results, secrets=()):
    """Assertion text and the browser console/network summary of a failed control."""
    errors = []
    for result in results:
        if result.get("status") == "passed":
            continue
        for error in result.get("errors", []) or [result.get("error", {})]:
            if error.get("message"):
                errors.append(scrub(error["message"], secrets))
    browser = [
        scrub(note.get("description", ""), secrets, 1500)
        for test in item["tests"]
        for note in test.get("annotations", [])
        if note.get("type") == "browser-summary"
    ]
    return {"errors": errors[:3], "browser": browser[:1]}


def summarize(raw, pair, sources, bundle_digest, fixture):
    """Require every declared case exactly once and report known defects honestly."""
    items = specs(raw.get("suites", []))
    required = set(pair["required_controls"]) | set(pair["known_defects"])
    observed = {}
    locations = {}
    messages = {}
    secrets = fixture_secrets(fixture)
    for item in items:
        match = re.match(r"(T(?:\d+-[ID]\d{2}|W-\d{2}))\b", item["title"])
        if not match or match[1] in observed:
            raise RuntimeError("Unidentified or duplicate tenancy test ID")
        results = [r for test in item["tests"] for r in test.get("results", [])]
        observed[match[1]] = bool(results) and all(
            r["status"] == "passed" for r in results
        )
        if not observed[match[1]]:
            locations[match[1]] = failure_locations(results)
            messages[match[1]] = failure_messages(item, results, secrets)
    complete = (
        set(observed) == required and all(observed.values()) and not raw.get("errors")
    )
    diagnostics = fixture.get("diagnostics", {})
    known = {
        key: {"observed": bool(diagnostics.get(key)), "resolution_phase": 1}
        for key in pair["known_defects"]
    }
    complete = complete and all(value["observed"] for value in known.values())
    return {
        "phase": pair["phase"],
        "foundation_passed": complete,
        "saas_ready": False,
        "sources": sources,
        "bundle_sha256": bundle_digest,
        "required_cases": sorted(required),
        "case_results": observed,
        "failure_locations": locations,
        "failure_messages": messages,
        "known_defects": known,
        "resolved_defects": pair.get("resolved_defects", {}),
        "storage": "postgres",
        "worker": "apps.worker.main",
        "schema_version": fixture.get("schema_version"),
        "note": (
            "Known defects are baseline observations; "
            "they are not passing isolation guarantees."
        ),
    }
