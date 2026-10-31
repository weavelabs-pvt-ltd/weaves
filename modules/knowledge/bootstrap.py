"""Seed the default local knowledge documents."""

from __future__ import annotations

from datetime import datetime

from application.ports.repository import Repository
from contracts.v1 import KnowledgeDocument


class KnowledgeBootstrapService:
    """Create starter handbook documents for the default workspace."""

    def __init__(self, documents: Repository[KnowledgeDocument]) -> None:
        """Bind the knowledge document repository."""
        self.documents = documents

    def _seed(self, record: KnowledgeDocument) -> KnowledgeDocument:
        """Create a seeded document unless its deterministic ID already exists."""
        try:
            return self.documents.get(record.document_id)
        except KeyError:
            try:
                return self.documents.create(record)
            except ValueError:
                return self.documents.get(record.document_id)

    def seed_local_handbook(self, now: datetime) -> None:
        """Seed the local working agreements and security basics documents."""
        self._seed(
            KnowledgeDocument(
                document_id="starter-working-agreements",
                source_id="handbook:working-agreements",
                org_id="local-org",
                workspace_id="local-workspace",
                title="Working agreements",
                text=(
                    "Keep tasks small, document decisions, and ask for review "
                    "before production changes."
                ),
                is_seeded=True,
                created_at=now,
                updated_at=now,
            )
        )
        self._seed(
            KnowledgeDocument(
                document_id="starter-security-basics",
                source_id="handbook:security-basics",
                org_id="local-org",
                workspace_id="local-workspace",
                title="Security basics",
                text=(
                    "Use least privilege and never include credentials in prompts "
                    "or generated artifacts."
                ),
                is_seeded=True,
                created_at=now,
                updated_at=now,
            )
        )
