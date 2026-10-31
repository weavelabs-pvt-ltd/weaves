"""Shared validation and immutable JSON helpers for product contracts."""

import json
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

_SENSITIVE_CONFIG_PARTS = (
    "accesskey",
    "accesstoken",
    "apikey",
    "authorization",
    "clientsecret",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
)


def freeze_json_value(value: Any, field_name: str = "value") -> Any:
    """Validate JSON-compatible data and recursively freeze objects/arrays."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain JSON-compatible values") from exc
    return _freeze(value)


def validate_json_schema(value: Any, field_name: str) -> Any:
    """Validate a schema against JSON Schema Draft 2020-12 and freeze it."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(value)
    except SchemaError as exc:
        raise ValueError(
            f"{field_name} must be a valid JSON Schema: {exc.message}"
        ) from exc
    return freeze_json_value(value, field_name)


def reject_credential_fields(value: Any, field_name: str) -> None:
    """Reject secret-like keys in product configuration that must stay non-secret."""
    if _credential_paths(value):
        raise ValueError(
            f"{field_name} contains credential-shaped fields; use an opaque auth_ref"
        )


def thaw_json_value(value: Any) -> Any:
    """Return a regular JSON tree suitable for API serialization."""
    if isinstance(value, Mapping):
        return {key: thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json_value(item) for item in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _credential_paths(value: Any, path: str = "") -> list[str]:
    paths = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            child_path = f"{path}.{key}" if path else key
            if any(part in normalized for part in _SENSITIVE_CONFIG_PARTS):
                paths.append(child_path)
            paths.extend(_credential_paths(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_credential_paths(item, f"{path}[{index}]"))
    return paths
