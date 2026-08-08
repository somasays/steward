"""The masking layer: the only path from a sampled value to anything else (I6).

Two types and one function make that structural rather than conventional, and
they are deliberately the same shape as `secrets.Secret`, which does the same
job for credentials:

* `RawCell` -- one value as a source returned it. It is **not** a `str`: it
  cannot be assigned to a `str` field, put in a `MaskedSample`, formatted into
  a log message as text, or handed to anything typed for data, because none of
  those accept it and `mypy --strict` says so (GUARDRAILS.md G2). Like `Secret`
  it redacts itself in `repr`/`str`, so even the accidental `%s` -- the one path
  types cannot cover -- prints `RawCell(***)`.
* `MaskedSample` (`steward_schemas.profile`) -- the far side. Every value-
  carrying field of a profile is typed as one, so a raw value has nowhere to go
  even if a future caller obtains one.
* `mask()` -- the only bridge. It is the one function that reads a `RawCell`'s
  characters, exactly as `Secret.reveal()` is the one way to a credential's.

What this buys, precisely: a raw sampled value cannot be **persisted, returned,
or passed to a prompt builder**, because every one of those seams demands a
`MaskedSample` and a `RawCell` does not satisfy it. What it does not buy is
protection against code that deliberately reaches for the private attribute, or
against a library writing to a file descriptor -- which is why H7 exists and
runs canaries end to end (GUARDRAILS.md §1, Tier H).

**Masks are format-preserving, not value-preserving, and there is a floor.** A
mask reveals a character or two so a human reviewer can read a profile, which is
the whole value on a short one -- so below `MIN_MASKED_ALNUM` hidden characters
a segment reveals none. The property the rest of the system may rely on is
therefore "at least three characters of every non-empty value are unknown",
not merely "the mask is not the value". Classification (#50) and
documentation (#51) work from shape, name and statistics -- SPEC.md §4's second
design rule -- so the mask keeps delimiters and character classes and discards
the payload. Uniformly: there is no exemption for numbers, booleans or dates.
An account number is a number, a date of birth is a date, and an exemption is
a hole the moment a customer's data disagrees with our intuition about which
columns are sensitive. The cost is real and stated in SPEC.md §13 D10: a
profile's `min_value`/`max_value` no longer support range reasoning, so M4's
range rules will need a policy-gated unmasked path rather than this one.

Inference is deterministic: a regex over the value's own text, never the column
name (a column called `email` full of integers is a finding, and inferring from
the name would hide it) and never a model -- this slice calls none.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from steward_schemas import MaskedSample, SemanticType

__all__ = [
    "MASK_RUN",
    "RawCell",
    "column_semantic_type",
    "infer_semantic_type",
    "mask",
    "mask_optional",
]

MASK_RUN = "***"
"""What an elided run of characters looks like in a collapsed mask."""

MASK_CHAR = "*"

COLLAPSE_ABOVE = 32
"""Length past which a mask stops preserving shape character by character.

