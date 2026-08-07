"""What the catalog API returns: a page of assets, and one asset in full
(SPEC.md §8, issue #20).

Separate from `asset` and `column` because it composes both, and `column`
already depends on `asset` for the shared lifecycle enum -- putting the
composite in either module would make that a cycle.

These are projections, not rows. `AssetDetail` is deliberately "asset + its
columns" rather than a fatter `Asset`: profiles, documents and classifications
attach to the same resource in later milestones, and each arrives as another
field here without touching the `Asset` contract itself (I3, N9).
"""

from steward_schemas._base import SchemaModel
from steward_schemas.asset import Asset
from steward_schemas.column import Column


class AssetPage(SchemaModel):
    """One page of `GET /v1/assets`.

    `next_cursor` is opaque and is `None` on the last page. Cursors rather than
    offsets (SPEC.md §8) because a scan running concurrently with a client's
    pagination would shift offsets underneath it, silently skipping assets --
    the one thing a catalog listing must not do.
    """

    items: tuple[Asset, ...]
    next_cursor: str | None = None


class AssetDetail(SchemaModel):
    """`GET /v1/assets/{id}`: the asset and the columns discovered on it,
    in the source's own ordinal order."""

    asset: Asset
    columns: tuple[Column, ...]
