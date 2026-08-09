"""What a profile says about a table — and the only shape a sampled value may
take once it leaves the reader that read it (I6, issue #49).

`MaskedSample` is the load-bearing model here. Every field of a profile that
carries a *value* rather than a *count* is typed as one: `min_value`,
`max_value`, and every entry of `top_values`. There is deliberately no field
anywhere in this module that a `str` read out of a customer database can be
assigned to, so the compiler is what stops a raw payload from being persisted,
returned or put in front of a model -- not a reviewer noticing (GUARDRAILS.md
G2). The masking itself lives in `steward_catalog.masking`, which owns the raw
side of the boundary; this package owns the far side, because it is the one
package everything may import (I4) and the far side has to be reachable from
the API, the catalog and -- when #50 lands -- the prompt builders.

Counts and ratios are not masked and do not need to be: a null ratio is a
statistic about a column, not a value out of it. Sizes are the one thing a mask
does disclose (`MaskedSample.length`), stated as a field rather than smuggled
through the mask's own shape, so what a profile gives away is legible.

These are projections, not rows: `profiles` stores a `TableProfile` as JSONB
with the version and digest around it (SPEC.md §7), so what a profile *is* can
grow without a migration, and what a profile *row* is stays the catalog's.
"""

from decimal import Decimal
from enum import StrEnum

from steward_schemas._base import SchemaModel


class SemanticType(StrEnum):
    """What a value looks like, inferred from its format alone.

    Inference is deterministic and local: a regex over one value's text
    rendering, never a model (#49 ships no LLM) and never a column's name --
    a column called `email` holding integers is a data-quality finding, and a
    profile that inferred `EMAIL` from the name would hide it.

    Most members describe a single value. `MIXED` and `UNKNOWN` are
    column-level only: a column whose sampled values disagree is `MIXED`, and
    one with nothing to look at (all null, or empty) is `UNKNOWN`. They are in
    the same enum rather than a second one because a consumer asks one question
    -- "what is in this column" -- and two enums would make "no answer" a
    different kind of thing from "text".
    """

    EMAIL = "email"
    PHONE = "phone"
    CREDIT_CARD = "credit_card"
    UUID = "uuid"
    IP_ADDRESS = "ip_address"
    URL = "url"
    NUMBER = "number"
    TIMESTAMP = "timestamp"
    BOOLEAN = "boolean"
    EMPTY = "empty"
    TEXT = "text"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class MaskedSample(SchemaModel):
    """One sampled value, after masking. The only form a sample is published in.

    `masked` is format-preserving (`j***@g***.***`, `****-****-****-****`), so
    a downstream consumer can still see *shape* -- which is what classification
    (#50) and documentation (#51) work from (SPEC.md §4, rule 2) -- without the
    payload.

    `length` is the original rendering's character count, kept because "how long
    are the values in this column" is a profiling question and the mask does not
    answer it. It is the one thing this model discloses beyond shape, and saying
    so in a field is better than a mask that leaks it by being length-preserving
    while claiming not to.

    **It is `None` when a length would be the value.** In a column with very few
    distinct values, the size is decisive rather than descriptive: `(BOOLEAN, 4)`
    and `(BOOLEAN, 5)` name `true` and `false` exactly, and `yes`/`no` or
    `male`/`female` do the same in any column two values wide. An
    `is_hiv_positive` column would have published every sampled value and its
    distribution into an append-only table (#49 review). `steward_catalog`
    decides -- at the column level, where the cardinality is known -- and a
    consumer that treats `length` as always-present is the bug this optionality
    exists to surface.
    """

    masked: str
    semantic_type: SemanticType
    length: int | None = None


class ValueFrequency(SchemaModel):
    """A masked value and how often it occurred in the profiled table."""

    value: MaskedSample
    count: int


class ColumnProfile(SchemaModel):
    """One column's statistics and its masked sample.

    Ratios are computed against the table's row count and carried as `Decimal`
    rather than `float` for the reason every other money-or-ratio field in this
    package is: two runs over unchanged data must produce byte-identical JSON,
    and binary floating point does not promise that across platforms.

    **`min_value`/`max_value` are extrema in the column's own type, or absent.**
    They are computed with the source's `min`/`max` over the column and only
    then rendered as text, so a column of 2, 10, 100 reports 2 and 100 -- not
    the extrema of the *renderings*, which are `10` and `2` (issue #70). A type
    with no `min`/`max` aggregate at all -- `json`, `uuid`, `bytea`, `point` --
    reports `None` for both. A consumer may therefore read these as facts about
    the column, and `None` means "this type has no order, or the column has no
    rows", never "here is a lexical stand-in".

    `top_values` doubles as the column's sample. Profiling reads the most
    frequent values rather than an arbitrary page of rows because frequency is
    the statistic worth having, and because "the k most common values, ties
    broken by value" is deterministic where `LIMIT k` without an `ORDER BY` is
    not (I8).
    """

    name: str
    data_type: str
    null_count: int
    null_ratio: Decimal
    distinct_count: int
    distinct_ratio: Decimal
    min_value: MaskedSample | None = None
    max_value: MaskedSample | None = None
    top_values: tuple[ValueFrequency, ...] = ()
    semantic_type: SemanticType = SemanticType.UNKNOWN


class TableProfile(SchemaModel):
    """A profile of one asset: its row count and one entry per active column.

    Deliberately identity-free -- no asset id, no version, no timestamp. Those
    belong to the `profiles` row that carries this as JSONB, and keeping them
    out is what lets the profile be compared by value: re-profiling unchanged
    data produces an equal `TableProfile`, which is how convergence is decided
    (I8, the same shape `plan_convergence` gives a scan).
    """

    row_count: int
    columns: tuple[ColumnProfile, ...] = ()
