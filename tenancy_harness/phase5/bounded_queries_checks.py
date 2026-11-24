"""Slice 5.4 control T5-I04: Home and every list stay bounded on a large history.

Runs in the seed container after the native worker is up. It creates a fresh
organization through the real account API, then copies tenant A's real,
finished task and turn thousands of times (SQL, owner connection, rewritten to
the new organization) so that workspace holds a history far larger than any
page. Through the real API it then checks that:

* every list answers at most ``limit`` rows and keyset pages visit every row
  exactly once, matching the database count;
* the historic bare-array responses are unchanged when no paging parameter is
  sent, so an older frontend keeps working;
* a cursor minted for one tenant never exposes another tenant's rows;
* ``GET /overview`` equals counts computed independently in SQL, for the
  seeded organization and for the two untouched fixture tenants.

The browser half (request count, tile numbers, no history traversal) is
``bounded_queries_ui.cjs``, which reads the numbers recorded here.
"""

import json
import math
import os
import time
from pathlib import Path

import psycopg

from tenancy_harness.client import request
from tenancy_harness.lifecycle_checks import create_organization
from tenancy_harness.seed import require_disposable_database

CONTROL_ID = "T5-I04"
SLICE = "5.4"
OWNER = "bounded.owner@tenancy.test"
RECENT = 1200  # tasks created within the last ~4.2 days
OLD = 800  # tasks created 40+ days ago (outside every short chart range)
PAGE = 50
# (n, ts): recent rows every five minutes, old rows 40+ days back.
STAMPS = (
    "SELECT n, CASE WHEN n <= %s THEN now() - make_interval(mins => n * 5) "
    "ELSE now() - make_interval(days => 40, mins => n) END AS ts "
    "FROM generate_series(1, %s) n"
)
VISIBLE = """(COALESCE(k.payload->>'audience', 'workspace') <> 'private'
              OR k.payload->>'audience_owner_principal_id' = %s)"""


def call(actor, path):
    """GET through the real API as a tenant actor."""
    return request("GET", path, actor["headers"])


def walk(actor, path, limit=PAGE, key="task_id"):
    """Follow next_cursor to the end; return (ids, pages, largest page, cursors)."""
    joiner = "&" if "?" in path else "?"
    ids, sizes, cursors, cursor = [], [], [], ""
    for _ in range(1000):
        suffix = f"&cursor={cursor}" if cursor else ""
        response = call(actor, f"{path}{joiner}paginate=true&limit={limit}{suffix}")
        if response.status_code != 200:
            detail = str(response.json().get("detail", ""))[:300]
            raise AssertionError(f"{path}: HTTP {response.status_code} {detail}")
        body = response.json()
        ids += [item[key] for item in body["items"]]
        sizes.append(len(body["items"]))
        cursor = body["next_cursor"] or ""
        if cursor:
            cursors.append(cursor)
        else:
            return ids, len(sizes), max(sizes), cursors
    raise AssertionError("cursor walk did not terminate")


def sql(query, *params):
    """Read or write the disposable database on the owner connection."""
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection:
        cursor = connection.execute(query, params or None)
        return cursor.fetchall() if cursor.description else None


