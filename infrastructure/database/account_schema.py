"""Database-enforced uniqueness for accounts, identities and memberships."""

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
)
