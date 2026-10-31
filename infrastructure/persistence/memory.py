"""Deterministic model and context providers used by local development."""

from typing import Any, Protocol

from clients.model_providers.gateway import ModelAdapter
from contracts.v1 import ModelProfileBinding, PluginInstallation
from contracts.v1.model_execution import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
)


class ContextProvider(Protocol):
    def search(self, query: str) -> tuple[dict[str, Any], ...]: ...


class MockKnowledgePlugin:
    """A bounded, deterministic context-read capability."""

    def __init__(self, documents: tuple[dict[str, str], ...] = ()) -> None:
        self._documents = documents or (
            {
                "source_id": "handbook:working-agreements",
                "title": "Working agreements",
                "text": (
                    "Keep tasks small, document decisions, and ask for review "
                    "before production changes."
                ),
            },
            {
                "source_id": "handbook:security-basics",
                "title": "Security basics",
                "text": (
                    "Use least privilege and never include credentials in prompts "
                    "or generated artifacts."
                ),
            },
        )

    def search(self, query: str) -> tuple[dict[str, Any], ...]:
        """Return at most three matching fixture documents with provenance."""
        terms = {term.lower() for term in query.split() if len(term) > 2}
        ranked = sorted(
            self._documents,
            key=lambda item: (
                -sum(
                    1
                    for term in terms
                    if term in (item["title"] + " " + item["text"]).lower()
                )
            ),
        )
        return tuple(
            {
                "source_id": item["source_id"],
                "title": item["title"],
                "text": item["text"],
            }
            for item in ranked[:3]
        )

    def invoke(self, capability_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if capability_id != "knowledge.search":
            raise ValueError("unsupported local knowledge capability")
        query = payload.get("query")
        if not isinstance(query, str):
            raise ValueError("query must be text")
        return {"documents": list(self.search(query))}


class RepositoryKnowledgePlugin:
    """Read workspace context from the product document repository."""

    def __init__(self, documents: Any) -> None:
        self._documents = documents

    def search(
        self,
        query: str,
        *,
        org_id: str,
        workspace_id: str,
    ) -> tuple[dict[str, Any], ...]:
        terms = {term.lower() for term in query.split() if len(term) > 2}
        documents = self._documents.list_scoped(org_id, workspace_id)
        ranked = sorted(
            documents,
            key=lambda item: (
                -sum(
                    1
                    for term in terms
                    if term in (item.title + " " + item.text).lower()
                )
            ),
        )
        return tuple(
            {
                "source_id": item.source_id,
                "title": item.title,
                "text": item.text,
            }
            for item in ranked[:3]
        )

    def invoke(self, capability_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("local knowledge search requires an installation scope")

    def invoke_for_installation(
        self,
        capability_id: str,
        payload: dict[str, Any],
        installation: PluginInstallation,
    ) -> dict[str, Any]:
        """Search only the organization and workspace owning the installation."""
        if capability_id != "knowledge.search":
            raise ValueError("unsupported local knowledge capability")
        query = payload.get("query")
        if not isinstance(query, str):
            raise ValueError("query must be text")
        return {
            "documents": list(
                self.search(
                    query,
                    org_id=installation.org_id,
                    workspace_id=installation.workspace_id,
                )
            )
        }


class DeterministicMockModel(ModelAdapter):
    """Produces repeatable output without network access or model credentials."""

    provider_id = "local-mock-provider"
    model_id = "weaves-deterministic-v1"

    def complete(
        self, request: ModelRequest, binding: ModelProfileBinding
    ) -> ModelResponse:
        prompt = request.messages[-1].content
        response_text = f"Local assistant response:\n\n{prompt}"
        input_tokens = max(
            1, sum(len(message.content.split()) for message in request.messages)
        )
        output_tokens = max(1, len(response_text.split()))
        return ModelResponse(
            provider_id=binding.provider.id,
            model_id=binding.profile.model,
            content=response_text,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            finish_reason="stop",
        )