def wait_finished(actor, task_id):
    """The seeded template must be a finished turn before it is copied."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        task = call(actor, "/tasks/" + task_id).json()
        if task.get("latest_turn_status") == "succeeded":
            return task
        time.sleep(1)
    raise AssertionError("template task did not finish")


def principal_of(actor):
    """The tenant-local principal an organization owner acts as."""
    return call(actor, "/auth/me").json()["principal"]["id"]


def seed_history(template, org, workspace, principal):
    """Copy the finished template task and turn into the new organization."""
    task = wait_finished(template["admin"], template["task"]["task_id"])
    rewrite = json.dumps(
        {
            "org_id": org,
            "workspace_id": workspace,
            "created_by_principal_id": principal,
            "requested_by_principal_id": principal,
            "audience": "workspace",
            "audience_owner_principal_id": None,
            "private_skill_snapshots": [],
            "idempotency_key": None,
        }
    )
    turn_rewrite = json.dumps({**json.loads(rewrite), "run_idempotency_key": None})
    sql(
        """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
               requested_by_principal_id, payload, created_at, updated_at)
           SELECT 'tasks', 'bq-task-'||n, %s, %s, %s,
                  payload || %s::jsonb || jsonb_build_object('task_id', 'bq-task-'||n,
                      'latest_turn_id', 'bq-turn-'||n, 'title', 'Bounded history '||n,
                      'created_at', to_jsonb(ts), 'updated_at', to_jsonb(ts)),
                  ts, ts
           FROM product_records t, LATERAL ("""
        + STAMPS
        + """) g
           WHERE t.collection = 'tasks' AND t.record_id = %s
             AND t.org_id = %s AND t.workspace_id = %s""",
        org,
        workspace,
        principal,
        rewrite,
        RECENT,
        RECENT + OLD,
        task["task_id"],
        template["org_id"],
        template["workspace_id"],
    )
    sql(
        """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
               requested_by_principal_id, payload, created_at, updated_at)
           SELECT 'task_turns', 'bq-turn-'||n, %s, %s, %s,
                  payload || %s::jsonb || jsonb_build_object('turn_id', 'bq-turn-'||n,
                      'task_id', 'bq-task-'||n,
                      'status', CASE WHEN n %% 7 = 0 THEN 'failed' ELSE 'succeeded' END,
                      'created_at', to_jsonb(ts), 'updated_at', to_jsonb(ts)),
                  ts, ts
           FROM product_records t, LATERAL ("""
        + STAMPS
        + """) g
           WHERE t.collection = 'task_turns' AND t.record_id = %s
             AND t.org_id = %s AND t.workspace_id = %s""",
        org,
        workspace,
        principal,
        turn_rewrite,
        RECENT,
        RECENT + OLD,
        task["latest_turn_id"],
        template["org_id"],
        template["workspace_id"],
    )
    sql("ANALYZE product_records")


def truth(org, workspace, principal):
    """Independent SQL truth for one workspace, as the principal sees it."""
    tasks = sql(
        f"""SELECT count(*) FROM product_records k WHERE k.collection = 'tasks'
              AND k.org_id = %s AND k.workspace_id = %s AND {VISIBLE}""",
        org,
        workspace,
        principal,
    )[0][0]
    week = sql(
        f"""SELECT count(*), count(*) FILTER (WHERE t.payload->>'status' = 'failed'),
                   count(*) FILTER (WHERE t.payload->>'status' = 'succeeded')
            FROM product_records t JOIN product_records k ON k.collection = 'tasks'
              AND k.record_id = t.payload->>'task_id' AND k.org_id = t.org_id
              AND k.workspace_id = t.workspace_id
            WHERE t.collection = 'task_turns' AND t.org_id = %s AND t.workspace_id = %s
              AND t.created_at >= now() - interval '5 days' AND {VISIBLE}""",
        org,
        workspace,
        principal,
    )[0]
    history = sql(
        f"""SELECT count(*) FROM product_records t JOIN product_records k
              ON k.collection = 'tasks' AND k.record_id = t.payload->>'task_id'
              AND k.org_id = t.org_id AND k.workspace_id = t.workspace_id
            WHERE t.collection = 'task_turns' AND t.org_id = %s AND t.workspace_id = %s
              AND t.created_at >= now() - interval '182 days' AND {VISIBLE}""",
        org,
        workspace,
        principal,
    )[0][0]
    return {
        "tasks": tasks,
        "turns_5_days": week[0],
        "failed_5_days": week[1],
        "succeeded_5_days": week[2],
        "turns_182_days": history,
    }


def overview(actor, offset=0):
    """GET /overview as the actor's tenant."""
    response = call(actor, f"/overview?tz_offset_minutes={offset}")
    if response.status_code != 200:
        raise AssertionError(f"/overview: HTTP {response.status_code}")
    return response.json()


