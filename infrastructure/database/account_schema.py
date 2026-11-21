"""Database-enforced uniqueness and lookup indexes for the account layer."""

# Applied idempotently on every start. They add indexes only, so an older
# release can roll back without knowing about them.
ACCOUNT_SCHEMA: tuple[str, ...] = (
    # A verified login identity belongs to exactly one account, platform-wide.
    """CREATE UNIQUE INDEX IF NOT EXISTS account_identity_verified_uq
           ON product_records
             ((payload->>'kind'), (COALESCE(payload->>'issuer', '')),
              (payload->>'subject'))
           WHERE collection = 'account_identities'
             AND payload->>'verified_at' IS NOT NULL""",
    # An account never holds the same identity twice, verified or not.
    """CREATE UNIQUE INDEX IF NOT EXISTS account_identity_owner_uq
           ON product_records
             ((payload->>'account_id'), (payload->>'kind'),
              (COALESCE(payload->>'issuer', '')), (payload->>'subject'))
           WHERE collection = 'account_identities'""",
    # At most one membership per account and organization, and one account per
    # tenant-local user. Reactivation updates the same row.
    """CREATE UNIQUE INDEX IF NOT EXISTS membership_account_org_uq
           ON product_records (org_id, (payload->>'account_id'))
           WHERE collection = 'memberships'""",
    """CREATE UNIQUE INDEX IF NOT EXISTS membership_user_uq
           ON product_records (org_id, (payload->>'user_id'))
           WHERE collection = 'memberships'""",
    """CREATE INDEX IF NOT EXISTS membership_account_idx
           ON product_records ((payload->>'account_id'))
           WHERE collection = 'memberships'""",
    # A session token digest resolves to exactly one account session.
    """CREATE UNIQUE INDEX IF NOT EXISTS account_session_token_uq
           ON product_records ((payload->>'token_digest'))
           WHERE collection = 'account_sessions'""",
    # Indexed lookups for the account layer; each serves one list_by_field,
    # find_by_field or list_before query so no sign-in path scans a collection.
    """CREATE INDEX IF NOT EXISTS account_identity_account_idx
           ON product_records ((payload->>'account_id'))
           WHERE collection = 'account_identities'""",
    """CREATE INDEX IF NOT EXISTS account_identity_subject_idx
           ON product_records ((payload->>'subject'))
           WHERE collection = 'account_identities'""",
    """CREATE INDEX IF NOT EXISTS membership_user_idx
           ON product_records ((payload->>'user_id'))
           WHERE collection = 'memberships'""",
    """CREATE INDEX IF NOT EXISTS account_session_account_idx
           ON product_records ((payload->>'account_id'))
           WHERE collection = 'account_sessions'""",
    # One Google sign-in attempt per state digest, so a callback finds it by an
    # index and a replayed or colliding state can never match two rows.
    """CREATE UNIQUE INDEX IF NOT EXISTS account_oauth_state_uq
           ON product_records ((payload->>'state_digest'))
           WHERE collection = 'account_oauth_states'""",
    # Bounded purge of expired sign-in states and expired or revoked sessions.
    """CREATE INDEX IF NOT EXISTS account_oauth_state_expiry_idx
           ON product_records ((payload->>'expires_at'), record_id)
           WHERE collection = 'account_oauth_states'""",
    """CREATE INDEX IF NOT EXISTS account_session_expiry_idx
           ON product_records ((payload->>'expires_at'), record_id)
           WHERE collection = 'account_sessions'""",
    """CREATE INDEX IF NOT EXISTS account_session_revoked_idx
           ON product_records ((payload->>'revoked_at'), record_id)
           WHERE collection = 'account_sessions'
             AND payload->>'revoked_at' IS NOT NULL""",
    # One idempotency record per account and key digest: a repeated key can
    # never create a second organization. Its account_id prefix also serves the
    # per-account quota count.
    """CREATE UNIQUE INDEX IF NOT EXISTS account_org_request_key_uq
           ON product_records
             ((payload->>'account_id'), (payload->>'key_digest'))
           WHERE collection = 'account_organization_requests'""",
    # Verified-email linking finds tenant users by normalized address.
    """CREATE INDEX IF NOT EXISTS user_email_lower_idx
           ON product_records ((lower(payload->>'email')))
           WHERE collection = 'users'""",
)
