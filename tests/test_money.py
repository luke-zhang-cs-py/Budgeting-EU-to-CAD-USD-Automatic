"""Amounts: parsing, converting, formatting.

The separator cases are the ones worth having. Reading "1.234,56" as 1.23
is not a rounding error, it is a factor of a thousand, and the result looks
like a perfectly ordinary small purchase -- so nothing downstream flags it
and the budget is simply wrong.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import money  # noqa: E402


# ------------------------------------------------------------------ parsing

@pytest.mark.parametrize("text,cents", [
    # European: dot groups, comma is the decimal point. This is what a German
    # or Dutch bank export looks like.
    ("1.234,56", 123456),
    ("12,34", 1234),
    ("1.234.567,89", 123456789),
    # Anglo: comma groups, dot is the decimal point.
    ("1,234.56", 123456),
    ("12.34", 1234),
    # No separator at all.
    ("50", 5000),
    ("0", 0),
    # Currency symbols and spaces are noise.
    ("€50", 5000),
    ("EUR 12,34", 1234),
    ("1 234,56", 123456),
    # Negatives, including the accounting form.
    ("-8,50", -850),
    ("(12,34)", -1234),
    # A leading decimal separator.
    (".50", 50),
    ("0,05", 5),
])
def test_amounts_are_read_as_cents(text, cents):
    assert money.parse(text) == cents


def test_the_last_separator_decides_the_decimal_point():
    """Both conventions in one assertion, because this is the whole trick.

    A three-digit tail with no other separator is grouping instead: "1,234"
    is a thousand-odd euros, not one euro twenty-three. Getting these two
    rules the wrong way round swaps a purchase between 12.34 and 1234.00.
    """
    assert money.parse("1.234,56") == money.parse("1,234.56") == 123456
    assert money.parse("1,234") == money.parse("1.234") == 123400


def test_an_unreadable_amount_raises_rather_than_becoming_zero():
    """A silent zero is the dangerous failure: the import "succeeds", a
    purchase is missing, and the budget says there is money left."""
    for bad in ("", "   ", None, "abc", "n/a", "--"):
        with pytest.raises(money.MoneyError):
            money.parse(bad)


def test_an_integer_is_taken_as_whole_units():
    assert money.parse(50) == 5000


# --------------------------------------------------------------- converting

def test_conversion_uses_the_given_rate():
    # EUR 100.00 at 1.6033 -> CAD 160.33
    assert money.convert(10000, 1.6033) == 16033
    assert money.convert(10000, "1.1614") == 11614


def test_conversion_rounds_half_up_not_half_even():
    """Python rounds 2.5 to 2 by default. In a column of figures that reads
    as a bug even though it is defensible, so this rounds the way a person
    doing it by hand would."""
    assert money.convert(5, 1.5) == 8        # 7.5 -> 8, not 7
    assert money.convert(7, 1.5) == 11       # 10.5 -> 11, not 10


@pytest.mark.parametrize("cents,rate,correct,what_float_gives", [
    # EUR 50.00 at the real CAD rate is 8016.5 cents exactly, so half-up owes
    # CA$80.17. In binary floating point the product comes out 8016.4999...,
    # and round() then takes it *down* to CA$80.16.
    (5000, "1.6033", 8017, 8016),
    (25000, "1.6033", 40083, 40082),
    # EUR 75.00 at the real USD rate, same story.
    (7500, "1.1614", 8711, 8710),
    (17500, "1.1614", 20325, 20324),
])
def test_conversion_beats_float_rounding_on_exact_halves(cents, rate, correct,
                                                         what_float_gives):
    """Why this uses Decimal, in cases that actually occur.

    These are not contrived: they are round euro amounts at the ECB rates
    published the day this was written. A cent per transaction is exactly the
    size of error that makes a total disagree with its own rows.
    """
    assert money.convert(cents, rate) == correct
    assert round(cents * float(rate)) == what_float_gives
    assert correct != what_float_gives


def test_conversion_is_exact_at_scale():
    # 133333 * 1.6033 = 213772.7989
    assert money.convert(133333, 1.6033) == 213773


def test_a_negative_amount_converts_as_a_negative():
    """Refunds are real transactions and must not flip sign."""
    assert money.convert(-10000, 1.6033) == -16033


def test_a_missing_or_impossible_rate_raises():
    for bad in (None, 0, -1.5):
        with pytest.raises(money.MoneyError):
            money.convert(10000, bad)


def test_totalling_rows_equals_totalling_by_hand():
    """The property the whole integer-cents decision exists to protect: the
    rows in the file add up to the total printed under them."""
    rows = [money.parse("0,10"), money.parse("0,20"), money.parse("12,34")]
    assert money.total(rows) == 1264
    assert money.format(money.total(rows), "EUR") == "€12.64"


# --------------------------------------------------------------- formatting

@pytest.mark.parametrize("cents,currency,shown", [
    (1234, "EUR", "€12.34"),
    (16033, "CAD", "CA$160.33"),
    (11614, "USD", "US$116.14"),
    (-850, "EUR", "-€8.50"),
    (0, "EUR", "€0.00"),
    (5, "EUR", "€0.05"),
    (123456789, "EUR", "€1,234,567.89"),
])
def test_formatting(cents, currency, shown):
    assert money.format(cents, currency) == shown


def test_the_csv_form_has_no_symbol_and_no_grouping_confusion():
    """The file is read by other programs more than by people, so the plain
    form is a bare number a parser will accept."""
    assert money.format(123456, "EUR", symbol=False) == "1,234.56"
    assert money.format(-850, "EUR", symbol=False) == "-8.50"


def test_formatting_never_truncates_the_cents():
    """"€12.3" in a money column means somebody is formatting with %g."""
    for cents in (1230, 1200, 5, 100000):
        assert money.format(cents, "EUR").split(".")[1].__len__() == 2


def test_parse_and_format_round_trip():
    for text in ("1.234,56", "0,05", "-8,50", "12,34"):
        cents = money.parse(text)
        assert money.parse(money.format(cents, "EUR", symbol=False)) == cents
