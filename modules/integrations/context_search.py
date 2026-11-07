"""Shared output shape for connector-provided contextual search tools."""

from contracts.v1 import CapabilityKind, CapabilitySpec


def read_context_search(
    capability_id: str, name: str, description: str, timeout_seconds: int = 20
) -> CapabilitySpec:
    """Build a bounded query-to-sources schema for connector search tools."""
    return CapabilitySpec(
        capability_id=capability_id,
        name=name,
        description=description,
        kind=CapabilityKind.CONTEXT_READ,
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "documents": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_id": {"type": "string", "maxLength": 128},
                            "title": {"type": "string", "maxLength": 160},
                            "text": {"type": "string", "maxLength": 2000},
                        },
                        "required": ["source_id", "title", "text"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["documents"],
            "additionalProperties": False,
        },
        timeout_seconds=timeout_seconds,
    )
