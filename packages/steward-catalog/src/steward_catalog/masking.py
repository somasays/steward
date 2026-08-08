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
the whole value on a short one. The property the rest of the system may rely on
is **`_required_concealment`, and that function is the only statement of it** --
prose is a second copy that drifts, which this one twice did (once false in four
documents, once stale in three). In words, for orientation only: a mask conceals
the greater of an absolute floor and half of a value's alphanumerics, and all of
them when the value is shorter than the floor.

Stated in alphanumerics because that is what is enforced and what is
measurable: delimiters are preserved as shape, and `length` is published
outright, so neither is concealed and a guarantee phrased over "characters"
would be false the moment anyone checked. `_conceals_enough` is the check,
`mask()` applies it to every branch's output, and `test_masking.py` asserts both
that function's contract and the masks that must satisfy it. Classification
(#50) and
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

KNOWN_SCHEMES = frozenset(
    {
        "http",
        "https",
        "ftp",
        "ftps",
        "sftp",
        "ssh",
        "file",
        "s3",
        "gs",
        "abfss",
        "hdfs",
        "ws",
        "wss",
        "git",
        "postgres",
        "postgresql",
        "mysql",
        "mongodb",
        "redis",
        "kafka",
        "jdbc",
    }
)
"""URL schemes a mask may publish verbatim -- a closed taxonomy, checked.

The point is the *checking*. `_URL` matches anything with a letter, some
punctuation and `://`, so "the part before the separator is a scheme" is a
guess about position, and an identifier column that happens to contain `://`
had its identifier published whole. A member of this set carries no customer
data by construction; a non-member is shaped like any other segment. Adding a
scheme here is a deliberate act, which is the property the previous version
lacked.

Schemes without `://` are deliberately absent. `_URL` requires the separator, so
`mailto:` and `data:` never reach this branch -- listing them would imply a
safety this module does not provide for them, and a `data:` URI carries its
payload inline. They are classified and masked as ordinary text.
"""

MIN_MASKED_ALNUM = 3
"""How many of a value's alphanumerics a mask must conceal.

The floor that keeps "format-preserving" from collapsing into "value-preserving".
A mask reveals a character or two to stay legible, and on `M`, `42` or `9.5`
that is the whole value. Three rather than one because the guarantee worth
stating is not "the mask differs from the value" -- `4*` differs from `42` and
tells you everything -- but "three of its alphanumerics are concealed".

**It is enforced at the exit of `mask()`, not inside each format's branch, and
that is the whole point.** Every per-format mask below is best-effort
legibility; `_conceals_enough` is the property. The first version enforced it
branch by branch and three branches did not get it: `_mask_email` interpolated
the TLD verbatim, so a notes column of `case@2019.DIAGNOSIS-HIV-POSITIVE`
published the diagnosis into an append-only table; `_mask_url` revealed a
scheme with no floor (`s://a` -> `s://*`); `_collapsed` had none at all. A
per-branch floor is a rule every future format has to remember. This is a gate
every format goes through, so a branch added in #50 or a new connector inherits
it without knowing it exists.
"""

MIN_CONCEALED_NUMERATOR = 1
MIN_CONCEALED_DENOMINATOR = 2
"""The proportional half of the floor: at least half of a value's alphanumerics.

A count alone is the wrong shape for a long value, and the gap was real rather
than theoretical: `X-CONFIDENTIAL-CASE-2019://abc` published 21 of its 24
alphanumerics and cleared an absolute floor of three, because the three
characters after the separator were enough to satisfy it. So the requirement is
`max(MIN_MASKED_ALNUM, half)` -- the count protects short values, where a
fraction is nothing, and the fraction protects long ones, where a count is.

An integer ratio rather than a float because this decides what gets published,
and a rounding difference across platforms would make the guarantee itself
platform-dependent. Half is where every mask in this module already sat -- the
narrowest margin is a six-alphanumeric number concealing four -- so it costs
nothing today and bounds what a branch added later may reveal.
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


def _alnum_count(text: str) -> int:
    return sum(1 for char in text if char.isalnum())


def _required_concealment(alnums: int) -> int:
    """How many of a value's alphanumerics its mask must hide.

    Two terms, because one of them alone is the wrong shape:

    * an **absolute floor** (`MIN_MASKED_ALNUM`), which is what protects `M`,
      `42`, a PIN or a CVV -- a fraction of a two-character value is nothing;
    * a **proportion** (`MIN_CONCEALED_NUMERATOR`/`_DENOMINATOR`), which is what
      protects a long
      one. A count alone made "a branch cannot make a mask less safe" false in
      the only direction that matters: `X-CONFIDENTIAL-CASE-2019://abc`
      published 21 of its 24 alphanumerics and cleared a floor of three.

    Capped at the value's own length, since "conceal more than there is" is not
    a requirement a mask can meet.
    """
    proportional = -(-alnums * MIN_CONCEALED_NUMERATOR // MIN_CONCEALED_DENOMINATOR)
    return min(alnums, max(MIN_MASKED_ALNUM, proportional))


def _conceals_enough(text: str, masked: str) -> bool:
    """Does `masked` hide enough of `text` to be published?

    Concealment is measured on the *value*, not on the mask: how many of the
    original's alphanumerics are missing from the mask. Counting asterisks
    instead would measure the mask's shape -- `a@b.co` -> `a***@b***.co` has six
    of them and conceals two characters, which is how the email leak scored as
    compliant while publishing a TLD verbatim.

    A mask only ever copies characters from its input or replaces them with
    `MASK_CHAR`, so the difference of the two counts is the number concealed.
    """
    original = _alnum_count(text)
    return original - _alnum_count(masked) >= _required_concealment(original)


def _revealed_prefix(part: str) -> str:
    """A segment's first character, if revealing it is safe *and* worth it.

    Alphanumeric only: the first character of `-internal-code` is a hyphen,
    which says nothing about the value and would spend the segment's one
    allowance on punctuation.
    """
    if not part or not part[0].isalnum():
        return ""
    return part[0] if _alnum_count(part) - 1 >= MIN_MASKED_ALNUM else ""


def _collapsed(text: str) -> str:
    """A long value's mask: its first and last character with a run between."""
    return f"{text[0]}{MASK_RUN}{text[-1]}"


def _mask_email(text: str) -> str:
    """`john.doe@gmail.com` -> `j***@g***.***` (SPEC.md §4, amended by #49).

    **The TLD is masked like everything else, and the earlier version's
    exemption for it was a leak.** "A TLD is a public taxonomy, not a payload"
    is true of `com` and false of whatever sits after the last dot in a string
    that merely *looks* like an address: `_EMAIL` asks for no whitespace, one
    `@` and a dot, which a reference or notes column satisfies by accident.
    `case@2019.DIAGNOSIS-HIV-POSITIVE` published the diagnosis verbatim into
    `profiles` -- append-only, so permanently, and then into whatever #50 builds
    from stored profiles. The canary could not see it because the canary's tail
    is `.test`.

    D10's own argument applies unchanged: an exemption is a hole the moment a
    customer's data disagrees with our intuition about which columns are
    sensitive. So the segment that used to be interpolated is now shaped like
    any other, and what a reader loses is `.com` -- a detail `semantic_type`
    already carries in a form that cannot smuggle a payload.
    """
    local, _, domain = text.partition("@")
    name, _, tld = domain.rpartition(".")
    shaped_tld = _shape(tld, keep_first=False, keep_last=0)
    return f"{_revealed_prefix(local)}{MASK_RUN}@{_revealed_prefix(name)}{MASK_RUN}.{shaped_tld}"


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
    """`https://example.com/orders` -> `https://e******.***/******`.

    **The scheme survives only if it is a scheme we recognise**, and that
    membership is *checked* rather than inferred from position. `_URL` asks for
    a letter, some of `[a-zA-Z0-9+.-]` and `://` -- which an identifier column
    satisfies by accident, and the earlier version then published it whole:
    `X-CONFIDENTIAL-CASE-2019://abc` came out untouched, because the exit gate
    was satisfied by concealing the three characters *after* the separator.

    That is the TLD leak in a second costume, and it is the same lesson: a
    segment is safe to publish when it belongs to a known, closed taxonomy, not
    when it sits where such a segment usually sits. So `KNOWN_SCHEMES` is a
    list, anything outside it is shaped, and the gate in `mask()` is the floor
    underneath rather than the argument.
    """
    scheme, separator, rest = text.partition("://")
    shown = scheme if scheme.lower() in KNOWN_SCHEMES else _shape(scheme, keep_first=False, keep_last=0)
    return f"{shown}{separator}{_shape(rest, keep_first=True, keep_last=0)}"


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
        # No suffix reveal. It used to keep the last two digits, on the reasoning
        # that a phone number's tail is how a human recognises their own -- but
        # `_PHONE` is a superset of every 9-to-11 digit identifier, so
        # `123-45-6789` (an SSN) came out as `***-**-**89`. That is a reveal
        # justified by a value *looking* like a phone number, which is the
        # exemption D10 rules out; the floor was met, so nothing caught it.
        return _shape(text, keep_first=False, keep_last=0)
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

    The last two lines are the guarantee. Whatever a format's own mask produced,
    it is published only if it conceals enough of the value
    (`_conceals_enough`); otherwise everything alphanumeric goes. So the
    property holds for every branch that exists and every branch anyone adds --
    a format can make a mask *more* legible, never less safe.
    """
    text = cell._text
    semantic_type = infer_semantic_type(text)
    masked = _masked_text(text, semantic_type)
    if not _conceals_enough(text, masked):
        masked = _shape(text, keep_first=False, keep_last=0)
    return MaskedSample(masked=masked, semantic_type=semantic_type, length=len(text))


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
