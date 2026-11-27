"""Compose the Slack access service (settings, allowlists, recent attempts)."""

from apps.capability_services import CapabilityServiceSet
from apps.runtime_composition_inputs import RuntimeCompositionInputs
from modules.slack.access.allowlists import Allowlists
from modules.slack.access.context import AccessContext
from modules.slack.access.members import MemberDirectory
from modules.slack.access.service import SlackAccessService


def configure_slack_access_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Share the organization guard's repositories and transaction lock."""
    guard = services.organization_administration_service.guard
    context = AccessContext(
        settings=dependencies.slack_access_settings,
        audit_events=dependencies.audit_events,
        authorize_organization=dependencies.authorize_organization,
        unit_of_work=dependencies._unit_of_work,
        lock=dependencies._advisory_transaction_lock,
    )
    allowlists = Allowlists(
        context,
        users=dependencies.slack_allowed_users,
        channels=dependencies.slack_allowed_channels,
        members=MemberDirectory(guard),
    )
    services.slack_access_service = SlackAccessService(
        context,
        allowlists,
        installations=dependencies.installations,
        model_profiles=dependencies.model_profiles,
        audit_events=dependencies.audit_events,
        agents=dependencies.agents,
    )
