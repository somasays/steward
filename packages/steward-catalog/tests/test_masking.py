"""The masking layer, value by value (I6).

These are unit tests over a pure function: no database, no source, no task.
The end-to-end claim -- that a planted secret never leaves the masker by any
route -- is H7's, in `test_masking_canary.py`.
"""

from __future__ import annotations

import itertools

import pytest
from steward_catalog.masking import (
    MIN_MASKED_ALNUM,
    RawCell,
    _required_concealment,
    column_semantic_type,
    mask,
    mask_optional,
)
from steward_schemas import MaskedSample, SemanticType

# (raw value, inferred type, mask) -- the table is the specification.
CASES: tuple[tuple[str, SemanticType, str], ...] = (
    ("john.doe@gmail.com", SemanticType.EMAIL, "j***@g***.***"),
    ("ada@mail.example.co.uk", SemanticType.EMAIL, "***@m***.**"),
    ("a@b.co", SemanticType.EMAIL, "***@***.**"),
    ("4111111111111111", SemanticType.CREDIT_CARD, "****-****-****-****"),
    ("4111-1111-1111-1111", SemanticType.CREDIT_CARD, "****-****-****-****"),
    ("378282246310005", SemanticType.CREDIT_CARD, "****-****-****-***"),
    # An IMEI is Luhn-valid by specification, so every one classifies as a card.
    # Nothing is revealed, which is the point: the classification is a guess the
    # value makes about itself, and a reveal riding on it published the last four
    # digits of every device identifier in a column.
    ("490154203237518", SemanticType.CREDIT_CARD, "****-****-****-***"),
    ("+1 (555) 010-0199", SemanticType.PHONE, "+* (***) ***-****"),
    # `_PHONE` is a superset of every 9-11 digit identifier, so a suffix reveal
    # here would publish the last two digits of an SSN (`***-**-**89`).
    ("123-45-6789", SemanticType.PHONE, "***-**-****"),
    ("f47ac10b-58cc-4372-a567-0e02b2c3d479", SemanticType.UUID, "********-****-****-****-************"),
    ("192.168.1.42", SemanticType.IP_ADDRESS, "***.***.*.**"),
    ("2026-08-08 11:30:00", SemanticType.TIMESTAMP, "****-**-** **:**:**"),
    ("2026-08-08", SemanticType.TIMESTAMP, "****-**-**"),
    ("https://example.com/orders", SemanticType.URL, "https://e******.***/******"),
    ("-1234.50", SemanticType.NUMBER, "-1***.*0"),
    # Below the floor: nothing is revealed, because the first and last
    # character of a short value *are* the value (`MIN_MASKED_ALNUM`).
    ("true", SemanticType.BOOLEAN, "****"),
    ("", SemanticType.EMPTY, ""),
    ("shipped", SemanticType.TEXT, "s*****d"),
    ("x", SemanticType.TEXT, "*"),
    ("M", SemanticType.TEXT, "*"),
    ("O+", SemanticType.TEXT, "*+"),
    ("42", SemanticType.NUMBER, "**"),
    ("9.5", SemanticType.NUMBER, "*.*"),
    ("7", SemanticType.NUMBER, "*"),
)

# Values whose first and last character give the whole thing away. Each one was
# published verbatim before the floor landed (the architecture guardian's
# finding on #49): a `gender`, `blood_type` or single-digit-score column would
# have had its entire value domain written into an append-only profile row.
SHORT_VALUES: tuple[str, ...] = (
    "M",
    "F",
    "Y",
    "N",
    "O+",
    "A-",
    "42",
    "9.5",
    "7",
    "no",
    "ok",
    "t",
    "US",
    "a@b.co",
)


@pytest.mark.parametrize(
    ("raw", "semantic_type", "masked"), CASES, ids=[case[0] or "empty" for case in CASES]
)
def test_a_value_is_inferred_and_masked(raw: str, semantic_type: SemanticType, masked: str) -> None:
    sample = mask(RawCell(raw))

    assert sample.semantic_type is semantic_type
    assert sample.masked == masked
    assert sample.length == len(raw)


