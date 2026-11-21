"""Regenerate the machine-readable part of the data-model documentation.

The handwritten narrative lives in ``docs/DATA_MODEL.md``. Everything that can
be derived from code is written to ``docs/data-model/generated.md`` by this
script: the collection catalogue (``REPOSITORY_SPECS`` and the contracts), the
index inventory (the migration statements), and the row-level-security system
scopes. Run ``python scripts/export_data_model.py`` to rewrite the file and
``python scripts/export_data_model.py --check`` to fail when it is stale.
"""

from __future__ import annotations

import enum
import re
import sys
import types
import typing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infrastructure.database import plugin_ownership_sql  # noqa: E402
from infrastructure.database.row_security import (  # noqa: E402
    LIFECYCLE_SCHEMA,
    POLICY_FUNCTIONS,
)
from infrastructure.database.store import _SCHEMA_MIGRATIONS  # noqa: E402
from infrastructure.persistence.product_repositories import (  # noqa: E402
    REPOSITORY_SPECS,
    TENANT_KEYED_COLLECTIONS,
)
from modules.auth.organization_export_rules import EXPORT_COLLECTIONS  # noqa: E402
from modules.auth.organization_retention import (  # noqa: E402
    KEEP_COLLECTIONS,
    PURGE_ORDER,
)

