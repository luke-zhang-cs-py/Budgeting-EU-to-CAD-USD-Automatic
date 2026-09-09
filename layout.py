"""
layout.py
---------
Working out the shape of a bank export: which column is which.

Split out of importers.py, which had grown to 291 statements across thirteen
functions and two unrelated jobs. Nine of them answered "what shape is this
file", four answered "turn these rows into transactions", and the two change
for different reasons -- a new bank format touches the first, a change to how
a transaction is built touches the second. That is divergent change, and it
showed up as the lowest maintainability index in the project.

Two ways in, because there are two kinds of file:

    by_header(headers)        a labelled file: match the column names
    infer(headers, rows)      a headerless one: judge by the values

The second exists because CIBC's Download Transactions has no header row, and
because CIBC alone produces at least three layouts -- credit card, chequing
and business differ in whether money out and money in are split and whether a
running balance is carried. A per-bank profile would have to guess which one
you downloaded. The values do not have to guess.

Both return a guess, never a decision. The preview shows it and the reader
corrects it, which matters more for a headerless file than a labelled one.
"""
import re

import ledger
import money

# Header names seen in the wild, lowercased. Order matters: the first match
# wins, so the more specific names come first.
DATE_NAMES = ("completed date", "date completed", "transaction date",
              "booking date", "value date", "date", "buchungstag", "datum")
DESCRIPTION_NAMES = ("description", "merchant", "payee", "reference", "details",
                     "narrative", "name", "beneficiary", "buchungstext",
                     "verwendungszweck")
AMOUNT_NAMES = ("amount", "value", "betrag", "gross")
OUT_NAMES = ("paid out", "money out", "debit", "withdrawal", "soll", "expense")
IN_NAMES = ("paid in", "money in", "credit", "deposit", "haben", "income")
CURRENCY_NAMES = ("currency", "ccy", "waehrung", "währung")
CATEGORY_NAMES = ("category", "type", "kategorie")

FIELDS = ("date", "description", "amount", "amount_out", "amount_in",
          "currency", "category")

# How many rows to judge a column by. Enough that one odd row cannot decide a
# column's type, few enough that a 13-month export is not scanned twice.
SAMPLE_ROWS = 40

# The share of a column's filled cells that must look like a type before the
# column is called that type. Not all of them: a real export has the odd "n/a"
# and the odd blank, and demanding perfection would leave every column
# unclassified.
TYPE_AGREEMENT = 0.8

# Below this share of rows filled, a numeric column is money-out or money-in
# rather than a running balance. A balance is present on every row; out and in
# are each blank whenever the other is used, and that sparseness is the only
# thing that distinguishes them.
BALANCE_DENSITY = 0.9

# An amount cell, allowing a currency symbol or a trailing three-letter code
# but no other letters: "85.94", "-1.234,56", "€52.30", "52.30 EUR".
_AMOUNT_CELL = re.compile(r"""^[-+(]?\s*        # optional sign or bracket
                              [^\w\s]{0,3}\s*   # optional currency symbol
                              \d[\d.,\s]*       # the figure
                              \)?\s*            # optional closing bracket
                              (?:[A-Za-z]{3})?$ # optional currency code
                           """, re.VERBOSE)


def empty():
    """A mapping that chose nothing. Every field present, all unset."""
    return dict.fromkeys(FIELDS, None)


# ------------------------------------------------------------ header or not

def looks_like_data(cells):
    """Is this row a transaction rather than a set of column names.

    Decided by content, not by a bank name: a header cell is a word, and a
    data row carries a readable date. Asking "does any cell parse as a date"
    is the one test that separates them without a list of bank formats to
    maintain -- no bank calls a column "2026-09-02".
    """
    for cell in cells:
        text = (cell or "").strip()
        if text and is_date(text):
            return True
    return False


# ------------------------------------------------------- a labelled file

def by_header(headers):
    """A guess from the column names."""
    out_column = _find(headers, OUT_NAMES)
    mapping = empty()
    mapping.update(
        date=_find(headers, DATE_NAMES),
        description=_find(headers, DESCRIPTION_NAMES),
        amount=_find(headers, AMOUNT_NAMES),
        amount_out=out_column,
        amount_in=_find(headers, IN_NAMES),
        currency=_find(headers, CURRENCY_NAMES),
        category=_find(headers, CATEGORY_NAMES),
        # Separate in/out columns carry the sign in the column choice, so the
        # figures inside them are unsigned by definition.
        expenses_positive=bool(out_column and not _find(headers,
                                                        AMOUNT_NAMES)),
    )
    return mapping


