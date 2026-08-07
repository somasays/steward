"""steward-catalog: the deterministic, metadata-only catalog (FR1, issue #20).

Register a Postgres source, scan it, persist what it holds. No model is called
here and none will be: profiling, documentation and classification are later
slices that read what this one wrote.

The shape, in the order a scan moves through it:

* `secrets` -- a source row stores a secret *reference*; a resolver turns it
  into a `Secret`, which is the only thing the connector accepts (I5, N7).
* `inspector` -- the read side: a connection to the *customer's* database on a
  read-only role, whose type offers nothing but `inspect()`. Steward's own
  writes go through `steward_queue.QueueConnection`; the two are different
  types so they cannot be confused.
* `diff` -- `plan_convergence(stored, observed)`, a pure function. An unchanged
  source produces an empty plan, and an empty plan writes nothing: no row, no
  timestamp, no audit entry. That is what makes rescanning byte-identical (I8).
* `repository` -- applies a plan inside the caller's transaction, writing each
  mutation's audit row alongside it (I7). Lifecycle, never deletion: a dropped
  table becomes `missing` and keeps its row.
* `handler` -- `scan_source`, the single bounded task a scan run plans (#37).

Importing this package registers that handler with `steward_queue`, the same
way importing `steward_orchestration` registers its goals -- no setup call a
process could forget.
"""

from steward_catalog.cursor import InvalidCursor, decode_cursor, encode_cursor
from steward_catalog.diff import CatalogState, ConvergencePlan, plan_convergence
from steward_catalog.handler import (
    SCAN_SOURCE_SAMPLE_PAYLOAD,
    SCAN_SOURCE_TASK_TYPE,
    ScanSourcePayload,
    build_scan_source,
)
from steward_catalog.inspector import (
    SourceInspector,
    SourceInspectorFactory,
    open_source_connection,
    postgres_inspector,
)
from steward_catalog.models import (
    WORKSPACE_ID,
    AssetRecord,
    ColumnRecord,
    DiscoveredAsset,
    DiscoveredColumn,
    SchemaFilter,
    SourceKey,
    SourceRecord,
)
from steward_catalog.repository import (
    CATALOG_ENTITIES,
    apply_plan,
    get_asset,
    get_source,
    list_asset_columns,
    list_assets,
    load_state,
    register_source,
)
from steward_catalog.secrets import (
    EnvSecretResolver,
    MalformedSecretRef,
    Secret,
    SecretNotFound,
    SecretResolver,
)

__all__ = [
    "CATALOG_ENTITIES",
    "SCAN_SOURCE_SAMPLE_PAYLOAD",
    "SCAN_SOURCE_TASK_TYPE",
    "WORKSPACE_ID",
    "AssetRecord",
    "CatalogState",
    "ColumnRecord",
    "ConvergencePlan",
    "DiscoveredAsset",
    "DiscoveredColumn",
    "EnvSecretResolver",
    "InvalidCursor",
    "MalformedSecretRef",
    "SchemaFilter",
    "ScanSourcePayload",
    "Secret",
    "SecretNotFound",
    "SecretResolver",
    "SourceInspector",
    "SourceInspectorFactory",
    "SourceKey",
    "SourceRecord",
    "apply_plan",
    "build_scan_source",
    "decode_cursor",
    "encode_cursor",
    "get_asset",
    "get_source",
    "list_asset_columns",
    "list_assets",
    "load_state",
    "open_source_connection",
    "plan_convergence",
    "postgres_inspector",
    "register_source",
]
