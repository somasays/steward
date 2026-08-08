"""The masking layer, value by value (I6).

These are unit tests over a pure function: no database, no source, no task.
The end-to-end claim -- that a planted secret never leaves the masker by any
route -- is H7's, in `test_masking_canary.py`.
"""

from __future__ import annotations

import pytest
from steward_catalog.masking import RawCell, column_semantic_type, mask, mask_optional
from steward_schemas import MaskedSample, SemanticType

# (raw value, inferred type, mask) -- the table is the specification.
CASES: tuple[tuple[str, SemanticType, str], ...] = (
    ("john.doe@gmail.com", SemanticType.EMAIL, "j***@g***.com"),
    ("ada@mail.example.co.uk", SemanticType.EMAIL, "a***@m***.uk"),
    ("4111111111111111", SemanticType.CREDIT_CARD, "4***-****-****-1111"),
    ("4111-1111-1111-1111", SemanticType.CREDIT_CARD, "4***-****-****-1111"),
    ("378282246310005", SemanticType.CREDIT_CARD, "3***-****-***0-005"),
    ("+1 (555) 010-0199", SemanticType.PHONE, "+* (***) ***-**99"),
    ("f47ac10b-58cc-4372-a567-0e02b2c3d479", SemanticType.UUID, "********-****-****-****-************"),
    ("192.168.1.42", SemanticType.IP_ADDRESS, "***.***.*.**"),
    ("2026-08-08 11:30:00", SemanticType.TIMESTAMP, "****-**-** **:**:**"),
    ("2026-08-08", SemanticType.TIMESTAMP, "****-**-**"),
    ("https://example.com/orders", SemanticType.URL, "https://e******.***/******"),
    ("-1234.50", SemanticType.NUMBER, "-1***.*0"),
    ("true", SemanticType.BOOLEAN, "t**e"),
    ("", SemanticType.EMPTY, ""),
    ("shipped", SemanticType.TEXT, "s*****d"),
    ("x", SemanticType.TEXT, "x"),
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
    """The property the table above is only evidence for: whatever the format,
    a mask of a value longer than two characters is not that value."""
    if len(raw) > 2:
        assert raw not in mask(RawCell(raw)).masked


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


def test_a_sixteen_digit_surrogate_key_is_not_a_credit_card() -> None:
    """The Luhn check is what keeps warehouse ids out of the card bucket -- a
    false positive here would drive a false classification in #50."""
    assert mask(RawCell("1234567890123456")).semantic_type is SemanticType.NUMBER


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