@pytest.mark.parametrize(
    ("raw", "semantic_type", "masked"), CASES, ids=[case[0] or "empty" for case in CASES]
)
def test_no_mask_contains_the_value_it_masked(raw: str, semantic_type: SemanticType, masked: str) -> None:
    """The property the table above is only evidence for, and it holds at every
    length -- an empty string being the one value there is nothing to hide in."""
    if raw:
        assert raw not in mask(RawCell(raw)).masked


def concealed(raw: str) -> int:
    """How many of `raw`'s alphanumerics its mask hides.

    Measured on the value, never on the mask. Counting `MASK_CHAR`s in the
    output is the mistake that let the email leak score as compliant:
    `a@b.co` -> `a***@b***.co` has six asterisks and conceals two characters,
    so a floor of three passed while the whole TLD was published.
    """
    alnums = sum(1 for char in raw if char.isalnum())
    return alnums - sum(1 for char in mask(RawCell(raw)).masked if char.isalnum())


def alnum_total(raw: str) -> int:
    return sum(1 for char in raw if char.isalnum())


@pytest.mark.parametrize("raw", SHORT_VALUES)
def test_a_short_value_is_masked_rather_than_republished(raw: str) -> None:
    """I6 does not have a lower length bound, and neither does the masker.

    Asserted as properties rather than a table of expected strings: the value
    must not survive in its own mask, and `MIN_MASKED_ALNUM` of its
    alphanumerics must be concealed -- "the mask differs from the value" is too
    weak a bar, since `4*` differs from `42` and gives it away.
    """
    assert raw not in mask(RawCell(raw)).masked
    assert concealed(raw) >= _required_concealment(alnum_total(raw))


# Values whose sensitive content sits *after the last dot* -- the region
# `_mask_email` used to interpolate verbatim on the theory that a TLD is a
# public taxonomy. Each is a string a notes, reference or identifier column
# produces by accident: no whitespace, one `@`, at least one dot.
TLD_TAIL_VALUES: tuple[str, ...] = (
    "case@2019.DIAGNOSIS-HIV-POSITIVE",
    "id@sys.EMP-00417-TERMINATED",
    "ref@2024.SETTLEMENT-CONFIDENTIAL",
    "a@b.co",
    "1@2.museum",
    "x@y.z",
)


@pytest.mark.parametrize("raw", TLD_TAIL_VALUES)
def test_nothing_after_the_last_dot_is_published_verbatim(raw: str) -> None:
    """The leak the first version shipped: a value that merely *looks* like an
    address had everything past its final dot copied into an append-only
    profile row."""
    masked = mask(RawCell(raw)).masked
    tail = raw.rpartition(".")[2]

    assert tail not in masked
    assert concealed(raw) >= _required_concealment(alnum_total(raw))


ALPHABET = "abZ40+-._@?:/ "
"""The exhaustive sweep's alphabet: letters, digits, and every delimiter a mask
is allowed to preserve -- including `?` and `:` and `/`, which are what the
URL and delimiter-only branches turn on. Fourteen characters over lengths 1-3
is 2,954 values.

`*` is excluded deliberately and the exclusion is load-bearing: a string of
asterisks is a fixed point of `mask()` (`"*"` masks to `"*"`), so it would be
reported as surviving its own mask. It carries no information to conceal --
there is nothing behind an asterisk -- but the sweep cannot tell those apart,
so the alphabet leaves it out rather than the assertion carving an exception.
"""

# Templates, because the flat sweep above structurally cannot reach the branches
# that actually broke: an email needs 5 characters minimum, a URL 5, a card 13,
# so a sweep over lengths 1-3 exercises `_shape` and `EMPTY` and nothing else --
# and *both* post-floor leaks on this branch (the verbatim TLD, the verbatim
# scheme) lived in branches it could not construct. Each template puts a
# payload in the segment that branch interpolates.
FORMAT_TEMPLATES: tuple[str, ...] = (
    "user@host.{payload}",  # the TLD slot
    "{payload}@host.com",  # the local slot
    "user@{payload}.com",  # the domain slot
    "{payload}://host/path",  # the scheme slot
    "https://{payload}/path",  # the authority slot
    "https://host/{payload}",  # the path slot
    "+1-555-{payload}",  # the phone tail
    "{payload}",  # no format at all
)