A 4 KB comment masked one character at a time is 4 KB of asterisks in a profile
row, and its shape says nothing anyway. Above this a value collapses to
`f***t`. The threshold is a readability choice, not a privacy one -- both forms
disclose the same thing, and `MaskedSample.length` carries the size explicitly.
"""

REDACTED = "RawCell(***)"

_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\S+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_IPV4 = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
_IPV6 = re.compile(r"^[0-9a-fA-F:]{3,45}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?)?$")
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_PHONE = re.compile(r"^\+?[\d][\d\s().-]{5,20}$")
_DIGITS = re.compile(r"\D")

BOOLEAN_VALUES = frozenset({"true", "false", "t", "f"})
"""Postgres renders `boolean` as `true`/`false`; `t`/`f` covers a source (or a
future connector) that renders the short form."""

CARD_DIGITS = range(13, 20)
"""Digit counts a payment card can have. Combined with a Luhn check below --
without it every 16-digit surrogate key in a warehouse would be a credit card,
which is a false positive that would then drive a classification (#50)."""

CARD_GROUP = 4
CARD_REVEALED_SUFFIX = 4
PHONE_REVEALED_SUFFIX = 2

MIN_MASKED_ALNUM = 3
"""How many alphanumerics must remain hidden for a mask to reveal any.

The floor that keeps "format-preserving" from collapsing into "value-preserving"
on short values. A mask reveals its first and last character to stay legible;
on `M`, `42` or `9.5` that is the whole value, so below this floor a segment
reveals nothing at all. Three rather than one because the guarantee worth
stating is not "the mask differs from the value" -- `4*` differs from `42` and
tells you everything -- but "at least three characters of it are unknown".

A payment card is exempt by arithmetic rather than by choice: `CARD_DIGITS`
starts at 13 and five digits are revealed, so eight always remain.
"""


@dataclass(frozen=True, slots=True, repr=False)
class RawCell:
    """One value as the source returned it, rendered as text.

    Not a `str` on purpose, and for the same reason `Secret` is not one:
    subclassing `str` would make it substitutable everywhere a `str` goes,
    which is every logging call, every f-string and every JSON dump -- exactly
    the paths I6 is about. This type is opaque, prints as `RawCell(***)`, and
    `mask()` is the deliberate way out.

    A `None` from the database is not a `RawCell` at all: nulls are counted,
    never sampled, so there is nothing to mask (`mask_optional`).
    """

    _text: str

    def __repr__(self) -> str:
        return REDACTED

    def __str__(self) -> str:
        return REDACTED


def _luhn(digits: str) -> bool:
    """The check digit test payment cards satisfy and surrogate keys do not."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _is_card(text: str) -> bool:
    if not re.fullmatch(r"[\d\s-]+", text):
        return False
    digits = _DIGITS.sub("", text)
    return len(digits) in CARD_DIGITS and _luhn(digits)


def infer_semantic_type(text: str) -> SemanticType:
    """What `text` looks like. Ordered most specific first, deliberately.

    A credit card is checked before a phone number and both before a plain
    number, because each of those patterns is a superset of the next: reversing
    the order would classify every card as a number and lose the finding that
    matters most.
    """
    if not text:
        return SemanticType.EMPTY
    if _EMAIL.match(text):
        return SemanticType.EMAIL
    if _URL.match(text):
        return SemanticType.URL
    if _UUID.match(text):
        return SemanticType.UUID
    if _IPV4.match(text) or (":" in text and _IPV6.match(text)):
        return SemanticType.IP_ADDRESS
    if _TIMESTAMP.match(text):
        return SemanticType.TIMESTAMP
    if _is_card(text):
        return SemanticType.CREDIT_CARD
    if _NUMBER.match(text):
        return SemanticType.NUMBER
    if _PHONE.match(text):
        return SemanticType.PHONE
    if text.lower() in BOOLEAN_VALUES:
        return SemanticType.BOOLEAN
    return SemanticType.TEXT


def _shape(text: str, *, keep_first: bool, keep_last: int) -> str:
    """`text` with every alphanumeric replaced by `*`, delimiters preserved.

    `keep_first`/`keep_last` say how many of the alphanumerics survive at each
    end. They exist because "the local part starts with j" is what makes a
    masked profile legible to a human reviewer, and revealing one character of
    a value is the smallest disclosure that does it. Types that gain nothing
    from it -- a UUID, an IP, a timestamp -- reveal none.

    **The floor comes first.** A request to keep characters is honoured only
    when `MIN_MASKED_ALNUM` of them would still be hidden afterwards; otherwise
    nothing is revealed. Without it, "keep the first and the last" is the
    identity function on any value with two alphanumerics or fewer -- `M`, `42`,
    `O+`, `9.5` -- so a `gender`, `blood_type` or single-digit-score column
    would have published its entire value domain verbatim, into an append-only
    table, while satisfying every type in the system (a `MaskedSample` was
    constructed; its payload merely equalled the input). Caught by the
    architecture guardian on #49 before merge.
    """
    positions = [index for index, char in enumerate(text) if char.isalnum()]
    if not positions:
        # Delimiters are normally passed through because they are the *shape*
        # around a value. A value that is nothing but delimiters -- `-`, `??`,
        # a `+` standing in for "unknown" -- has no shape to preserve and would
        # otherwise be published verbatim, so here they are the value and are
        # masked like one.
        return MASK_CHAR * len(text)
    wanted = (1 if keep_first else 0) + keep_last
    if len(positions) - wanted < MIN_MASKED_ALNUM:
        wanted = 0
    revealed = set()
    if keep_first and wanted and positions:
        revealed.add(positions[0])
    if keep_last and wanted:
        revealed.update(positions[-keep_last:])
    return "".join(
        char if not char.isalnum() or index in revealed else MASK_CHAR for index, char in enumerate(text)
    )


def _revealed_prefix(part: str) -> str:
    """A segment's first character, or nothing when the floor forbids it."""
    return part[0] if len(part) - 1 >= MIN_MASKED_ALNUM else ""


def _collapsed(text: str) -> str:
    """A long value's mask: its first and last character with a run between."""
    return f"{text[0]}{MASK_RUN}{text[-1]}"


def _mask_email(text: str) -> str:
    """`john.doe@gmail.com` -> `j***@g***.com` (SPEC.md §4).

    Each side is subject to the same floor `_shape` applies: a local part or a
    domain name short enough that its first character would give it away keeps
    nothing (`a@b.co` -> `***@***.co`). The TLD survives whatever its length --
    it is a public taxonomy, not a payload.
    """
    local, _, domain = text.partition("@")
    name, _, tld = domain.rpartition(".")
    return f"{_revealed_prefix(local)}{MASK_RUN}@{_revealed_prefix(name)}{MASK_RUN}.{tld}"


def _mask_card(text: str) -> str:
    """`4111111111111111` -> `4***-****-****-1234` (SPEC.md §4).

    The first digit (the issuer network) and the last four (what a human uses
    to recognise their own card) survive; everything between is masked and the
    digits are regrouped in fours, so the mask reads as a card whatever
    separators the source used.
    """
    digits = _DIGITS.sub("", text)
    revealed = {0, *range(len(digits) - CARD_REVEALED_SUFFIX, len(digits))}
    masked = "".join(char if index in revealed else MASK_CHAR for index, char in enumerate(digits))
    groups = [masked[start : start + CARD_GROUP] for start in range(0, len(masked), CARD_GROUP)]
    return "-".join(groups)


def _mask_url(text: str) -> str:
    """Scheme kept, everything after it shaped: `https://e******.***/******`."""
    scheme, separator, rest = text.partition("://")
    return f"{scheme}{separator}{_shape(rest, keep_first=True, keep_last=0)}"


def _masked_text(text: str, semantic_type: SemanticType) -> str:
    if semantic_type is SemanticType.EMPTY:
        return ""
    if semantic_type is SemanticType.EMAIL:
        return _mask_email(text)
    if semantic_type is SemanticType.CREDIT_CARD:
        return _mask_card(text)
    if semantic_type is SemanticType.URL:
        return _mask_url(text)
    if semantic_type in (SemanticType.UUID, SemanticType.IP_ADDRESS, SemanticType.TIMESTAMP):
        return _shape(text, keep_first=False, keep_last=0)
    if semantic_type is SemanticType.PHONE:
        return _shape(text, keep_first=False, keep_last=PHONE_REVEALED_SUFFIX)
    # Only the unstructured tail collapses. A recognised format keeps its shape
    # at any length -- a UUID is 36 characters and shaping it is the whole point.
    if len(text) > COLLAPSE_ABOVE:
        return _collapsed(text)
    return _shape(text, keep_first=True, keep_last=1)


def mask(cell: RawCell) -> MaskedSample:
    """The one bridge: a raw value in, a publishable sample out (I6).

    Pure and total -- every string has a mask, so there is no input for which a
    caller has to decide what to do instead, which is how a "just this once"
    unmasked path gets added.
    """
    text = cell._text
    semantic_type = infer_semantic_type(text)
    return MaskedSample(
        masked=_masked_text(text, semantic_type), semantic_type=semantic_type, length=len(text)
    )


def mask_optional(cell: RawCell | None) -> MaskedSample | None:
    """`mask`, for the aggregates that are `NULL` on an empty table."""
    return None if cell is None else mask(cell)


def column_semantic_type(samples: Iterable[MaskedSample]) -> SemanticType:
    """What a column holds, from what its sampled values agreed on.

    Deliberately computed from the *masked* samples: the inference already
    happened one value at a time, so the column-level answer needs no second
    look at the raw data. Empty strings do not vote -- a column of mostly-empty
    text with a few emails in it is an email column with a data-quality problem,
    not a `MIXED` one.
    """
    voted = {sample.semantic_type for sample in samples} - {SemanticType.EMPTY}
    if not voted:
        return SemanticType.UNKNOWN
    if len(voted) > 1:
        return SemanticType.MIXED
    return voted.pop()
