"""Pagination cursors round-trip, and anything else is a typed rejection."""

from __future__ import annotations

from uuid import UUID

import pytest
from steward_catalog import InvalidCursor, decode_cursor, encode_cursor

ASSET_ID = UUID("33333333-3333-3333-3333-333333333333")


@pytest.mark.parametrize(
    ("schema_name", "name"),
    [("public", "orders"), ("sales", "customer-facing view"), ("Ünïcode", "tāble")],
    ids=["plain", "spaces-and-dashes", "non-ascii"],
)
def test_a_cursor_round_trips(schema_name: str, name: str) -> None:
    assert decode_cursor(encode_cursor(schema_name, name, ASSET_ID)) == (schema_name, name, ASSET_ID)


def test_a_cursor_is_url_safe_and_unpadded() -> None:
    # It travels in a query string; `+`, `/` and `=` all need escaping there.
    cursor = encode_cursor("sales", "recent_orders", ASSET_ID)

    assert set(cursor) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


@pytest.mark.parametrize(
    "cursor",
    ["", "not-base64!!", "cHVibGlj", encode_cursor("public", "orders", ASSET_ID)[:-4]],
    ids=["empty", "not-base64", "too-few-fields", "truncated"],
)
def test_anything_else_is_an_invalid_cursor(cursor: str) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(cursor)
