"""Evaluate workflow conditions against structured JSON output."""

from __future__ import annotations

import json
from typing import Any

from pydantic import JsonValue

from contracts.v1 import WorkflowConditionOperator, WorkflowStepCondition
from contracts.v1.json_data import thaw_json_value


def workflow_condition_matches(
    condition: WorkflowStepCondition,
    source_output: JsonValue,
    *,
    has_structured_output: bool,
) -> tuple[bool, str]:
    """Evaluate a bounded JSON Pointer condition without exposing values."""
    if not has_structured_output:
        return False, "source_has_no_structured_output"
    current: Any = source_output
    if condition.json_pointer:
        for raw_part in condition.json_pointer.split("/")[1:]:
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, dict) and part in current:
                current = current[part]
                continue
            if isinstance(current, list) and part.isdigit():
                index = int(part)
                if index < len(current):
                    current = current[index]
                    continue
            return False, "condition_path_missing"
    if condition.operator is WorkflowConditionOperator.EXISTS:
        return True, "condition_matched"
    expected = thaw_json_value(condition.expected_value)
    actual_json = json.dumps(current, ensure_ascii=False, sort_keys=True)
    expected_json = json.dumps(expected, ensure_ascii=False, sort_keys=True)
    matched = actual_json == expected_json
    if condition.operator is WorkflowConditionOperator.NOT_EQUALS:
        matched = not matched
    return (True, "condition_matched") if matched else (False, "condition_not_matched")
