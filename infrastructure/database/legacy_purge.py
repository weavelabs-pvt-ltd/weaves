"""Migration 22: remove the rows and schema objects of retired sign-in concepts.

Legacy tenant sign-in (passwords, tenant email codes, invitations, tenant
sessions, organization SSO) and two earlier retired features (memory items,
knowledge documents) no longer exist in the code. This migration deletes what
they left behind in an existing database. Objects created by migrations 6, 19
and the retired account challenge indexes stay in migration history; this
migration is what removes them, so a new database and an old database converge
on the same schema.

Every statement is idempotent (``IF EXISTS``, plain ``DELETE``) so the
migration is safe on an empty database and when re-run by hand.
"""

from __future__ import annotations

# Collections that used to hold records and no longer exist in
# ``REPOSITORY_SPECS``. A test requires every collection in the store to be
# either in the registry or in this tuple, so new drift cannot go unnoticed.
PURGED_COLLECTIONS: tuple[str, ...] = (
    "user_passwords",
    "user_password_credentials",
    "user_sessions",
    "user_invitations",
    "sso_connections",
    "sso_external_identities",
    "email_login_challenges",
    "account_login_challenges",
    "memory_items",
    "knowledge_documents",
)

# Indexes whose only purpose was one of the purged collections. The users
# lower(email) index is NOT here: verified-email account linking still uses it.
DROPPED_INDEXES: tuple[str, ...] = (
    "product_records_user_session_digest_uq",
    "product_records_user_invitation_digest_uq",
    "sso_identity_subject_uq",
    "sso_identity_user_connection_uq",
    "sso_connections_org_provider_idx",
    "account_challenge_email_idx",
    "account_challenge_expiry_idx",
)

# Constants only (never user input), so inlining them as literals is safe.
_COLLECTION_LITERALS = ", ".join(f"'{name}'" for name in PURGED_COLLECTIONS)

LEGACY_PURGE_MIGRATION: tuple[int, tuple[str, ...]] = (
    22,
    (
        # The immutability trigger would reject touching SSO rows; drop it first.
        "DROP TRIGGER IF EXISTS sso_identity_immutable ON product_records",
        "DROP FUNCTION IF EXISTS reject_sso_identity_rewrite()",
        f"DELETE FROM product_records WHERE collection IN ({_COLLECTION_LITERALS})",
        *(f"DROP INDEX IF EXISTS {name}" for name in DROPPED_INDEXES),
    ),
)