OUTPUT = ROOT / "docs" / "data-model" / "generated.md"
_ACCOUNT_SCHEMA_MODULE = "infrastructure.database.account_schema"
_INDEX = re.compile(
    r"CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF NOT EXISTS\s+)?(\w+)\s+ON\s+(\w+)\s*"
    r"\((.*?)\)\s*(?:WHERE\s+(.*))?$",
    re.S,
)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _type_name(annotation: Any) -> str:
    """Render a type annotation compactly."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Annotated:
        return _type_name(args[0])
    if origin in (typing.Union, types.UnionType):
        parts = [_type_name(a) for a in args if a is not type(None)]
        suffix = "?" if len(parts) < len(args) else ""
        return " | ".join(parts) + suffix
    if origin is typing.Literal:
        return "literal(" + ", ".join(repr(a) for a in args) + ")"
    if origin in (list, tuple, set, frozenset):
        return f"{origin.__name__}[" + ", ".join(_type_name(a) for a in args) + "]"
    if origin is dict:
        return "dict[" + ", ".join(_type_name(a) for a in args) + "]"
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        values = "/".join(str(m.value) for m in annotation)
        return f"enum {annotation.__name__}({values})"
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def _constraints(field: Any) -> str:
    out: list[str] = []
    for meta in field.metadata:
        for key in (
            "min_length",
            "max_length",
            "ge",
            "gt",
            "le",
            "lt",
            "pattern",
        ):
            value = getattr(meta, key, None)
            if value is not None:
                out.append(f"{key}={value}")
    annotation = field.annotation
    if typing.get_origin(annotation) is typing.Annotated:
        for meta in typing.get_args(annotation)[1:]:
            for key in ("min_length", "max_length", "pattern"):
                value = getattr(meta, key, None)
                if value is not None:
                    out.append(f"{key}={value}")
    return ", ".join(out)


def _default(field: Any) -> str:
    if field.is_required():
        return "required"
    if field.default_factory is not None:
        return "default factory"
    return f"default {field.default!r}"


def _index_rows() -> list[dict[str, str]]:
    """Parse every index created by migrations and repeatable statements."""
    from infrastructure.database import account_schema

    sources: list[tuple[str, str]] = []
    for version, statements in _SCHEMA_MIGRATIONS:
        sources += [(f"migration {version}", s) for s in statements]
    sources += [
        ("repeatable (account schema)", s) for s in account_schema.ACCOUNT_SCHEMA
    ]
    sources += [("repeatable (lifecycle)", s) for s in LIFECYCLE_SCHEMA]
    sources += [
        ("migration 20 (operator-activated)", s) for s in plugin_ownership_sql.TABLES
    ]
    rows = []
    dropped = {
        m.group(1)
        for _, statement in sources
        for m in [re.search(r"DROP INDEX IF EXISTS (\w+)", statement)]
        if m
    }
    for origin, statement in sources:
        match = _INDEX.search(_squash(statement).replace("( ", "("))
        if not match:
            continue
        unique, name, table, columns, predicate = match.groups()
        if name in dropped:
            continue
        rows.append(
            {
                "origin": origin,
                "name": name,
                "table": table,
                "unique": "yes" if unique else "no",
                "columns": _squash(columns),
                "predicate": _squash(predicate or ""),
            }
        )
    return rows


def _collection_of(predicate: str) -> str | None:
    match = re.search(r"collection = '(\w+)'", predicate)
    return match.group(1) if match else None


def _system_purposes() -> dict[str, list[str]]:
    """Return the named system scope allow-lists from the policy function."""
    text = POLICY_FUNCTIONS[0]
    purposes: dict[str, list[str]] = {}
    for purpose, body in re.findall(
        r"WHEN '(\w+)' THEN (true|coll = ANY \(ARRAY\[.*?\]\))", text, re.S
    ):
        purposes[purpose] = (
            ["*all collections*"] if body == "true" else re.findall(r"'(\w+)'", body)
        )
    return purposes


def _global_policy_collections() -> list[str]:
    match = re.search(r"WHEN coll IN \((.*?)\)", POLICY_FUNCTIONS[1], re.S)
    return re.findall(r"'(\w+)'", match.group(1)) if match else []


def _by_collection(indexes: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in indexes:
        collection = _collection_of(row["predicate"])
        if collection:
            grouped.setdefault(collection, []).append(row)
    return grouped


def _summary_lines(by_collection: dict[str, list[dict[str, str]]]) -> list[str]:
    lines = [
        "# Generated data-model reference",
        "",
        "<!-- Generated by scripts/export_data_model.py. Do not edit by hand. -->",
        "",
        "Regenerate with `python scripts/export_data_model.py`; "
        "`tests/contracts/test_data_model_doc.py` fails when this file is stale. "
        "Handwritten explanation is in [../DATA_MODEL.md](../DATA_MODEL.md).",
        "",
        "## 1. Collection summary",
        "",
        "Scope is derived from the contract fields (`org_id`, `workspace_id`) that "
        "become the `product_records` scope columns. `Export` means the collection "
        "is in `EXPORT_COLLECTIONS`; `Purge` is its position in `PURGE_ORDER` "
        "(`keep` = in `KEEP_COLLECTIONS`, never swept).",
        "",
        "| Collection | Contract | Id field | Scope from contract | Has updated_at | "
        "Tenant-keyed id | Export | Purge | Indexes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for _, collection, id_field, contract in REPOSITORY_SPECS:
        fields = contract.model_fields
        scope = (
            "workspace"
            if "workspace_id" in fields and "org_id" in fields
            else "organization"
            if "org_id" in fields
            else "global/account"
        )
        mutable = "yes" if "updated_at" in fields else "no"
        if contract.model_config.get("frozen"):
            mutable = "no (frozen)"
        export = "yes" if collection in EXPORT_COLLECTIONS else "no"
        if collection in PURGE_ORDER:
            purge = str(PURGE_ORDER.index(collection) + 1)
        else:
            purge = "keep" if collection in KEEP_COLLECTIONS else "none"
        names = ", ".join(f"`{r['name']}`" for r in by_collection.get(collection, []))
        keyed = "yes" if collection in TENANT_KEYED_COLLECTIONS else "no"
        lines.append(
            f"| `{collection}` | `{contract.__name__}` | `{id_field}` | {scope} | "
            f"{mutable} | {keyed} | {export} | {purge} | {names or '-'} |"
        )
    return lines


def _field_lines() -> list[str]:
    lines = ["", "## 2. Fields per collection", ""]
    for _, collection, id_field, contract in REPOSITORY_SPECS:
        lines += [
            f"### `{collection}`",
            "",
            f"Contract `{contract.__module__}.{contract.__name__}`; "
            f"`record_id` = `{id_field}`.",
            "",
            "| Field | Type | Constraints | Presence |",
            "|---|---|---|---|",
        ]
        for name, field in contract.model_fields.items():
            lines.append(
                f"| `{name}` | {_type_name(field.annotation)} | "
                f"{_constraints(field) or '-'} | {_default(field)} |"
            )
        refs = [
            n
            for n in contract.model_fields
            if (n.endswith("_id") or n.endswith("_ids") or n.endswith("_ref"))
            and n not in {id_field, "org_id", "workspace_id"}
        ]
        lines += [
            "",
            "Reference-like fields: " + (", ".join(f"`{r}`" for r in refs) or "none"),
            "",
        ]
    return lines


def _index_lines(indexes: list[dict[str, str]]) -> list[str]:
    lines = [
        "## 3. Index inventory",
        "",
        "Parsed from the migration and repeatable statements. `collection` is the "
        "partial-index predicate that scopes an index to one logical collection of "
        "`product_records`.",
        "",
        "| Index | Origin | Table | Unique | Key expressions | Predicate |",
        "|---|---|---|---|---|---|",
    ]
    for row in indexes:
        lines.append(
            f"| `{row['name']}` | {row['origin']} | `{row['table']}` | "
            f"{row['unique']} | "
            f"`{row['columns']}` | `{row['predicate'] or '-'}` |"
        )
    return lines


def _rls_lines() -> list[str]:
    lines = [
        "",
        "## 4. Row-level security: named system scopes",
        "",
        "From `weaves_system_allows` in `infrastructure/database/row_security.py`. "
        "A transaction sets `app.system_purpose` from `system_scope(...)`; any "
        "other value allows no collection.",
        "",
        "| Purpose | Collections it may read and write without a tenant match |",
        "|---|---|",
    ]
    for purpose, collections in _system_purposes().items():
        lines.append(f"| `{purpose}` | {', '.join(f'`{c}`' for c in collections)} |")
    global_rls = sorted(set(_global_policy_collections()))
    lines += [
        "",
        "Collections handled by the account branch of `weaves_rls_allows` (visible "
        "only under a system purpose that lists them, never by tenant match): "
        + ", ".join(f"`{c}`" for c in global_rls)
        + ".",
    ]
    return lines


def _orphan_lines(by_collection: dict[str, list[dict[str, str]]]) -> list[str]:
    lines = [
        "",
        "## 5. Collections present in code but not in `REPOSITORY_SPECS`",
        "",
        "Collection names referenced by purge order or indexes that no repository "
        "serves in this release.",
        "",
    ]
    repo_collections = {spec[1] for spec in REPOSITORY_SPECS}
    orphans = sorted((set(PURGE_ORDER) | set(by_collection)) - repo_collections)
    lines += [f"- `{c}`" for c in orphans] or ["- none"]
    lines.append("")
    return lines


def render() -> str:
    """Return the complete generated Markdown document."""
    indexes = _index_rows()
    by_collection = _by_collection(indexes)
    lines = [
        *_summary_lines(by_collection),
        *_field_lines(),
        *_index_lines(indexes),
        *_rls_lines(),
        *_orphan_lines(by_collection),
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """Write the generated file, or with ``--check`` verify it is current."""
    content = render()
    if "--check" in argv:
        current = OUTPUT.read_text() if OUTPUT.exists() else ""
        if current != content:
            print(
                "docs/data-model/generated.md is stale; "
                "run scripts/export_data_model.py"
            )
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
