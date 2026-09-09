"""
money.py
--------
Amounts as integers, in minor units.

Everything in this project stores money as a whole number of cents. Floats are
the wrong type for money and it shows up quickly: 0.1 + 0.2 is 0.30000000000004,
so a budget adds up its own rows and disagrees with its own total. Once a
reader sees that, they stop trusting every other figure on the page.

So: 1234 means EUR 12.34, and conversion rounds once, explicitly, at the point
of conversion.

Nothing here knows about exchange rates or transactions. It parses, converts
and formats.
"""
import re
from decimal import Decimal, ROUND_HALF_UP

# The three currencies this app deals in. EUR is what the wallet exports and
# the other two are what the answer is wanted in.
BASE = "EUR"
TARGETS = ("CAD", "USD")
CURRENCIES = (BASE,) + TARGETS

SYMBOLS = {"EUR": "€", "CAD": "CA$", "USD": "US$"}

# Every currency here happens to have two decimal places. Kept as a named
# constant rather than a literal 100 so the assumption is visible -- JPY has
# none, and if this ever grows to it, this is the thing that has to change.
MINOR_UNITS = 2
_SCALE = 10 ** MINOR_UNITS

_CLEAN = re.compile(r"[^\d,.\-+]")


class MoneyError(ValueError):
    """An amount that could not be read. Never a silent zero.

    A row whose amount is unparseable must stop the import and say which row,
    because the alternative is a ledger that is quietly missing a purchase and
    a budget that says there is money left when there is not.
    """


def parse(text):
    """A written amount as an integer number of cents.

    Handles both decimal conventions, because this is euro data and the
    separators are genuinely ambiguous:

        "1.234,56"  ->  123456    (European: dot groups, comma decides)
        "1,234.56"  ->  123456    (Anglo: comma groups, dot decides)
        "12,34"     ->   1234     (European decimal comma)
        "12.34"     ->   1234     (Anglo decimal point)
        "-8,50"     ->   -850
        "1 234,56"  ->  123456    (thin/normal space grouping)

    The rule is *the last separator present wins as the decimal point*, which
    is what makes "1.234,56" and "1,234.56" both come out right. A German bank
    CSV and a Canadian one can therefore be imported without a flag, which
    matters because getting this wrong is a factor-of-100 error, not a rounding
    one -- and it silently looks plausible.
    """
    if text is None:
        raise MoneyError("no amount given")
    if isinstance(text, int):
        return text * _SCALE
    raw = str(text).strip()
    if not raw:
        raise MoneyError("empty amount")

    # Accounting negatives: (12,34) means -12.34
    negative = raw.startswith("-") or (raw.startswith("(") and raw.endswith(")"))
    cleaned = _CLEAN.sub("", raw).lstrip("+-")

    if not cleaned:
        raise MoneyError(f"no digits in {text!r}")

    last_comma = cleaned.rfind(",")
    last_dot = cleaned.rfind(".")
    cut = max(last_comma, last_dot)

    if cut == -1:
        whole, frac = cleaned, ""
    else:
        separator = cleaned[cut]
        tail = cleaned[cut + 1:]
        # Three digits after the last separator, and no other separator, is
        # grouping rather than a decimal: "1,234" is a thousand, not 1.234.
        # Two is the only unambiguous decimal length for these currencies.
        others = cleaned[:cut].count(",") + cleaned[:cut].count(".")
        if len(tail) == 3 and others == 0 and separator in ",.":
            whole, frac = cleaned.replace(",", "").replace(".", ""), ""
        else:
            whole = cleaned[:cut].replace(",", "").replace(".", "")
            frac = tail

    if not whole.isdigit() and whole != "":
        raise MoneyError(f"cannot read {text!r}")
    if frac and not frac.isdigit():
        raise MoneyError(f"cannot read {text!r}")

    whole = whole or "0"
    frac = (frac + "00")[:MINOR_UNITS] if frac else "00"
    cents = int(whole) * _SCALE + int(frac)
    return -cents if negative else cents


def convert(cents, rate):
    """`cents` in EUR at `rate` EUR->target, as cents in the target currency.

    Decimal with ROUND_HALF_UP, not float and not banker's rounding. Two
    reasons, and both are about the reader rather than the arithmetic: half-up
    is what a person gets doing it by hand, and Python's default half-even
    would round 2.5 cents to 2 and 3.5 to 4, which looks like a bug in a
    column of figures even though it is defensible.

    Rounded once, here. Converting a running total is not the same as totalling
    converted rows, and the file shows the rows, so the rows are what must be
    right.
    """
    if rate is None:
        raise MoneyError("no rate to convert at")
    rate = Decimal(str(rate))
    if rate <= 0:
        raise MoneyError(f"implausible rate {rate}")
    return int((Decimal(cents) * rate).quantize(Decimal("1"),
                                                rounding=ROUND_HALF_UP))


def format(cents, currency=BASE, symbol=True, grouping=True):
    """For display and for the CSV. Always two decimals, never truncated.

    Negative amounts get a leading minus rather than parentheses, because the
    CSV is read by other programs more often than by accountants.

    `grouping=False` drops the thousands separators, which is what a CSV needs:
    "1,234.56" is two fields to anything that splits on commas, and splitting
    on commas is the one thing a CSV reader reliably does. export.py had its
    own copy of this arithmetic with the scale written out as a literal 100 --
    the same knowledge in two places, one of which did not know that
    MINOR_UNITS existed.
    """
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), _SCALE)
    grouped = f"{whole:,}" if grouping else f"{whole}"
    body = f"{grouped}.{frac:0{MINOR_UNITS}d}"
    if not symbol:
        return f"{sign}{body}"
    return f"{sign}{SYMBOLS.get(currency, currency + ' ')}{body}"


def total(amounts):
    """Sum of cents. Exists so no caller is tempted to sum floats."""
    return sum(int(a) for a in amounts if a is not None)
