"""Small, trusted instruction skills that can be pinned to agent versions."""

from dataclasses import dataclass

from contracts.v1 import AgentSkillSnapshot


@dataclass(frozen=True)
class AgentSkill:
    """A reusable instruction package that grants no tools or credentials."""

    skill_id: str
    name: str
    description: str
    version: int
    instructions: str

    def snapshot(self) -> AgentSkillSnapshot:
        """Return the immutable skill content stored with an agent version."""
        return AgentSkillSnapshot(
            skill_id=self.skill_id,
            name=self.name,
            version=self.version,
            instructions=self.instructions,
        )


_SKILLS = (
    AgentSkill(
        skill_id="evidence-based-research",
        name="Evidence-based research",
        description="Ground answers in connected sources and make uncertainty clear.",
        version=1,
        instructions=(
            "Research skill: use the available approved sources before answering. "
            "Separate source evidence from inference, cite source identifiers or "
            "titles when available, and say when evidence is missing or conflicts."
        ),
    ),
    AgentSkill(
        skill_id="concise-action-plan",
        name="Concise action plan",
        description="Turn findings into a short, ordered set of next steps.",
        version=1,
        instructions=(
            "Action-plan skill: when the user needs a plan, give ordered steps, "
            "identify dependencies, and distinguish recommendations from actions "
            "already performed. Never claim an action succeeded without evidence."
        ),
    ),
)
_SKILLS_BY_ID = {skill.skill_id: skill for skill in _SKILLS}


def list_agent_skills() -> tuple[AgentSkill, ...]:
    """Return the stable, non-secret skill catalog available to agent builders."""
    return _SKILLS


def snapshot_agent_skills(skill_ids: tuple[str, ...]) -> tuple[AgentSkillSnapshot, ...]:
    """Validate selected skill IDs and pin their current trusted instructions."""
    if len(skill_ids) != len(set(skill_ids)):
        raise ValueError("agent skills must be unique")
    unknown = set(skill_ids) - _SKILLS_BY_ID.keys()
    if unknown:
        raise ValueError(f"unknown agent skills: {', '.join(sorted(unknown))}")
    return tuple(_SKILLS_BY_ID[skill_id].snapshot() for skill_id in skill_ids)


__all__ = ["AgentSkill", "list_agent_skills", "snapshot_agent_skills"]