def page_checks(big, other, expected_tasks):
    """Bounded pages, exact keyset walks, compatibility and cursor isolation."""
    ids, pages, largest, cursors = walk(big, "/tasks")
    other_ids = {
        item["task_id"] for item in call(other, "/tasks?limit=100").json()["items"]
    }
    stray = call(other, f"/tasks?limit=100&cursor={cursors[0]}").json()["items"]
    roles = call(big, "/roles").json()
    role_ids, _, _, role_cursors = walk(big, "/roles", limit=1, key="id")
    foreign = call(
        other, f"/roles?paginate=true&limit=100&cursor={role_cursors[0]}"
    ).json()["items"]
    other_roles = {role["id"] for role in call(other, "/roles").json()}
    bare = (
        "/agents",
        "/workflows",
        "/users",
        "/workspaces",
        "/model-profiles",
        "/plugin-installations",
        "/approvals?limit=100",
        "/workflow-triggers",
        "/workflow-schedules",
    )
    return {
        "task_pages_are_bounded": largest <= PAGE
        and pages == math.ceil(expected_tasks / PAGE),
        "task_walk_visits_every_task_once": len(ids) == len(set(ids)) == expected_tasks,
        "task_limit_above_maximum_is_422": call(big, "/tasks?limit=101").status_code
        == 422,
        "catalog_walk_matches_bare_list": isinstance(roles, list)
        and sorted(role_ids) == sorted(role["id"] for role in roles),
        "paging_parameters_are_validated": all(
            call(big, path).status_code == 422
            for path in (
                "/roles?limit=0",
                "/roles?cursor=not-a-cursor",
                "/approvals?cursor=not-a-cursor",
            )
        ),
        "bare_arrays_unchanged_without_paging": all(
            isinstance(call(big, path).json(), list) for path in bare
        ),
        "paginate_selects_the_envelope": all(
            set(call(big, path + "paginate=true&limit=1").json())
            == {"items", "next_cursor"}
            for path in ("/agents?", "/workflows?", "/approvals?", "/users?")
        ),
        "task_cursor_never_crosses_tenants": all(
            item["task_id"] in other_ids for item in stray
        ),
        "catalog_cursor_never_crosses_tenants": all(
            role["id"] in other_roles for role in foreign
        ),
    }


def overview_checks(big, truths, others):
    """The aggregate equals SQL truth for the seeded and the untouched tenants."""
    seen, shifted = overview(big), overview(big, 330)
    big_truth = truths["big"]
    result = {
        "overview_task_total_matches_database": seen["tasks"]["total"]
        == big_truth["tasks"],
        "overview_history_matches_database": sum(r["total"] for r in seen["daily"])
        == big_truth["turns_182_days"],
        "overview_day_buckets_follow_the_zone": sum(
            r["total"] for r in shifted["daily"]
        )
        == big_truth["turns_182_days"],
        "overview_counts_more_history_than_the_week": seen["tasks"]["total"]
        > big_truth["turns_5_days"],
        "overview_has_no_cost_from_other_tenants": all(
            row["cost_usd"] is None for row in seen["daily"]
        ),
    }
    for label, (actor, expected) in others.items():
        mine = overview(actor)
        result[f"overview_tenant_{label}_matches_database"] = (
            mine["tasks"]["total"] == expected["tasks"]
            and sum(r["total"] for r in mine["daily"]) == expected["turns_182_days"]
        )
    return result


def bounded_queries_control():
    """Seed, check, and return the named results plus the browser's expectations."""
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    tenants = json.loads(path.read_text())["tenants"]
    a, b = tenants["a"], tenants["b"]
    created = create_organization(OWNER, "Bounded history")
    if created["status"] != 201:
        raise AssertionError(f"bounded organization: HTTP {created['status']}")
    org, workspace = created["org_id"], created["headers"]["X-Workspace-ID"]
    big = {"headers": created["headers"]}
    principal = principal_of(big)
    seed_history(a, org, workspace, principal)
    truths = {"big": truth(org, workspace, principal)}
    for label, tenant in (("a", a), ("b", b)):
        truths[label] = truth(
            tenant["org_id"], tenant["workspace_id"], tenant["admin"]["principal_id"]
        )
    assert truths["big"]["tasks"] == RECENT + OLD, truths["big"]
    results = page_checks(big, a["admin"], truths["big"]["tasks"])
    results.update(
        overview_checks(
            big,
            truths,
            {"a": (a["admin"], truths["a"]), "b": (b["admin"], truths["b"])},
        )
    )
    results["seeded_history_is_larger_than_any_page"] = (
        truths["big"]["tasks"] > 20 * PAGE
    )
    results["browser"] = {
        "email": OWNER,
        "org_id": org,
        "workspace_id": workspace,
        "truth": truths["big"],
    }
    return results


def main():
    """Record the control in the private fixture file the browser suite reads."""
    require_disposable_database()
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    result = bounded_queries_control()
    fixtures = json.loads(path.read_text())
    fixtures["bounded_queries"] = result
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    print("Bounded queries control recorded.")


if __name__ == "__main__":
    main()
