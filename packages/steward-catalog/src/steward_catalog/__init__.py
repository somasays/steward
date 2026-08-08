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

Profiling (issue #49) is the second slice and the first that reads customer
*data* rather than metadata, so it adds a layer the metadata half did not need:

* `masking` -- `RawCell` in, `MaskedSample` out, and nothing else. The only
  path from a sampled value to anything that persists, is returned, or is put
  in front of a model (I6).
* `profiler` -- the read side for data: statistics and a masked sample per
  column, through the same read-only connection the inspector uses (I5).
* `profiles` -- append-only versioned persistence, where a profile equal to the
  stored one writes nothing at all (I8).
* `profile_handler` -- `profile_asset`, one bounded task per asset.

Importing this package registers both handlers with `steward_queue`, the same
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
from steward_catalog.masking import RawCell, column_semantic_type, mask, mask_optional
from steward_catalog.models import (
    WORKSPACE_ID,
    AssetRecord,
    ColumnRecord,
    DiscoveredAsset,
    DiscoveredColumn,
    ProfileRecord,
    ProfileTarget,
    SchemaFilter,
    SourceKey,
    SourceRecord,
)
from steward_catalog.profile_handler import (
    PROFILE_ASSET_SAMPLE_PAYLOAD,
    PROFILE_ASSET_TASK_TYPE,
    ProfileAssetPayload,
    build_profile_asset,
)
from steward_catalog.profiler import (
    SourceProfiler,
    SourceProfilerFactory,
    postgres_profiler,
)
from steward_catalog.profiles import (
    PROFILE_ENTITY,
    RecordedProfile,
    latest_profile,
    profile_digest,
    record_profile,
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
    "PROFILE_ASSET_SAMPLE_PAYLOAD",
    "PROFILE_ASSET_TASK_TYPE",
    "PROFILE_ENTITY",
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
    "ProfileAssetPayload",
    "ProfileRecord",
    "ProfileTarget",
    "RawCell",
    "RecordedProfile",
    "SchemaFilter",
    "ScanSourcePayload",
    "Secret",
    "SecretNotFound",
    "SecretResolver",
    "SourceInspector",
    "SourceInspectorFactory",
    "SourceProfiler",
    "SourceProfilerFactory",
    "SourceKey",
    "SourceRecord",
    "apply_plan",
    "build_profile_asset",
    "build_scan_source",
    "column_semantic_type",
    "decode_cursor",
    "encode_cursor",
    "get_asset",
    "get_source",
    "latest_profile",
    "list_asset_columns",
    "list_assets",
    "load_state",
    "mask",
    "mask_optional",
    "open_source_connection",
    "plan_convergence",
    "postgres_inspector",
    "postgres_profiler",
    "profile_digest",
    "record_profile",
    "register_source",
]
