"""Migration 23: organization URL slugs and claimed email domains.

Both values live in the tenant-owned ``organization_settings`` payload. This
migration backfills existing organizations and then adds the two unique
indexes that enforce global uniqueness. Every statement is idempotent: the
backfills touch only rows that still lack a value, so running them again
changes nothing.

Slug backfill: the same rule as ``modules.auth.organization_slugs.slugify``
(lowercase, runs of anything outside ``a-z0-9`` become one hyphen, at most 40
characters, ``-org`` appended when shorter than 3), in creation order, with a
numeric suffix (``-2``, ``-3``...) when the slug is reserved or already held.

Email domain backfill: an organization claims the domain of its explicit
owner's email only when that address is a verified email identity of the
account linked to the owner by an active membership, the domain is not a
public mailbox provider, and exactly one organization would claim it. Anything
ambiguous is left unset and reported with a NOTICE; nothing is guessed.
"""

from __future__ import annotations

from shared.organization_naming import PUBLIC_EMAIL_DOMAINS, RESERVED_SLUGS


def _literals(values: frozenset[str]) -> str:
    """Inline constants (never user input) as a SQL text array body."""
    return ", ".join(f"'{value}'" for value in sorted(values))


SLUG_BACKFILL = f"""DO $$
DECLARE
  item record;
  base text;
  candidate text;
  suffix text;
  n integer;
BEGIN
  FOR item IN
    SELECT s.record_id, COALESCE(o.payload->>'name', '') AS name
      FROM product_records s
      LEFT JOIN product_records o
        ON o.collection = 'organizations' AND o.record_id = s.record_id
     WHERE s.collection = 'organization_settings'
       AND COALESCE(s.payload->>'slug', '') = ''
     ORDER BY s.created_at, s.record_id
  LOOP
    base := btrim(regexp_replace(lower(item.name), '[^a-z0-9]+', '-', 'g'), '-');
    base := btrim(left(base, 40), '-');
    IF length(base) < 3 THEN
      base := btrim(base || '-org', '-');
    END IF;
    candidate := base;
    n := 1;
    WHILE candidate = ANY (ARRAY[{_literals(RESERVED_SLUGS)}])
       OR EXISTS (
            SELECT 1 FROM product_records
             WHERE collection = 'organization_settings'
               AND lower(payload->>'slug') = candidate)
    LOOP
      n := n + 1;
      suffix := '-' || n;
      candidate := btrim(left(base, 40 - length(suffix)), '-') || suffix;
    END LOOP;
    UPDATE product_records
       SET payload = jsonb_set(payload, '{{slug}}', to_jsonb(candidate))
     WHERE collection = 'organization_settings' AND record_id = item.record_id;
  END LOOP;
END $$"""

EMAIL_DOMAIN_BACKFILL = f"""DO $$
DECLARE
  item record;
BEGIN
  FOR item IN
    WITH candidates AS (
      SELECT s.record_id AS org_id,
             lower(split_part(u.payload->>'email', '@', 2)) AS domain
        FROM product_records s
        JOIN product_records o
          ON o.collection = 'organizations' AND o.record_id = s.record_id
         AND o.payload->>'status' <> 'deleted'
        JOIN product_records p
          ON p.collection = 'principals' AND p.org_id = s.record_id
         AND p.record_id = s.payload->>'owner_principal_id'
        JOIN product_records u
          ON u.collection = 'users' AND u.org_id = s.record_id
         AND u.record_id = p.payload->>'user_id'
        JOIN product_records m
          ON m.collection = 'memberships' AND m.org_id = s.record_id
         AND m.payload->>'principal_id' = p.record_id
         AND m.payload->>'status' = 'active'
       WHERE s.collection = 'organization_settings'
         AND COALESCE(s.payload->>'email_domain', '') = ''
         AND EXISTS (
               SELECT 1 FROM product_records i
                WHERE i.collection = 'account_identities'
                  AND i.payload->>'account_id' = m.payload->>'account_id'
                  AND i.payload->>'kind' = 'email'
                  AND i.payload->>'verified_at' IS NOT NULL
                  AND i.payload->>'subject' = lower(u.payload->>'email'))
    )
    SELECT org_id, domain, count(*) OVER (PARTITION BY domain) AS claimants
      FROM candidates
     WHERE domain ~ '^[a-z0-9.-]{{3,253}}$'
       AND domain <> ALL (ARRAY[{_literals(PUBLIC_EMAIL_DOMAINS)}])
     ORDER BY org_id
  LOOP
    IF item.claimants > 1 OR EXISTS (
         SELECT 1 FROM product_records
          WHERE collection = 'organization_settings'
            AND lower(payload->>'email_domain') = item.domain)
    THEN
      RAISE NOTICE 'organization % keeps no email domain: % is not uniquely its own',
        item.org_id, item.domain;
      CONTINUE;
    END IF;
    UPDATE product_records
       SET payload = jsonb_set(payload, '{{email_domain}}', to_jsonb(item.domain))
     WHERE collection = 'organization_settings' AND record_id = item.org_id;
  END LOOP;
END $$"""

ORGANIZATION_IDENTITY_MIGRATION: tuple[int, tuple[str, ...]] = (
    23,
    (
        SLUG_BACKFILL,
        EMAIL_DOMAIN_BACKFILL,
        # One organization per slug, compared without case. The settings row
        # is purged with the organization, which is what frees the slug.
        """CREATE UNIQUE INDEX IF NOT EXISTS organization_slug_uq
               ON product_records ((lower(payload->>'slug')))
               WHERE collection = 'organization_settings'""",
        # One organization per claimed custom email domain.
        """CREATE UNIQUE INDEX IF NOT EXISTS organization_email_domain_uq
               ON product_records ((lower(payload->>'email_domain')))
               WHERE collection = 'organization_settings'""",
    ),
)
