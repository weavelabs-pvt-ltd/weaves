"""Domain errors for optimistic skill lifecycle updates."""


class SkillRevisionConflict(ValueError):
    """Signal that a skill changed after the caller loaded it."""
