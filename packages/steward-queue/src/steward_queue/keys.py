"""Canonical keys derived from a payload.

Two places need "the same request must hash the same way": task dedup within a
run, and the admission lock that keeps a goal from being started twice
concurrently for the same parameters. They agree because they share this
module -- two canonicalisations of the same dict are two things that can drift.

`default=str` renders whatever JSON cannot (a `UUID`, a `datetime`) rather than
raising, so a payload that is valid as a `jsonb` column is always hashable here.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

__all__ = ["canonical_json", "digest"]


def canonical_json(value: Mapping[str, Any]) -> str:
    """`value` as JSON with sorted keys and no incidental whitespace."""
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Mapping[str, Any]) -> str:
    """A stable SHA-256 hex digest of `value`."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
