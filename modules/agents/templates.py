"""Built-in agent templates kept separate from persisted agent definitions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentTemplate:
    template_id: str
    name: str
    description: str
    persona: str
    instructions: str
    recommended_capability_ids: tuple[str, ...]


_TEMPLATES = {
    "engineering-assistant": AgentTemplate(
        template_id="engineering-assistant",
        name="Engineering Assistant",
        description=(
            "Research engineering work across approved GitHub, Slack, Jira, "
            "and workspace knowledge sources."
        ),
        persona="A careful engineering operations assistant.",
        instructions=(
            "Answer engineering questions using only the approved context "
            "returned by connected capabilities. Search relevant sources before "
            "drawing conclusions. Distinguish source facts from inference, cite "
            "the source titles available in context, and call out missing or "
            "conflicting evidence. Never claim to have changed a ticket, "
            "repository, or message. Suggest next steps without taking write "
            "actions."
        ),
        recommended_capability_ids=(
            "knowledge.search",
            "github.issues.search",
            "slack.messages.search",
            "jira.issues.search",
        ),
    )
}


def get_agent_template(template_id: str) -> AgentTemplate:
    """Return a registered immutable template or raise ``KeyError``."""
    try:
        return _TEMPLATES[template_id]
    except KeyError:
        raise KeyError(f"agent template not found: {template_id}") from None


def list_agent_templates() -> tuple[AgentTemplate, ...]:
    """Return built-in templates in stable order."""
    return tuple(_TEMPLATES[key] for key in sorted(_TEMPLATES))


__all__ = ["AgentTemplate", "get_agent_template", "list_agent_templates"]
