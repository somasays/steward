"""Profile persistence: append-only versions, and the digest that ends them.

The same two rules `repository` holds to -- the caller owns the transaction, and
a mutation and its audit row are one write (I7) -- with one property of its own:

**A profile is written only when it differs from the latest stored one.** The
digest of the computed `TableProfile` is compared with the digest on the latest
version; equal means no INSERT, no audit row, no version. That is the
convergence property #20 gave a rescan (I8), reached the same way -- by deciding
*before* writing rather than by upserting and hoping nothing moved -- and it is
what keeps an append-only table from growing a row per scheduled profile of a
table nobody has touched since March.

The digest is `steward_queue.digest`, the same canonicalisation the queue uses
for dedup keys, because two ways of hashing a payload are two things that drift.

What the audit row carries is the *version and the digest*, never the profile
itself: the profile row is the record, and copying masked samples into the
ledger would double the number of places a value lives for no gain (I7, N7).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from steward_queue import Actor, QueueConnection, digest, write_audit
from steward_schemas import TableProfile

from steward_catalog import _sql
from steward_catalog.models import WORKSPACE_ID, ProfileRecord

__all__ = ["PROFILE_ENTITY", "RecordedProfile", "latest_profile", "profile_digest", "record_profile"]

PROFILE_ENTITY = "profile"
"""The `audit_log.entity_type` a profile version is recorded under."""

FIRST_VERSION = 1


@dataclass(frozen=True, slots=True)
class RecordedProfile:
    """What `record_profile` did: which version stands, and whether it wrote it.

    `changed=False` is the convergent case and is the interesting one -- it is
    the assertion a caller (and `test_profile_convergence`) makes about
    re-profiling unchanged data.
    """

    version: int
    digest: str
    changed: bool


def profile_digest(profile: TableProfile) -> str:
    """The stable digest a profile is compared by.

    Taken over the JSON rendering rather than the model, because JSON is what is
    stored: a digest of something the database does not hold could agree while
    the rows disagreed.
    """
    return digest(profile.model_dump(mode="json"))


def _profile_record(row: Sequence[Any]) -> ProfileRecord:
    return ProfileRecord(
        id=row[0],
        workspace_id=row[1],
        asset_id=row[2],
        version=row[3],
        digest=row[4],
        profile=TableProfile.model_validate(row[5]),
        created_at=row[6],
    )


def latest_profile(conn: QueueConnection, asset_id: UUID) -> ProfileRecord | None:
    """The highest-versioned profile of `asset_id`, or None if never profiled."""
    row = conn.execute(_sql.SELECT_LATEST_PROFILE, {"asset_id": asset_id}).fetchone()
    return _profile_record(row) if row is not None else None


def record_profile(
    conn: QueueConnection, asset_id: UUID, profile: TableProfile, *, actor: Actor
) -> RecordedProfile:
    """Append `profile` as the next version of `asset_id`'s -- unless it is the
    one already stored, in which case nothing is written at all.

    Runs in the caller's transaction and does not commit, so the profile row,
    its audit row and the task's terminal state settle together (I7, I8).
    """
    latest = latest_profile(conn, asset_id)
    computed = profile_digest(profile)
    if latest is not None and latest.digest == computed:
        return RecordedProfile(version=latest.version, digest=computed, changed=False)

    version = FIRST_VERSION if latest is None else latest.version + 1
    profile_id = uuid4()
    conn.execute(
        _sql.INSERT_PROFILE,
        {
            "id": profile_id,
            "workspace_id": WORKSPACE_ID,
            "asset_id": asset_id,
            "version": version,
            "digest": computed,
            "profile": Jsonb(profile.model_dump(mode="json")),
        },
    )
    write_audit(
        conn,
        actor=actor,
        action="profile.recorded",
        entity_type=PROFILE_ENTITY,
        entity_id=str(profile_id),
        before=None if latest is None else {"version": latest.version, "digest": latest.digest},
        after={
            "asset_id": str(asset_id),
            "version": version,
            "digest": computed,
            "columns": len(profile.columns),
            "row_count": profile.row_count,
        },
    )
    return RecordedProfile(version=version, digest=computed, changed=True)
