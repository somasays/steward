"""Opaque cursors for the assets listing (SPEC.md §8: cursor pagination).

A cursor is the keyset the listing orders by -- `(schema, name, id)` -- encoded
so clients treat it as a token rather than as a query they can edit. The
encoding is base64url over a unit-separated string: reversible, URL-safe, and
short. It is deliberately not a signed token: a cursor names a position in a
public listing, not an authorization.

Decoding is total in the sense that matters: anything that is not a cursor this
module produced raises `InvalidCursor`, which the API turns into a 400 rather
than into a 500 or -- worse -- a silently different page.
"""

from __future__ import annotations

import base64
import binascii
from uuid import UUID

__all__ = ["InvalidCursor", "decode_cursor", "encode_cursor"]

SEPARATOR = "\x1f"
"""ASCII unit separator: cannot occur in a Postgres identifier, so it cannot
collide with a schema or relation name."""


class InvalidCursor(ValueError):
    """A pagination cursor was not produced by `encode_cursor`."""

    def __init__(self, cursor: str) -> None:
        super().__init__(f"not a valid pagination cursor: {cursor!r}")
        self.cursor = cursor


def encode_cursor(schema_name: str, name: str, asset_id: UUID) -> str:
    """The cursor that resumes a listing immediately after this asset."""
    raw = SEPARATOR.join((schema_name, name, str(asset_id)))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str, UUID]:
    """The keyset a cursor encodes, or `InvalidCursor`."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        schema_name, name, asset_id = raw.split(SEPARATOR)
        return schema_name, name, UUID(asset_id)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursor(cursor) from exc
