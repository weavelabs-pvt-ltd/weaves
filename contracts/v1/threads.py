"""Conversation thread contracts for scoped agent runs and memory."""

from contracts.v1.base import (
    DisplayName,
    MutableWorkspaceScopedContract,
    OpaqueId,
    StrEnum,
)


class ConversationThreadStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ConversationThread(MutableWorkspaceScopedContract):
    thread_id: OpaqueId
    agent_id: OpaqueId
    title: DisplayName
    status: ConversationThreadStatus = ConversationThreadStatus.OPEN


__all__ = ["ConversationThread", "ConversationThreadStatus"]
