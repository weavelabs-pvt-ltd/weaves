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


def summarize(raw, pair, sources, bundle_digest, fixture):
    """Require every declared case exactly once and report known defects honestly."""
    items = specs(raw.get("suites", []))
    required = set(pair["required_controls"]) | set(pair["known_defects"])
    observed = {}
    for item in items:
        match = re.match(r"(T\d+-[ID]\d{2})\b", item["title"])
        if not match or match[1] in observed:
            raise RuntimeError("Unidentified or duplicate tenancy test ID")
        results = [r for test in item["tests"] for r in test.get("results", [])]
        observed[match[1]] = bool(results) and all(
            r["status"] == "passed" for r in results
        )
    complete = (
        set(observed) == required and all(observed.values()) and not raw.get("errors")
    )
    diagnostics = fixture.get("diagnostics", {})
    known = {
        key: {"observed": bool(diagnostics.get(key)), "resolution_phase": 1}
        for key in pair["known_defects"]
        if key != "T0-D04"
    }
    known["T0-D04"] = {"observed": observed.get("T0-D04", False), "resolution_phase": 1}
    complete = complete and all(value["observed"] for value in known.values())
    return {
        "phase": pair["phase"],
        "foundation_passed": complete,
        "saas_ready": False,
        "sources": sources,
        "bundle_sha256": bundle_digest,
        "required_cases": sorted(required),
        "case_results": observed,
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
