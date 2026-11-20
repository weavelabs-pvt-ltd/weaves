"""Additive catalog ownership tables and integrity guards for migration 20."""

TABLES = (
    """CREATE TABLE product_plugin_ownership (
       plugin_id text PRIMARY KEY,
       record_collection text NOT NULL DEFAULT 'plugins'
         CHECK (record_collection='plugins'),
       state text NOT NULL CHECK(state IN ('builtin','owned','quarantined')),
       org_id text,
       reason text,
       source_org_ids text[] NOT NULL,
       created_at timestamptz NOT NULL DEFAULT now(),
       FOREIGN KEY(record_collection,plugin_id)
         REFERENCES product_records(collection,record_id) ON DELETE CASCADE,
       CHECK ((state='owned' AND org_id IS NOT NULL AND reason IS NULL)
          OR (state='builtin' AND org_id IS NULL AND reason IS NULL)
          OR (state='quarantined' AND org_id IS NULL AND reason IS NOT NULL))
    )""",
    """CREATE TABLE product_plugin_ownership_rollout (
       version integer PRIMARY KEY CHECK(version=20),
       reader_image text NOT NULL,
       counts jsonb NOT NULL,
       applied_at timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE INDEX product_plugin_owner_org_idx
       ON product_plugin_ownership(org_id,plugin_id) WHERE state='owned'""",
    """CREATE INDEX product_installations_plugin_owner_idx
       ON product_records((payload->>'plugin_id'),org_id,record_id)
       WHERE collection='plugin_installations'""",
)

