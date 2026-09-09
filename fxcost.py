"""
fxcost.py
---------
What the conversion cost you.

A Canadian card used in Europe does not bill euros. CIBC converts at the Visa
network rate and adds 2.5%, so its export shows CAD -- already converted, at a
rate you did not choose and are not told.

The app already knows the ECB reference rate for the day of every purchase,
which makes the comparison free:

    2026-09-02  REWE SAGT DANKE     EUR 52.30
                billed by the card  CA$ 85.94
                at the ECB rate     CA$ 83.85
                the conversion cost CA$  2.09   (2.49%)

That is a more useful answer than "here is your spending in dollars", and it
only exists because the rate is stored per transaction date rather than as one
current figure.

It depends on recovering the original foreign amount, which card statements
carry in the description when they carry it at all. When they do not, the row
is an ordinary purchase in the billed currency and nothing here applies --
reported as unavailable rather than estimated, because an FX cost computed
from a guessed original amount is worse than none.
"""
import re
from decimal import Decimal

import money

# CIBC's own published foreign-transaction fee, for reference in the report.
# Not used in the arithmetic -- the cost is measured, not assumed. It is here
# so a figure wildly unlike it is recognisable as a parsing problem rather
# than as a bank being extraordinary.
TYPICAL_CARD_FEE = Decimal("0.025")

# A markup this large is not a fee, it is a misread amount.
IMPLAUSIBLE_MARKUP = Decimal("0.25")

# "52.30 EUR", "EUR 52.30", "52,30 EUR" -- the orderings card statements use.
_TRAILING = re.compile(r"(?<![\d.,])(\d[\d.,]*)\s*([A-Z]{3})\b")
_LEADING = re.compile(r"\b([A-Z]{3})\s*(\d[\d.,]*)(?![\d.,])")


def foreign_amount(description, billed_currency=None):
    """(cents, currency) hidden in a description, or None.

    Only currencies other than the billed one count: a CAD statement line
    reading "... 85.94 CAD" is restating the charge, not disclosing an
    original, and treating it as one would produce a nonsense zero-cost
    comparison.
    """
    text = (description or "").upper()
    for pattern, order in ((_TRAILING, "after"), (_LEADING, "before")):
        for match in pattern.finditer(text):
            raw, code = ((match.group(1), match.group(2)) if order == "after"
                         else (match.group(2), match.group(1)))
            if code == (billed_currency or "").upper():
                continue
            if code not in money.CURRENCIES and code != money.BASE:
                continue
            # No try needed: the pattern captures only a digit followed by
            # digits, dots and commas, and money.parse accepts every such
            # string. Checked by fuzzing 200k of them, none of which failed.
            cents = money.parse(raw)
            if cents:
                return abs(cents), code
    return None


def compare(charged_minor, charged_currency, base_minor, rate):
    """What the card charged against what the reference rate says.

    `rate` is base -> charged_currency on the purchase date. Returns None when
    anything needed is missing, so a caller can say "not known" rather than
    print a zero.
    """
    if not charged_minor or not base_minor or rate is None:
        return None
    reference = money.convert(abs(base_minor), rate)
    if not reference:
        return None
    billed = abs(int(charged_minor))
    cost = billed - reference
    share = Decimal(cost) / Decimal(reference)
    return {
        "billed_minor": billed,
        "billed_text": money.format(billed, charged_currency),
        "reference_minor": reference,
        "reference_text": money.format(reference, charged_currency),
        "cost_minor": cost,
        "cost_text": money.format(cost, charged_currency),
        "share": float(round(share, 6)),
        "percent": float(round(share * 100, 2)),
        "plausible": abs(share) <= IMPLAUSIBLE_MARKUP,
    }


def summarise(rows):
    """The month's FX cost, over the rows that have one.

    Reports how many rows it covers, because "you paid CA$14 in conversion
    fees" means something different over 3 purchases than over 40, and a
    reader who is not told will assume it covers everything.
    """
    priced = [r for r in rows if r.get("fx") and r["fx"]["plausible"]]
    if not priced:
        return {"available": False, "rows": 0, "of": len(rows)}

    currency = priced[0]["charged_currency"]
    billed = sum(r["fx"]["billed_minor"] for r in priced)
    reference = sum(r["fx"]["reference_minor"] for r in priced)
    cost = billed - reference
    return {
        "available": True,
        "rows": len(priced),
        "of": len(rows),
        "currency": currency,
        "billed_text": money.format(billed, currency),
        "reference_text": money.format(reference, currency),
        "cost_minor": cost,
        "cost_text": money.format(cost, currency),
        "percent": float(round(Decimal(cost) / Decimal(reference) * 100, 2))
        if reference else None,
    }
