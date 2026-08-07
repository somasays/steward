"""Every statement the catalog runs against Steward's own database, as a
module-level constant.

I5: SQL is never assembled from strings. Nothing here is an f-string, a
`.format()`, a `%`-format or a concatenation -- the only variability is
server-side parameter binding (`%(name)s` placeholders, bound by psycopg).
That is also what keeps ruff S608 (S3) quiet without a single pragma.

Private module on purpose, the same way `steward_queue._sql` is: SQL text is an
implementation detail of the module that runs it.
"""

# Registration is idempotent on the natural key (issue #20). `DO NOTHING`
# rather than `DO UPDATE`: a second registration of the same database subset is
# the same source, and letting it rewrite `name` or the secret reference would
# make the *last* caller win silently. The caller re-reads the existing row.
INSERT_SOURCE = """
INSERT INTO sources (id, workspace_id, name, engine, host, database_name,
                     include_schemas, exclude_schemas, dsn_secret_ref, scan_schedule)
VALUES (%(id)s, %(workspace_id)s, %(name)s, %(engine)s, %(host)s, %(database_name)s,
        %(include_schemas)s, %(exclude_schemas)s, %(dsn_secret_ref)s, %(scan_schedule)s)
ON CONFLICT (workspace_id, engine, host, database_name, include_schemas, exclude_schemas)
DO NOTHING
RETURNING id, workspace_id, name, engine, host, database_name, include_schemas, exclude_schemas,
          dsn_secret_ref, scan_schedule, created_at, updated_at
"""

SELECT_SOURCE_BY_KEY = """
SELECT id, workspace_id, name, engine, host, database_name, include_schemas, exclude_schemas,
       dsn_secret_ref, scan_schedule, created_at, updated_at
FROM sources
WHERE workspace_id = %(workspace_id)s
  AND engine = %(engine)s
  AND host = %(host)s
  AND database_name = %(database_name)s
  AND include_schemas = %(include_schemas)s
  AND exclude_schemas = %(exclude_schemas)s
"""

SELECT_SOURCE = """
SELECT id, workspace_id, name, engine, host, database_name, include_schemas, exclude_schemas,
       dsn_secret_ref, scan_schedule, created_at, updated_at
FROM sources
WHERE id = %(id)s
"""

SELECT_SOURCE_ASSETS = """
SELECT a.id, a.workspace_id, a.source_id, s.database_name, a.schema_name, a.name, a.asset_type,
       a.lifecycle, a.created_at, a.updated_at
FROM assets AS a
JOIN sources AS s ON s.id = a.source_id
WHERE a.source_id = %(source_id)s
ORDER BY a.schema_name, a.name
"""

SELECT_SOURCE_COLUMNS = """
SELECT c.id, c.workspace_id, c.asset_id, c.name, c.data_type, c.ordinal, c.nullable, c.lifecycle,
       c.created_at, c.updated_at
FROM columns AS c
JOIN assets AS a ON a.id = c.asset_id
WHERE a.source_id = %(source_id)s
ORDER BY c.asset_id, c.name
"""

INSERT_ASSET = """
INSERT INTO assets (id, workspace_id, source_id, schema_name, name, asset_type, lifecycle)
VALUES (%(id)s, %(workspace_id)s, %(source_id)s, %(schema_name)s, %(name)s, %(asset_type)s, 'active')
"""

# Guarded by `IS DISTINCT FROM`, and that guard is the whole convergence
# argument at the SQL level: the planner already decided nothing changed, and
# this makes a redundant write a no-op even if a concurrent scan raced ahead --
# `updated_at` never moves for a row whose facts did not.
UPDATE_ASSET = """
UPDATE assets
SET asset_type = %(asset_type)s, lifecycle = %(lifecycle)s, updated_at = now()
WHERE id = %(id)s
  AND (asset_type IS DISTINCT FROM %(asset_type)s OR lifecycle IS DISTINCT FROM %(lifecycle)s)
"""

MARK_ASSET_MISSING = """
UPDATE assets
SET lifecycle = 'missing', updated_at = now()
WHERE id = %(id)s AND lifecycle IS DISTINCT FROM 'missing'
"""

INSERT_COLUMN = """
INSERT INTO columns (id, workspace_id, asset_id, name, data_type, ordinal, nullable, lifecycle)
VALUES (%(id)s, %(workspace_id)s, %(asset_id)s, %(name)s, %(data_type)s, %(ordinal)s,
        %(nullable)s, 'active')
"""

UPDATE_COLUMN = """
UPDATE columns
SET data_type = %(data_type)s, ordinal = %(ordinal)s, nullable = %(nullable)s,
    lifecycle = %(lifecycle)s, updated_at = now()
WHERE id = %(id)s
  AND (data_type IS DISTINCT FROM %(data_type)s
       OR ordinal IS DISTINCT FROM %(ordinal)s
       OR nullable IS DISTINCT FROM %(nullable)s
       OR lifecycle IS DISTINCT FROM %(lifecycle)s)
"""

MARK_COLUMN_MISSING = """
UPDATE columns
SET lifecycle = 'missing', updated_at = now()
WHERE id = %(id)s AND lifecycle IS DISTINCT FROM 'missing'
"""

# The listing keyset (SPEC.md §8: cursor pagination). `(schema_name, name, id)`
# is a total order, so a cursor resumes at exactly one row -- an offset would
# skip or repeat rows when a scan commits between two pages.
SELECT_ASSETS_PAGE = """
SELECT a.id, a.workspace_id, a.source_id, s.database_name, a.schema_name, a.name, a.asset_type,
       a.lifecycle, a.created_at, a.updated_at
FROM assets AS a
JOIN sources AS s ON s.id = a.source_id
WHERE (%(source_id)s::uuid IS NULL OR a.source_id = %(source_id)s::uuid)
  AND (%(after_schema)s::text IS NULL
       OR (a.schema_name, a.name, a.id) > (%(after_schema)s::text, %(after_name)s::text, %(after_id)s::uuid))
ORDER BY a.schema_name, a.name, a.id
LIMIT %(limit)s
"""

SELECT_ASSET = """
SELECT a.id, a.workspace_id, a.source_id, s.database_name, a.schema_name, a.name, a.asset_type,
       a.lifecycle, a.created_at, a.updated_at
FROM assets AS a
JOIN sources AS s ON s.id = a.source_id
WHERE a.id = %(id)s
"""

SELECT_ASSET_COLUMNS = """
SELECT id, workspace_id, asset_id, name, data_type, ordinal, nullable, lifecycle,
       created_at, updated_at
FROM columns
WHERE asset_id = %(asset_id)s
ORDER BY ordinal, name
"""
