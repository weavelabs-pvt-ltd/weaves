"""Persistent, concurrency-safe lifetime testing ledger in integer nano-dollars."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal

NANOS = 1_000_000_000
MAX_OUTPUT_TOKENS = 1024


class BudgetExhausted(RuntimeError):
    """The next paid request requires explicit additional user authorization."""


class Ledger:
    """Reserve before network dispatch; never reset spend for a new test run."""

    def __init__(self, directory):
        """Require an existing user-authorized configuration and durable ledger."""
        self.directory = directory
        self.config = json.loads((directory / "budget.json").read_text())
        self.path = directory / "testing-spend.sqlite3"
        if not self.path.is_file():
            raise RuntimeError("Lifetime ledger is missing; refusing automatic reset")

    @contextmanager
    def transaction(self):
        """Serialize admissions across processes and recover after crashes."""
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def reserve(self, model, run_id, maximum_calls):
        """Atomically hold conservative spend before a real provider request starts."""
        if model not in self.config["rates"]:
            raise ValueError("Unknown model price; paid testing is blocked")
        verified = datetime.fromisoformat(self.config["rates_verified_at"])
        if (datetime.now(UTC) - verified).days > 14:
            raise ValueError("Verify provider prices before further paid testing")
        reserve = self.config["request_reservation_nanos"]
        limit = self.config["lifetime_limit_nanos"]
        maximum_cost = usage_cost(
            self.config,
            model,
            {"input_tokens": 64000, "output_tokens": MAX_OUTPUT_TOKENS},
        )
        if type(reserve) is not int or reserve <= 0 or maximum_cost > reserve:
            raise ValueError("Request hold cannot cover its maximum possible cost")
        with self.transaction() as db:
            stored_limit = db.execute("SELECT limit_nanos FROM budget").fetchone()[0]
            if stored_limit != limit:
                raise ValueError("Budget change requires explicit user authorization")
            used = db.execute(
                "SELECT COALESCE(sum(COALESCE(actual_nanos, reserve_nanos)),0) "
                "FROM requests"
            ).fetchone()[0]
            calls = db.execute(
                "SELECT count(*) FROM requests WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            if calls >= maximum_calls:
                raise BudgetExhausted(
                    "Per-run model-call allowance exhausted; stop testing"
                )
            if used + reserve > limit:
                raise BudgetExhausted(
                    "Lifetime $100 testing budget exhausted or reserved; "
                    "ask the user before adding funds"
                )
            request_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,?,?)",
                (
                    request_id,
                    run_id,
                    model,
                    reserve,
                    None,
                    "reserved",
                    datetime.now(UTC).isoformat(),
                ),
            )
        return request_id

    def settle(self, request_id, model, usage):
        """Debit verified usage conservatively; retain holds for unknown costs."""
        cost = usage_cost(self.config, model, usage)
        with self.transaction() as db:
            row = db.execute(
                "SELECT reserve_nanos,status,model FROM requests WHERE id=?",
                (request_id,),
            ).fetchone()
            if row is None or row[1] != "reserved" or row[2] != model:
                raise ValueError(
                    "Cannot settle a missing, mismatched or settled request"
                )
            if cost > row[0]:
                db.execute(
                    "UPDATE requests SET status='uncertain' WHERE id=?", (request_id,)
                )
                raise RuntimeError(
                    "Usage exceeded its conservative reservation; stop testing"
                )
            db.execute(
                "UPDATE requests SET actual_nanos=?,status='settled' WHERE id=?",
                (cost, request_id),
            )

    def uncertain(self, request_id):
        """Keep the full hold after timeout or uncertain errors."""
        with self.transaction() as db:
            db.execute(
                "UPDATE requests SET status='uncertain' WHERE id=? "
                "AND status='reserved'",
                (request_id,),
            )

    def snapshot(self):
        """Report conservative consumption and unresolved holds."""
        with self.transaction() as db:
            settled, held, count = db.execute(
                "SELECT COALESCE(sum(actual_nanos),0), "
                "COALESCE(sum(CASE WHEN actual_nanos IS NULL "
                "THEN reserve_nanos ELSE 0 END),0), "
                "count(*) FROM requests"
            ).fetchone()
        limit = self.config["lifetime_limit_nanos"]
        return {
            "currency": "USD",
            "lifetime_limit_usd": str(Decimal(limit) / NANOS),
            "accounted_usd": str(Decimal(settled) / NANOS),
            "unresolved_reserved_usd": str(Decimal(held) / NANOS),
            "available_usd": str(Decimal(limit - settled - held) / NANOS),
            "requests": count,
            "accounting_multiplier": self.config["accounting_multiplier"],
        }


def usage_cost(config, model, usage):
    """Price only supported text usage; reject unpriced cache/server-side features."""
    if any(
        usage.get(key)
        for key in (
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "server_tool_use",
        )
    ):
        raise ValueError("Unpriced provider usage; retain the request reservation")
    incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
    if (
        type(incoming) is not int
        or type(outgoing) is not int
        or min(incoming, outgoing) < 0
    ):
        raise ValueError("Missing/invalid provider usage; retain its reservation")
    if incoming > 64000 or outgoing > MAX_OUTPUT_TOKENS:
        raise ValueError("Usage exceeds the bounded live smoke envelope")
    rate = config["rates"][model]
    amounts = [
        Decimal(rate["input_per_million_usd"]),
        Decimal(rate["output_per_million_usd"]),
    ]
    margin = Decimal(config["accounting_multiplier"])
    if any(not value.is_finite() or value <= 0 for value in amounts):
        raise ValueError("Invalid model prices; paid testing blocked")
    if not margin.is_finite() or margin < 1:
        raise ValueError("Accounting margin cannot reduce provider charges")
    usd = (
        Decimal(rate["input_per_million_usd"]) * incoming
        + Decimal(rate["output_per_million_usd"]) * outgoing
    ) / 1_000_000
    return int(
        (usd * Decimal(config["accounting_multiplier"]) * NANOS).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


def initialize(directory, config):
    """Create the explicitly authorized lifetime ledger once; never overwrite it."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    config_path = directory / "budget.json"
    db_path = directory / "testing-spend.sqlite3"
    if config_path.exists() or db_path.exists():
        raise ValueError("Existing budget/ledger must be preserved, not reinitialized")
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE budget(limit_nanos INTEGER NOT NULL)")
        db.execute("INSERT INTO budget VALUES (?)", (config["lifetime_limit_nanos"],))
        db.execute(
            "CREATE TABLE requests(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,"
            "model TEXT NOT NULL,reserve_nanos INTEGER NOT NULL,actual_nanos INTEGER,"
            "status TEXT NOT NULL,created_at TEXT NOT NULL)"
        )
    db_path.chmod(0o600)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    config_path.chmod(0o600)