# The card branch needs its own sweep: a payload spliced into
# `4111-1111-1111-{payload}` stops the value being digits-only and Luhn-valid,
# so it never enters `_mask_card` at all -- the slot looked covered and was
# inert, which is how the last-four reveal survived four review rounds. These
# are real Luhn-valid identifiers of card length, including an IMEI (Luhn-valid
# by specification) and a surrogate key that passes by luck.
LUHN_VALID_IDENTIFIERS: tuple[str, ...] = (
    "4111111111111111",
    "4111-1111-1111-1111",
    "378282246310005",
    "490154203237518",
    "1234567890123452",
    "4539578763621486",
)


@pytest.mark.parametrize("raw", LUHN_VALID_IDENTIFIERS)
def test_a_card_shaped_value_publishes_no_digit_of_itself(raw: str) -> None:
    """The card branch reveals nothing, so a false positive costs nothing.

    Asserted over the digits rather than the string: the mask regroups in fours,
    so a substring check would miss a digit that survived into a different
    group.
    """
    masked = mask(RawCell(raw)).masked

    assert not any(char.isdigit() for char in masked), f"{raw!r} -> {masked!r}"
    assert set(masked) <= {"*", "-"}
    assert concealed(raw) == alnum_total(raw)


# Payloads a real column holds and nobody would want republished. Two
# characters minimum: a one-character payload landing in the last slot of a
# value is indistinguishable from the end-character reveal every mask is allowed
# (`4111-1111-1111-Z` -> `4***-****-****-Z`), which is bounded by the floor
# rather than by this assertion. Asserting on it would be asserting that the
# allowance does not exist.
PAYLOADS: tuple[str, ...] = (
    "DIAGNOSIS-HIV-POSITIVE",
    "EMP-00417-TERMINATED",
    "SETTLEMENT-CONFIDENTIAL",
    "hunter2",
    "0199",
    "ab",
)

EXHAUSTIVE_VALUES = len(ALPHABET) + len(ALPHABET) ** 2 + len(ALPHABET) ** 3


def test_no_short_value_survives_its_own_mask() -> None:
    """The floor, asserted exhaustively rather than by example.

    Every string of up to three characters over `ALPHABET`. Each must be absent
    from its own mask and must have its alphanumerics concealed to the floor.
    Written this way because every defect in this module so far was found in
    the region a hand-written table does not reach: `M`, `42`, `9.5`, values
    made of nothing but delimiters, and `s://a`.
    """
    survivors = []
    for length in (1, 2, 3):
        for combination in itertools.product(ALPHABET, repeat=length):
            value = "".join(combination)
            masked = mask(RawCell(value)).masked
            if value in masked or concealed(value) < _required_concealment(alnum_total(value)):
                survivors.append((value, masked))

    assert survivors == []
    assert EXHAUSTIVE_VALUES == 2954  # the count the docstring claims


@pytest.mark.parametrize("template", FORMAT_TEMPLATES)
@pytest.mark.parametrize("payload", PAYLOADS)
def test_no_format_publishes_the_payload_in_the_segment_it_interpolates(template: str, payload: str) -> None:
    """The sweep the flat one cannot do: one payload per interpolated segment.

    Both leaks this branch shipped were a segment published verbatim because of
    where it sat -- the TLD, then the URL scheme. Neither was reachable from
    strings of three characters. This drives a payload through every slot each
    branch treats specially and asserts it does not come back out.
    """
    raw = template.format(payload=payload)
    masked = mask(RawCell(raw)).masked

    assert payload not in masked, f"{raw!r} -> {masked!r}"
    assert concealed(raw) >= _required_concealment(alnum_total(raw))


def test_a_long_value_collapses_instead_of_shaping_character_by_character() -> None:
    raw = "a very long free-text comment that nobody wants as asterisks"
    sample = mask(RawCell(raw))

    assert sample.masked == "a***s"
    assert sample.length == len(raw)


def test_a_recognised_format_keeps_its_shape_however_long_it_is() -> None:
    """A UUID is 36 characters -- longer than the collapse threshold -- and
    collapsing it would throw away the one thing worth publishing about it."""
    assert mask(RawCell("f47ac10b-58cc-4372-a567-0e02b2c3d479")).masked == (
        "********-****-****-****-************"
    )


