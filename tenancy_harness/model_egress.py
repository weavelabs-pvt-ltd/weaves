"""Exercise provider registration, tenant credentials and legacy worker egress."""

import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg

from tenancy_harness.client import PROVIDER, call


def seed_model_egress(fixtures):
    """Prove denials dispatch nothing, while scoped allowed discovery still works."""
    a, b = fixtures["tenants"].values()
    baseline = httpx.get(PROVIDER + "/egress", timeout=10).json()["paths"]
    before = call("GET", "/model-providers", a["admin"])
    rejected = [
        ("openai", "https://provider/egress-probe/v1"),
        ("anthropic", "https://169.254.169.254"),
        ("gemini", "https://[::1]/v1beta"),
        ("openai_compatible", "https://127.0.0.1/v1"),
        ("openai_compatible", "https://blocked-models.tenancy.test/v1"),
    ]
    for kind, url in rejected:
        call(
            "POST",
            "/model-providers",
            a["admin"],
            {
                "provider_type": kind,
                "display_name": "Denied egress fixture",
                "api_key": "fixture-denied-key",
                "base_url": url,
            },
            expected=(422,),
        )
    for own, foreign in [(a, b), (b, a)]:
        path = "/model-providers/" + foreign["provider_id"]
        call("POST", path + "/refresh-models", own["admin"], expected=(404,))
        call(
            "PUT",
            path + "/credential",
            own["admin"],
            {"api_key": "fixture-denied-key"},
            expected=(404,),
        )
    assert call("GET", "/model-providers", a["admin"]) == before
    assert httpx.get(PROVIDER + "/egress", timeout=10).json()["paths"] == baseline
    for url in [
        "https://wrong-cert.tenancy.test/v1",
        "https://models.tenancy.test/redirect/v1",
    ]:
        call(
            "POST",
            "/model-providers",
            a["admin"],
            {
                "provider_type": "openai_compatible",
                "display_name": "TLS/redirect fixture",
                "api_key": "fixture-denied-key",
                "base_url": url,
            },
            expected=(502,),
        )
    for tenant in [a, b]:
        call(
            "POST",
            "/model-providers/" + tenant["provider_id"] + "/refresh-models",
            tenant["admin"],
        )
    events = httpx.get(PROVIDER + "/model-events", timeout=10).json()["events"]
    for label in ["a", "b"]:
        digest = hashlib.sha256(("Bearer fixture-model-" + label).encode()).hexdigest()
        assert any(event["credential_sha256"] == digest for event in events)
    return {"api_denials": True, "tenant_credentials": True, **legacy_task(a)}


def legacy_task(tenant):
    """Seed only a disposable legacy record; execution uses the actual worker."""
    from apps.runtime_wiring import open_process_runtime
    from shared.request_context import reset_request_principal, set_request_identity
    from tenancy_harness.seed import require_disposable_database

    require_disposable_database()
    runtime = open_process_runtime(process="cli")
    try:
        runtime.bootstrap()
        provider = runtime.providers.get_scoped(tenant["provider_id"], tenant["org_id"])
        identity = set_request_identity(
            "tenancy-fixture", tenant["org_id"], tenant["workspace_id"]
        )
        try:
            auth_ref = runtime.secret_store.put(
                "fixture-legacy-egress-key", purpose="model_provider"
            )
        finally:
            reset_request_principal(identity)
        legacy = provider.model_copy(
            update={
                "id": str(uuid4()),
                "base_url": "https://blocked-models.tenancy.test/v1",
                "auth_ref": auth_ref,
            }
        )
        runtime.providers.create(legacy)
        profile = next(
            p
            for p in runtime.model_profiles.list_scoped(tenant["org_id"])
            if p.provider_id == provider.id
        )
        profile = profile.model_copy(
            update={"id": str(uuid4()), "provider_id": legacy.id}
        )
        runtime.model_profiles.create(profile)
    finally:
        runtime.close()
    call(
        "POST",
        "/model-providers/" + legacy.id + "/refresh-models",
        tenant["admin"],
        expected=(422,),
    )
    call(
        "PUT",
        "/model-providers/" + legacy.id + "/credential",
        tenant["admin"],
        {"api_key": "fixture-denied-key"},
        expected=(422,),
    )
    agent = call(
        "POST",
        "/agents",
        tenant["admin"],
        {
            "name": "Legacy egress containment",
            "instructions": "No external actions",
            "model_profile_id": profile.id,
            "allowed_capability_ids": [],
        },
    )
    task = call(
        "POST",
        "/tasks",
        tenant["admin"],
        {
            "agent_id": agent["id"],
            "title": "Legacy egress containment",
            "initial_message": "Must fail before sending model credentials",
        },
    )
    return {"task_id": task["task_id"], "org_id": tenant["org_id"]}


def observe_model_egress(fixtures):
    """Inspect durable worker denial, then verify the fake received no probe."""
    proof = fixtures["model_egress"]
    tenant = fixtures["tenants"]["a"]
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        task = call("GET", "/tasks/" + proof["task_id"], tenant["admin"])
        if task["latest_turn_status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.25)
    assert task["latest_turn_status"] == "failed"
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        row = connection.execute(
            "SELECT t.payload->'error',a.payload->'error' FROM product_records t "
            "LEFT JOIN product_records a ON a.collection='agent_runs' "
            "AND a.org_id=t.org_id AND a.record_id=t.payload->>'agent_run_id' "
            "WHERE t.collection='task_turns' "
            "AND t.org_id=%s AND t.payload->>'task_id'=%s",
            (proof["org_id"], proof["task_id"]),
        ).fetchone()
    evidence = (
        Path(os.environ["TENANCY_FIXTURE_FILE"]).parent / "model-egress-denial.json"
    )
    evidence.write_text(
        json.dumps(
            {
                "turn_error": row[0] if row else None,
                "agent_error": row[1] if row else None,
            }
        )
    )
    evidence.chmod(0o600)
    assert row and row[1] and row[1]["code"] == "model.egress_denied"
    paths = httpx.get(PROVIDER + "/egress", timeout=10).json()["paths"]
    assert not any("egress-probe" in path for path in paths)
    events = httpx.get(PROVIDER + "/model-events", timeout=10).json()["events"]
    assert not any(event["host"] == "blocked-models.tenancy.test" for event in events)
    proof["worker_denied"] = True
    proof["zero_probe_requests"] = True