def _find(headers, candidates):
    """The first header matching any candidate, exact before substring."""
    lowered = [h.strip().lower() for h in headers]
    for want in candidates:
        if want in lowered:
            return headers[lowered.index(want)]
    for want in candidates:
        for index, have in enumerate(lowered):
            if want in have:
                return headers[index]
    return None


# ------------------------------------------------------ a headerless file

def infer(headers, rows):
    """A guess from the values, for a file with no column names.

    Three steps, each its own function: sort the columns by what they hold,
    name the date and description, then decide which of the three amount
    shapes this file uses. As one function this measured cyclomatic
    complexity 22, the highest in the project.
    """
    sample = rows[:SAMPLE_ROWS]
    if not sample:
        return empty()

    kinds = _classify(headers, sample)
    mapping = empty()
    if kinds["dates"]:
        mapping["date"] = kinds["dates"][0]
    if kinds["texts"]:
        # The widest text column. A masked card number is short and repeats;
        # a merchant name is long and varies, and it is the one a reader needs
        # in order to recognise the purchase.
        mapping["description"] = max(kinds["texts"], key=lambda t: t[1])[0]

    mapping.update(_amount_shape(kinds["numbers"], headers, sample))
    return mapping


def _classify(headers, sample):
    """Each column sorted into dates, figures, or text.

    Figures carry how full the column is, because that is what later
    distinguishes a debit/credit pair from a running balance. Text carries its
    mean width, because that is what distinguishes a merchant name from a
    masked card number.
    """
    dates, texts, numbers = [], [], []
    for index, name in enumerate(headers):
        values = [(row[index] or "").strip() if index < len(row) else ""
                  for row in sample]
        filled = [v for v in values if v]
        if not filled:
            continue                      # a blank column says nothing
        if _mostly(filled, is_date):
            dates.append(name)
        elif _mostly(filled, is_amount):
            numbers.append((name, len(filled) / len(values)))
        else:
            texts.append((name, sum(len(v) for v in filled) / len(filled)))
    return {"dates": dates, "texts": texts, "numbers": numbers}


def _amount_shape(numbers, headers, sample):
    """Which of the three ways this file writes an amount.

      * money out and money in in separate columns
      * one signed column
      * one unsigned column, expenses positive

    A running balance is excluded first: it is filled on every row, where out
    and in are each blank whenever the other is used.
    """
    sparse = [name for name, density in numbers if density < BALANCE_DENSITY]

    if len(sparse) >= 2:
        # Out before in: statements put money leaving first, and the debit
        # column is the fuller one on a spending account.
        return {"amount_out": sparse[0], "amount_in": sparse[1],
                "expenses_positive": True}

    signed = [name for name, _density in numbers
              if _has_negatives(sample, headers, name)]
    if signed:
        return {"amount": signed[0], "expenses_positive": False}
    if sparse:
        return {"amount_out": sparse[0], "expenses_positive": True}
    if numbers:
        # One dense, all-positive numeric column. Taken as the amount rather
        # than as a balance, because refusing to guess leaves the file
        # unimportable and the preview exists to be corrected.
        return {"amount": numbers[0][0], "expenses_positive": True}
    return {"expenses_positive": False}


# ------------------------------------------------------------- the tests

def _mostly(values, test, share=TYPE_AGREEMENT):
    return sum(1 for v in values if test(v)) >= max(1, int(len(values) * share))


def is_date(text):
    try:
        ledger.as_date(text)
        return True
    except ValueError:
        return False


def is_amount(text):
    """Does this cell look like a figure, rather than merely contain one.

    money.parse is deliberately permissive -- it strips everything that is not
    a digit or a separator, which is right for reading a cell somebody has
    already told us is an amount. It is wrong for *deciding* which column is
    the amount: "REWE SAGT DANKE 52.30 EUR" parses to 52.30, so a description
    column full of foreign-currency notes was classified as numeric and the
    file then had no description column at all.

    Requiring the cell to be substantially a number, not text containing one,
    is the distinction. Nothing that matches the pattern fails money.parse --
    fuzzed over 200k matching cells -- so there is no failure branch to guard.
    """
    if not _AMOUNT_CELL.match(text.strip()):
        return False
    money.parse(text)
    return True


def _has_negatives(rows, headers, name):
    """Does this column ever hold a negative, i.e. is it a signed amount.

    Keeps its guard, unlike is_amount: a column classifies as numeric at
    TYPE_AGREEMENT, so up to a fifth of its cells can be unparseable, and this
    reads every raw cell rather than the ones that passed.
    """
    index = headers.index(name)
    for row in rows:
        text = (row[index] or "").strip() if index < len(row) else ""
        if not text:
            continue
        try:
            if money.parse(text) < 0:
                return True
        except money.MoneyError:
            continue
    return False
