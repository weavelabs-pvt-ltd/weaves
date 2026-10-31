"""Single-use workspace invitation acceptance and account activation."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    Principal,
    PrincipalStatus,
    PrincipalType,
    User,
    UserInvitation,
    UserInvitationStatus,
    UserPasswordCredential,
    UserSession,
    UserSessionStatus,
    UserStatus,
    Workspace,
    WorkspaceStatus,
)
from modules.auth.passwords import _hash_password
from modules.auth.runtime_invitations import InvitationRuntimeHost
from shared.clock import new_id as _id
from shared.clock import utc_now as _now


class InvitationAcceptanceMixin(InvitationRuntimeHost):
    """Validate invite tokens and activate the invited account atomically."""

    def accept_user_invitation(
        self, invitation_token: str, password: str
    ) -> tuple[User, Principal, UserSession, str]:
        """Consume an invite once, set its initial password, and start a session."""
        if not self._bootstrapped():
            self.bootstrap()
        if len(password) < 12 or len(password) > 1024 or not password.strip():
            raise ValueError(
                "password must contain 12 to 1024 non-whitespace characters"
            )
        digest = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
        invitation = self.user_invitations.find_by_field("token_digest", digest)
        timestamp = _now()
        if not self._invitation_is_usable(invitation, timestamp):
            raise PermissionError("invitation is invalid or expired")
        assert invitation is not None
        user, principal, workspace = self._invitation_target_records(invitation)
        active_user = User.model_validate(
            user.model_copy(
                update={"status": UserStatus.ACTIVE, "updated_at": timestamp}
            ).model_dump()
        )
        active_principal = Principal.model_validate(
            principal.model_copy(
                update={"status": PrincipalStatus.ACTIVE, "updated_at": timestamp}
            ).model_dump()
        )
        accepted = UserInvitation.model_validate(
            invitation.model_copy(
                update={
                    "status": UserInvitationStatus.ACCEPTED,
                    "accepted_at": timestamp,
                    "updated_at": timestamp,
                }
            ).model_dump()
        )
        password_credential = UserPasswordCredential(
            id=user.id,
            org_id=user.org_id,
            password_hash=_hash_password(password),
            changed_by_principal_id=principal.id,
            created_at=timestamp,
            updated_at=timestamp,
        )
        session_token = secrets.token_urlsafe(32)
        session = UserSession(
            id=_id(),
            org_id=principal.org_id,
            principal_id=principal.id,
            token_digest=hashlib.sha256(session_token.encode("utf-8")).hexdigest(),
            status=UserSessionStatus.ACTIVE,
            expires_at=timestamp + timedelta(hours=12),
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._accept_invitation_records(
            accepted,
            active_principal,
            active_user,
            invitation,
            password_credential,
            principal,
            session,
            timestamp,
            user,
            workspace,
        )
        return active_user, active_principal, session, session_token

    @staticmethod
    def _invitation_is_usable(
        invitation: UserInvitation | None, timestamp: Any
    ) -> bool:
        return (
            invitation is not None
            and invitation.status is UserInvitationStatus.PENDING
            and invitation.expires_at > timestamp
        )

    def _invitation_target_records(
        self, invitation: UserInvitation
    ) -> tuple[User, Principal, Workspace]:
        user = self.users.get_scoped(invitation.user_id, invitation.org_id)
        principal = self.principals.get_scoped(
            invitation.principal_id, invitation.org_id
        )
        workspace = self.workspaces.get_scoped(
            invitation.workspace_id, invitation.org_id
        )
        valid = (
            user.status is UserStatus.INVITED
            and principal.status is PrincipalStatus.DISABLED
            and principal.principal_type is PrincipalType.USER
            and principal.user_id == user.id
            and workspace.status is WorkspaceStatus.ACTIVE
        )
        if not valid:
            raise PermissionError("invitation is invalid or expired")
        return user, principal, workspace

    def _accept_invitation_records(
        self,
        accepted: Any,
        active_principal: Any,
        active_user: Any,
        invitation: Any,
        password_credential: Any,
        principal: Any,
        session: Any,
        timestamp: Any,
        user: Any,
        workspace: Any,
    ) -> None:
        """Persist invitation acceptance and membership."""
        with self._unit_of_work():
            if not self.user_invitations.put_if_invitation_pending(
                accepted, expected_token_digest=invitation.token_digest
            ):
                raise PermissionError("invitation is invalid or expired")
            self.users.put(active_user)
            self.principals.put(active_principal)
            self.user_passwords.create(password_credential)
            self.user_sessions.create(session)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=user.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=workspace.id,
                    actor_principal_id=principal.id,
                    action="user.invitation_accepted",
                    target_type="user_invitation",
                    target_id=invitation.invitation_id,
                    summary="Workspace user invitation accepted",
                    request_id=invitation.invitation_id,
                    metadata={"user_id": user.id},
                    created_at=timestamp,
                )
            )
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=user.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=workspace.id,
                    actor_principal_id=principal.id,
                    action="user.session_created",
                    target_type="user_session",
                    target_id=session.id,
                    summary="User signed in by accepting an invitation",
                    request_id=session.id,
                    created_at=timestamp,
                )
            )
