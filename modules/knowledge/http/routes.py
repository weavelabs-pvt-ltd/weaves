"""Knowledge, memory, and conversation context API routes."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from contracts.v1 import (
    ConversationThreadStatus,
    MemoryRetentionClass,
    MemoryScope,
)
from modules.knowledge.runtime import KnowledgeService
from modules.memory.runtime import MemoryService


class KnowledgeDocumentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=12_000)


class MemoryItemCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=12_000)
    scope: MemoryScope = MemoryScope.WORKSPACE
    retention_class: MemoryRetentionClass = MemoryRetentionClass.STANDARD
    expires_at: AwareDatetime | None = None
    source_ref: str | None = Field(default=None, max_length=256)
    scope_ref: str | None = Field(default=None, max_length=128)


class ConversationThreadCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_id: str | None = None
    title: str = Field(default="New conversation", min_length=1, max_length=160)


class ConversationThreadPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ConversationThreadStatus


def register_context_routes(
    app: FastAPI,
    knowledge_service: KnowledgeService,
    memory_service: MemoryService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register knowledge, memory, and conversation thread endpoints."""
    _register_context_routes_group_1(app, knowledge_service, memory_service, dump)
    _register_context_routes_group_2(app, memory_service, dump)
    _register_context_routes_group_3(app, memory_service)


def _register_context_routes_group_1(
    app: FastAPI,
    knowledge_service: KnowledgeService,
    memory_service: MemoryService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register knowledge, memory, and conversation thread endpoints."""
    _dump = dump

    def list_knowledge_documents() -> list[dict[str, Any]]:
        documents = knowledge_service.list_documents()
        views = [_dump(item) for item in documents]
        for view in views:
            view.pop("text", None)
        return views

    def create_knowledge_document(
        request: KnowledgeDocumentCreateRequest,
    ) -> dict[str, Any]:
        try:
            document = knowledge_service.create_document(
                title=request.title, text=request.text
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _dump(document)

    def delete_knowledge_document(document_id: str) -> Response:
        try:
            knowledge_service.delete_document(document_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Knowledge document not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=204)

    def list_memory_items() -> list[dict[str, Any]]:
        return [_dump(item) for item in memory_service.list_memory_items()]

    def list_conversation_threads() -> list[dict[str, Any]]:
        return [_dump(item) for item in memory_service.list_conversation_threads()]

    def create_conversation_thread(
        request: ConversationThreadCreateRequest,
    ) -> dict[str, Any]:
        try:
            thread = memory_service.create_conversation_thread(**request.model_dump())
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Agent not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _dump(thread)

    app.get("/api/v0/knowledge-documents")(list_knowledge_documents)
    app.post("/api/v0/knowledge-documents", status_code=201)(create_knowledge_document)
    app.delete("/api/v0/knowledge-documents/{document_id}", status_code=204)(
        delete_knowledge_document
    )
    app.get("/api/v0/memory-items")(list_memory_items)
    app.get("/api/v0/threads")(list_conversation_threads)
    app.post("/api/v0/threads", status_code=201)(create_conversation_thread)


def _register_context_routes_group_2(
    app: FastAPI,
    memory_service: MemoryService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register knowledge, memory, and conversation thread endpoints."""
    _dump = dump

    def get_conversation_thread(thread_id: str) -> dict[str, Any]:
        try:
            return _dump(memory_service.get_conversation_thread(thread_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc

    def update_conversation_thread(
        thread_id: str, request: ConversationThreadPatchRequest
    ) -> dict[str, Any]:
        if request.status is not ConversationThreadStatus.CLOSED:
            raise HTTPException(status_code=422, detail="Threads can only be closed")
        try:
            return _dump(memory_service.close_conversation_thread(thread_id))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc

    def create_memory_item(request: MemoryItemCreateRequest) -> dict[str, Any]:
        try:
            item = memory_service.create_memory_item(**request.model_dump())
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _dump(item)

    app.get("/api/v0/threads/{thread_id}")(get_conversation_thread)
    app.patch("/api/v0/threads/{thread_id}")(update_conversation_thread)
    app.post("/api/v0/memory-items", status_code=201)(create_memory_item)


def _register_context_routes_group_3(
    app: FastAPI,
    memory_service: MemoryService,
) -> None:
    """Register knowledge, memory, and conversation thread endpoints."""

    def delete_memory_item(memory_id: str) -> Response:
        try:
            memory_service.delete_memory_item(memory_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Memory item not found"
            ) from exc
        return Response(status_code=204)

    app.delete("/api/v0/memory-items/{memory_id}", status_code=204)(delete_memory_item)