GUARDS = (
    """CREATE FUNCTION guard_plugin_record() RETURNS trigger
       LANGUAGE plpgsql AS $$
       DECLARE owner product_plugin_ownership%ROWTYPE;
       BEGIN
         IF TG_OP='DELETE' THEN
           IF OLD.collection <> 'plugins' THEN RETURN OLD; END IF;
           SELECT * INTO owner FROM product_plugin_ownership WHERE
       plugin_id=OLD.record_id;
           IF owner.state IN ('builtin','quarantined') OR EXISTS
             (SELECT 1 FROM product_records WHERE collection='plugin_installations'
              AND payload->>'plugin_id'=OLD.record_id) THEN
             RAISE EXCEPTION 'Descriptor requires operator resolution or has
       installations'
               USING ERRCODE='23514';
           END IF;
           RETURN OLD;
         END IF;
         IF TG_OP='UPDATE' AND OLD.collection='plugins' AND
            (NEW.collection<>'plugins' OR NEW.record_id<>OLD.record_id) THEN
           RAISE EXCEPTION 'Descriptor identity is immutable' USING ERRCODE='23514';
         END IF;
         IF NEW.collection<>'plugins' THEN RETURN NEW; END IF;
         IF NEW.workspace_id IS NOT NULL THEN
           RAISE EXCEPTION 'Descriptor cannot have workspace ownership'
             USING ERRCODE='23514';
         END IF;
         IF NEW.payload->>'plugin_id' IS DISTINCT FROM NEW.record_id THEN
           RAISE EXCEPTION 'Descriptor identity mismatch' USING ERRCODE='23514';
         END IF;
         IF TG_OP='UPDATE' THEN
           SELECT * INTO owner FROM product_plugin_ownership WHERE
       plugin_id=OLD.record_id;
           IF NOT FOUND OR NEW.org_id IS DISTINCT FROM owner.org_id OR
              NEW.payload->>'plugin_type' IS DISTINCT FROM
       OLD.payload->>'plugin_type' THEN
             RAISE EXCEPTION 'Descriptor ownership and type are immutable' USING
       ERRCODE='23514';
           END IF;
         END IF;
         IF NEW.org_id IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM product_records WHERE collection='organizations' AND
       record_id=NEW.org_id) THEN
           RAISE EXCEPTION 'Descriptor organization does not exist' USING
       ERRCODE='23514';
         END IF;
         RETURN NEW;
       END $$""",
    """CREATE FUNCTION register_plugin_owner() RETURNS trigger
       LANGUAGE plpgsql AS $$
       DECLARE builtin boolean;
       BEGIN
         IF NEW.collection<>'plugins' THEN RETURN NEW; END IF;
         builtin := NEW.org_id IS NULL AND
           ((NEW.record_id='github' AND NEW.payload->>'plugin_type'='github') OR
            (NEW.record_id='slack' AND NEW.payload->>'plugin_type'='slack') OR
            (NEW.record_id='jira' AND NEW.payload->>'plugin_type'='jira_cloud'));
         INSERT INTO
       product_plugin_ownership(plugin_id,state,org_id,reason,source_org_ids)
           VALUES (NEW.record_id,
             CASE WHEN builtin THEN 'builtin'
                  WHEN NEW.org_id IS NOT NULL AND NEW.record_id NOT IN
       ('github','slack','jira')
                    THEN 'owned' ELSE 'quarantined' END,
             CASE WHEN NOT builtin AND NEW.record_id NOT IN ('github','slack','jira')
                  THEN NEW.org_id ELSE NULL END,
             CASE WHEN builtin OR (NEW.org_id IS NOT NULL AND
                  NEW.record_id NOT IN ('github','slack','jira')) THEN NULL
                  ELSE 'unowned_or_reserved_descriptor' END,
             CASE WHEN NEW.org_id IS NULL THEN ARRAY[]::text[] ELSE
       ARRAY[NEW.org_id] END);
         IF NEW.record_id IN ('github','slack','jira') AND NOT builtin AND
       NEW.org_id IS NOT NULL THEN
           RAISE EXCEPTION 'Reserved descriptor cannot be organization owned' USING
       ERRCODE='23514';
         END IF;
         RETURN NEW;
       END $$""",
    """CREATE FUNCTION guard_plugin_ownership() RETURNS trigger
       LANGUAGE plpgsql AS $$
       BEGIN
         IF TG_OP='DELETE' THEN
           IF EXISTS(SELECT 1 FROM product_records
                     WHERE collection='plugins' AND record_id=OLD.plugin_id)
              OR EXISTS(SELECT 1 FROM product_records
                        WHERE collection='plugin_installations'
                        AND payload->>'plugin_id'=OLD.plugin_id) THEN
             RAISE EXCEPTION 'Ownership cannot be removed independently' USING
       ERRCODE='23514';
           END IF;
           RETURN OLD;
         END IF;
         IF NEW IS DISTINCT FROM OLD THEN
           RAISE EXCEPTION 'Descriptor ownership and quarantine are immutable' USING
       ERRCODE='23514';
         END IF;
         RETURN NEW;
       END $$""",
    """CREATE FUNCTION guard_plugin_installation_owner() RETURNS trigger
       LANGUAGE plpgsql AS $$
       DECLARE owner product_plugin_ownership%ROWTYPE;
       BEGIN
         IF TG_OP='UPDATE' AND OLD.collection='plugin_installations' AND
            (NEW.collection<>'plugin_installations' OR NEW.record_id<>OLD.record_id)
       THEN
           RAISE EXCEPTION 'Installation identity is immutable' USING ERRCODE='23514';
         END IF;
         IF NEW.collection<>'plugin_installations' THEN RETURN NEW; END IF;
         IF TG_OP='UPDATE' AND OLD.collection='plugin_installations' AND
            (NEW.org_id IS DISTINCT FROM OLD.org_id OR
             NEW.workspace_id IS DISTINCT FROM OLD.workspace_id OR
             NEW.payload->>'plugin_id' IS DISTINCT FROM OLD.payload->>'plugin_id') THEN
           RAISE EXCEPTION 'Installation ownership and descriptor are immutable'
             USING ERRCODE='23514';
         END IF;
         IF NEW.payload->>'org_id' IS DISTINCT FROM NEW.org_id OR
            NEW.payload->>'workspace_id' IS DISTINCT FROM NEW.workspace_id OR
            NEW.payload->>'plugin_installation_id' IS DISTINCT FROM NEW.record_id OR
       NOT EXISTS
            (SELECT 1 FROM product_records WHERE collection='workspaces'
             AND record_id=NEW.workspace_id AND org_id=NEW.org_id) THEN
           RAISE EXCEPTION 'Installation scope is invalid' USING ERRCODE='23514';
         END IF;
         SELECT * INTO owner FROM product_plugin_ownership
           WHERE plugin_id=NEW.payload->>'plugin_id' FOR KEY SHARE;
         IF NOT FOUND OR owner.state='quarantined' OR
            (owner.state='owned' AND owner.org_id IS DISTINCT FROM NEW.org_id) THEN
           RAISE EXCEPTION 'Installation descriptor is outside organization scope'
       USING ERRCODE='23514';
         END IF;
         RETURN NEW;
       END $$""",
    """CREATE TRIGGER plugin_record_guard BEFORE INSERT OR UPDATE OR DELETE
       ON product_records FOR EACH ROW EXECUTE FUNCTION guard_plugin_record()""",
    """CREATE TRIGGER plugin_record_owner AFTER INSERT ON product_records
       FOR EACH ROW EXECUTE FUNCTION register_plugin_owner()""",
    """CREATE TRIGGER plugin_ownership_guard BEFORE UPDATE OR DELETE
       ON product_plugin_ownership FOR EACH ROW EXECUTE FUNCTION
       guard_plugin_ownership()""",
    """CREATE TRIGGER plugin_installation_owner_guard BEFORE INSERT OR UPDATE
       ON product_records FOR EACH ROW EXECUTE FUNCTION
       guard_plugin_installation_owner()""",
    """CREATE FUNCTION guard_plugin_rollout() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
         RAISE EXCEPTION 'Catalog rollout evidence is immutable' USING ERRCODE='23514';
       END $$""",
    """CREATE TRIGGER plugin_rollout_guard BEFORE UPDATE OR DELETE
       ON product_plugin_ownership_rollout FOR EACH ROW
       EXECUTE FUNCTION guard_plugin_rollout()""",
)

