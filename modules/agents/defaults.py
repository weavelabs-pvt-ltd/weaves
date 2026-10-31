"""Default agent selection for workspaces that omit an explicit agent."""

from application.ports.repository import Repository
from contracts.v1 import AgentDefinition, AgentStatus
from shared.request_context import effective_org_id, effective_workspace_id


def default_active_agent_id(agents: Repository[AgentDefinition]) -> str:
    """Return the seeded or first active agent in the selected workspace."""
    org_id = effective_org_id()
    workspace_id = effective_workspace_id()
    try:
        seeded = agents.get_scoped("local-assistant", org_id, workspace_id)
    except KeyError:
        seeded = None
    if seeded is not None and seeded.status is AgentStatus.ACTIVE:
        return seeded.id
    active = sorted(
        (
            agent
            for agent in agents.list_scoped(org_id, workspace_id)
            if agent.status is AgentStatus.ACTIVE
        ),
        key=lambda agent: agent.id,
    )
    if not active:
        raise KeyError("no active agent is configured in this workspace")
    return active[0].id