def test_the_luhn_check_reduces_card_false_positives_but_cannot_remove_them() -> None:
    """What the Luhn filter does and does not buy, stated honestly.

    It keeps *most* warehouse ids out of the card bucket -- but a checksum is a
    property the value computes about itself, not membership in a closed set, so
    roughly one in ten long numeric ids passes it, and an IMEI passes by
    specification. The earlier version of this test picked a Luhn-*invalid*
    number and concluded warehouse ids were safe, which proved only that this
    one was. The real defence is that the card branch reveals nothing.
    """
    assert mask(RawCell("1234567890123456")).semantic_type is SemanticType.NUMBER  # Luhn-invalid
    passes_luhn = mask(RawCell("1234567890123452"))
    assert passes_luhn.semantic_type is SemanticType.CREDIT_CARD  # a false positive we cannot avoid
    assert passes_luhn.masked == "****-****-****-****"  # ...and it costs nothing, because nothing is revealed


def test_masking_is_deterministic() -> None:
    # I8: a profile is compared by value across runs, so the mask may not vary.
    assert mask(RawCell("john.doe@gmail.com")) == mask(RawCell("john.doe@gmail.com"))


def test_a_raw_cell_redacts_itself_wherever_it_is_printed() -> None:
    """The path types cannot cover: an f-string, a `%s` log, a traceback."""
    cell = RawCell("hunter2@example.com")

    assert "hunter2" not in f"{cell}"
    assert "hunter2" not in repr(cell)
    assert "hunter2" not in "{}".format(cell)  # noqa: UP032 -- the shape a log call takes
    assert "hunter2" not in str([cell])


def test_a_null_is_not_a_sample() -> None:
    assert mask_optional(None) is None
    assert mask_optional(RawCell("x")) == mask(RawCell("x"))


def sample(semantic_type: SemanticType) -> MaskedSample:
    return MaskedSample(masked="*", semantic_type=semantic_type, length=1)


def test_a_columns_type_is_what_its_values_agreed_on() -> None:
    assert (
        column_semantic_type([sample(SemanticType.EMAIL), sample(SemanticType.EMAIL)]) is SemanticType.EMAIL
    )


def test_a_column_with_nothing_to_look_at_is_unknown() -> None:
    assert column_semantic_type([]) is SemanticType.UNKNOWN
    assert column_semantic_type([sample(SemanticType.EMPTY)]) is SemanticType.UNKNOWN


def test_a_column_whose_values_disagree_is_mixed() -> None:
    assert column_semantic_type([sample(SemanticType.EMAIL), sample(SemanticType.NUMBER)]) is (
        SemanticType.MIXED
    )


def test_empty_values_do_not_outvote_the_rest() -> None:
    assert column_semantic_type([sample(SemanticType.EMPTY), sample(SemanticType.EMAIL)]) is (
        SemanticType.EMAIL
    )


def test_the_floor_is_both_absolute_and_proportional() -> None:
    """`_required_concealment`'s own contract, asserted directly.

    The proportional term is the half of the fix that generalises -- it is what
    catches the *next* branch that publishes a segment because of where it sits,
    the way the URL scheme did (21 of 24 alphanumerics published, an absolute
    floor of three satisfied by the rest). Without this test, reverting the
    function to `min(alnums, MIN_MASKED_ALNUM)` leaves the whole suite green,
    because the scheme allowlist independently covers the one value that
    motivated it. A guard nothing asserts is a guard that gets refactored away.
    """
    # Short values: everything, since a fraction of two characters is nothing.
    assert _required_concealment(0) == 0
    assert _required_concealment(1) == 1
    assert _required_concealment(2) == 2
    # The absolute floor governs while it is the larger of the two.
    assert _required_concealment(3) == 3
    assert _required_concealment(6) == 3
    # Past that, the proportion does -- and rounds up, never down.
    assert _required_concealment(7) == 4
    assert _required_concealment(24) == 12
    assert _required_concealment(101) == 51


def test_the_proportional_term_rejects_a_long_value_revealed_by_its_ends() -> None:
    """The shape the count-only floor let through, as a property of the gate
    rather than of the branch that happened to produce it."""
    alnums = 24
    assert _required_concealment(alnums) > MIN_MASKED_ALNUM
    # 21 of 24 published — what `X-CONFIDENTIAL-CASE-2019://abc` used to do.
    assert alnums - 21 < _required_concealment(alnums)