# The query returns metadata only. Tool schemas, display names and credentials
# never enter the operator report. A disabled reference still proves ownership.
CLASSIFY = """WITH refs AS (
 SELECT i.payload->>'plugin_id' AS plugin_id,
        array_agg(DISTINCT i.org_id ORDER BY i.org_id) FILTER(WHERE i.org_id IS NOT
       NULL) AS orgs,
        bool_or(i.org_id IS NULL OR i.payload->>'org_id' IS DISTINCT FROM i.org_id
          OR i.payload->>'workspace_id' IS DISTINCT FROM i.workspace_id
          OR NOT EXISTS(SELECT 1 FROM product_records w WHERE w.collection='workspaces'
                        AND w.record_id=i.workspace_id AND w.org_id=i.org_id)
          OR NOT EXISTS(SELECT 1 FROM product_records o WHERE
       o.collection='organizations'
                        AND o.record_id=i.org_id)) AS invalid
 FROM product_records i WHERE i.collection='plugin_installations'
 GROUP BY i.payload->>'plugin_id'
), candidates AS (
 SELECT p.record_id AS plugin_id, p.org_id AS recorded_org,
        p.payload->>'plugin_id' IS DISTINCT FROM p.record_id AS invalid_identity,
        (p.record_id='github' AND p.payload->>'plugin_type'='github') OR
        (p.record_id='slack' AND p.payload->>'plugin_type'='slack') OR
        (p.record_id='jira' AND p.payload->>'plugin_type'='jira_cloud') AS builtin,
        p.record_id IN ('github','slack','jira') AS reserved,
        COALESCE(r.invalid,false) OR (p.org_id IS NOT NULL AND NOT EXISTS
          (SELECT 1 FROM product_records o WHERE o.collection='organizations'
           AND o.record_id=p.org_id)) AS invalid_scope,
        ARRAY(SELECT DISTINCT value FROM unnest(COALESCE(r.orgs,ARRAY[]::text[]) ||
          CASE WHEN p.org_id IS NULL THEN ARRAY[]::text[] ELSE ARRAY[p.org_id] END)
       value
          ORDER BY value) AS orgs
 FROM product_records p LEFT JOIN refs r ON r.plugin_id=p.record_id
 WHERE p.collection='plugins'
), classified AS (
 SELECT *, CASE
   WHEN invalid_identity THEN 'identity_mismatch'
   WHEN reserved AND (NOT COALESCE(builtin,false) OR recorded_org IS NOT NULL)
     THEN 'reserved_builtin_identity'
   WHEN COALESCE(builtin,false) THEN NULL
   WHEN invalid_scope THEN 'invalid_installation_scope'
   WHEN cardinality(orgs)=0 THEN 'orphaned_descriptor'
   WHEN cardinality(orgs)>1 THEN 'conflicting_organizations'
   ELSE NULL END AS reason
 FROM candidates
)
SELECT plugin_id,
 CASE WHEN reason IS NOT NULL THEN 'quarantined'
      WHEN COALESCE(builtin,false) THEN 'builtin' ELSE 'owned' END AS state,
 CASE WHEN reason IS NULL AND NOT COALESCE(builtin,false) THEN orgs[1] ELSE NULL END
       AS org_id,
 reason, orgs FROM classified ORDER BY plugin_id"""
