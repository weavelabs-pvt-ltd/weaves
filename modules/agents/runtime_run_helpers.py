"""Bound agent context and workflow references before provider calls."""

from __future__ import annotations

import json
from typing import Any

from contracts.v1 import Artifact


def bound_context_records(
    records: tuple[dict[str, Any], ...],
    *,
    max_records: int,
    max_serialized_chars: int,
) -> tuple[tuple[dict[str, Any], ...], list[str], list[str]]:
    """Bound context count and exact serialized size before building a model request."""
    bounded: list[dict[str, Any]] = []
    omitted_source_ids: list[str] = []
    truncated_source_ids: list[str] = []
    for record in records:
        normalized = _normalize_context_record(record)
        if normalized is None:
            continue
        source_id = normalized["source_id"]
        if len(bounded) >= max_records:
            omitted_source_ids.append(source_id)
            continue
        text = record.get("text", "")
        if isinstance(text, str) and len(text) > len(normalized["text"]):
            truncated_source_ids.append(source_id)
        bounded.append(normalized)
    _shrink_context_records(
        bounded, max_serialized_chars, omitted_source_ids, truncated_source_ids
    )
    return tuple(bounded), omitted_source_ids, truncated_source_ids


def _normalize_context_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Return one bounded context record, or None when it lacks a source ID."""
    source_id = record.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        return None
    text = record.get("text", "")
    title = record.get("title", "Retrieved context")
    normalized: dict[str, Any] = {
        "source_id": source_id[:256],
        "title": (title if isinstance(title, str) else "Retrieved context")[:160],
        "text": (text if isinstance(text, str) else "")[:2_000],
    }
    return normalized


def _shrink_context_records(
    bounded: list[dict[str, Any]],
    max_serialized_chars: int,
    omitted_source_ids: list[str],
    truncated_source_ids: list[str],
) -> None:
    """Trim context text, then remove records until the serialized limit fits."""
    while len(json.dumps(bounded, ensure_ascii=False)) > max_serialized_chars:
        serialized_length = len(json.dumps(bounded, ensure_ascii=False))
        excess_chars = serialized_length - max_serialized_chars
        text_record = next(
            (record for record in reversed(bounded) if record["text"]), None
        )
        if text_record is not None:
            source_id = text_record["source_id"]
            text = text_record["text"]
            remove_chars = min(len(text), max(1, excess_chars))
            text_record["text"] = text[: len(text) - remove_chars]
            if source_id not in truncated_source_ids:
                truncated_source_ids.append(source_id)
            continue
        if not bounded:
            break
        omitted_source_ids.insert(0, bounded.pop()["source_id"])


def bound_workflow_references(
    artifacts: tuple[Artifact, ...], *, max_serialized_chars: int
) -> tuple[list[dict[str, str]], list[str]]:
    """Keep prior-step context source-addressable without overflowing model inputs."""
    references = [
        {
            "artifact_id": item.artifact_id,
            "agent_run_id": item.agent_run_id or "",
            "text": item.summary[:6_000],
        }
        for item in artifacts
    ]
    truncated_artifact_ids: list[str] = []
    while len(json.dumps(references, ensure_ascii=False)) > max_serialized_chars:
        serialized_length = len(json.dumps(references, ensure_ascii=False))
        excess_chars = serialized_length - max_serialized_chars
        text_record = next(
            (record for record in reversed(references) if record["text"]), None
        )
        if text_record is not None:
            artifact_id = text_record["artifact_id"]
            text = text_record["text"]
            remove_chars = min(len(text), max(1, excess_chars))
            text_record["text"] = text[: len(text) - remove_chars]
            if artifact_id not in truncated_artifact_ids:
                truncated_artifact_ids.append(artifact_id)
            continue
        if not references:
            break
        truncated_artifact_ids.append(references.pop()["artifact_id"])
    return references, truncated_artifact_ids
