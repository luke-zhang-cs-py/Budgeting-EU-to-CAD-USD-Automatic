"""
importers.py
------------
Reading a CSV export, whatever shape it arrives in.

Apple Wallet and Google Wallet have no export API -- they are sandboxed on the
device and nothing on a desktop can read them. So the route in is a file: you
export from the bank or card behind the wallet, and this reads it. That is how
every honest tool in this space works, and pretending otherwise would mean
scraping screenshots.

Since the file could come from any bank, nothing here assumes a format. It
sniffs the delimiter, guesses which column is which from the header names, and
shows a preview so the guess can be corrected before anything is written. Two
things are guessed carefully because getting them wrong is silent:

  - **The sign convention.** Some banks write an expense as -45.50 and some as
    45.50 in a column called "Debit". Read the wrong way, every expense
    becomes income and the budget reports a surplus.
  - **The currency.** The whole app assumes euros in, so a row in another
    currency is refused rather than treated as euros. A 200 SEK lunch quietly
    booked as EUR 200 is a twenty-fold error that looks entirely plausible.
"""
import csv
import io

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

# Rows with more than this share unreadable are treated as the wrong mapping
# rather than a bad file, because that is nearly always what it is.
MAX_BAD_SHARE = 0.5

# ...but only once there are enough rows for the share to mean anything. Two
# bad rows out of three is 67% and says nothing about the mapping; it is
# probably two bad rows. Applying the ratio to tiny files made a three-line
# import refuse itself instead of reporting which two lines were wrong.
MIN_ROWS_FOR_MAPPING_CHECK = 5

PREVIEW_ROWS = 8


class ImportProblem(Exception):
    """The file or the mapping cannot be used at all."""


def sniff(text):
    """What this file looks like: delimiter, headers, and a suggested mapping.

    The delimiter matters more than it sounds: a German export is
    semicolon-separated *because* the comma is its decimal point, so guessing
    comma turns every amount into two columns.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", errors="replace")
    text = text.lstrip("﻿")
    sample = "\n".join(text.splitlines()[:20])
    if not sample.strip():
        raise ImportProblem("the file is empty")

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        # Sniffer fails on single-column files and some quoting. Fall back to
        # whichever candidate appears most on the header line.
        header_line = sample.splitlines()[0]
        delimiter = max(";,\t|", key=header_line.count)
        if header_line.count(delimiter) == 0:
            delimiter = ","

    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        raise ImportProblem("the file has no rows")

    headers = [cell.strip() for cell in rows[0]]
    return {
        "delimiter": delimiter,
        "headers": headers,
        "rows": rows[1:],
        "mapping": guess_mapping(headers),
        "sample": rows[1:PREVIEW_ROWS + 1],
    }


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


def guess_mapping(headers):
    """A best guess at which column is which.

    Returned rather than applied, so the preview can show what it would do.
    A guess that silently mis-imports 400 rows is worse than no guess.
    """
    out_column = _find(headers, OUT_NAMES)
    in_column = _find(headers, IN_NAMES)
    mapping = {
        "date": _find(headers, DATE_NAMES),
        "description": _find(headers, DESCRIPTION_NAMES),
        "amount": _find(headers, AMOUNT_NAMES),
        "amount_out": out_column,
        "amount_in": in_column,
        "currency": _find(headers, CURRENCY_NAMES),
        "category": _find(headers, CATEGORY_NAMES),
        # Separate in/out columns carry the sign in the column choice, so the
        # figures inside them are unsigned by definition.
        "expenses_positive": bool(out_column and not _find(headers,
                                                           AMOUNT_NAMES)),
    }
    return mapping


def _amount_of(row, mapping):
    """Signed cents for one row: negative spent, positive received.

    Three shapes are handled because all three are common:
      - one signed Amount column
      - separate Paid out / Paid in columns
      - one unsigned column plus expenses_positive
    """
    out_name = mapping.get("amount_out")
    in_name = mapping.get("amount_in")

    if out_name or in_name:
        spent = (row.get(out_name) or "").strip() if out_name else ""
        received = (row.get(in_name) or "").strip() if in_name else ""
        if spent and received:
            # Both filled is usually a zero in one of them.
            try:
                if money.parse(spent) == 0:
                    spent = ""
                elif money.parse(received) == 0:
                    received = ""
            except money.MoneyError:
                pass
        if spent:
            return -abs(money.parse(spent))
        if received:
            return abs(money.parse(received))
        raise money.MoneyError("no amount in either column")

    name = mapping.get("amount")
    if not name:
        raise ImportProblem("no amount column chosen")
    cents = money.parse(row.get(name))
    if mapping.get("expenses_positive"):
        return -cents
    return cents


def preview(sniffed, mapping=None, rows=PREVIEW_ROWS):
    """What the import would do, without doing it.

    Reports per-row problems rather than stopping at the first, because the
    useful message is "column looks wrong" and one row cannot show that.
    """
    mapping = mapping or sniffed["mapping"]
    headers = sniffed["headers"]
    if not mapping.get("date"):
        raise ImportProblem("no date column chosen")
    if not mapping.get("description"):
        raise ImportProblem("no description column chosen")

    parsed, problems = [], []
    for number, raw in enumerate(sniffed["rows"], start=2):
        row = dict(zip(headers, [cell.strip() for cell in raw]))
        try:
            entry = _row(row, mapping, number)
        except (money.MoneyError, ValueError, ImportProblem) as bad:
            problems.append({"row": number, "why": str(bad),
                             "raw": raw[:6]})
            continue
        parsed.append(entry)

    total = len(parsed) + len(problems)
    if total >= MIN_ROWS_FOR_MAPPING_CHECK and \
            len(problems) / total > MAX_BAD_SHARE:
        raise ImportProblem(
            f"{len(problems)} of {total} rows could not be read -- the column "
            f"mapping is probably wrong. First problem: "
            f"{problems[0]['why']} (row {problems[0]['row']})")

    return {
        "mapping": mapping,
        "rows": parsed,
        "problems": problems,
        "readable": len(parsed),
        "unreadable": len(problems),
        "shown": parsed[:rows],
        "spending_total": money.total(e["amount_eur"] for e in parsed
                                      if e["amount_eur"] < 0),
    }


def _row(row, mapping, number):
    """One parsed transaction, or an exception saying why not."""
    import ledger

    when = (row.get(mapping["date"]) or "").strip()
    if not when:
        raise ValueError("no date")
    on = ledger._as_date(when)

    what = (row.get(mapping["description"]) or "").strip()
    if not what:
        raise ValueError("no description")

    currency_column = mapping.get("currency")
    if currency_column:
        found = (row.get(currency_column) or "").strip().upper()
        if found and found != money.BASE:
            # Refused, not converted. This app takes euros in; a 200 SEK lunch
            # booked as EUR 200 is a twenty-fold error that looks plausible.
            raise ValueError(f"not in {money.BASE} (row says {found})")

    amount = _amount_of(row, mapping)
    category = None
    if mapping.get("category"):
        category = (row.get(mapping["category"]) or "").strip() or None

    return {
        "row": number,
        "spent_on": on.isoformat(),
        "description": what,
        "amount_eur": amount,
        "amount_text": money.format(amount, "EUR"),
        "category": category,
    }


def load(connection, previewed, source="import", use_file_categories=False):
    """Write the previewed rows. Returns counts.

    Duplicates are counted, not raised: importing a statement that overlaps
    one already loaded is the expected way to use this, and the useful answer
    is "40 added, 12 already known".

    The file's own categories are off by default. A bank's idea of "Shopping"
    rarely matches yours, and rules you wrote are a better guide than a
    category somebody else's algorithm assigned.
    """
    import ledger

    added = duplicate = 0
    failed = []
    for entry in previewed["rows"]:
        category = entry["category"] if use_file_categories else None
        try:
            _id, how = ledger.add(connection, entry["spent_on"],
                                  entry["description"], entry["amount_eur"],
                                  category=category, source=source)
        except (ValueError, money.MoneyError) as bad:
            failed.append({"row": entry["row"], "why": str(bad)})
            continue
        if how == "added":
            added += 1
        else:
            duplicate += 1

    return {
        "added": added,
        "duplicate": duplicate,
        "failed": failed,
        "unreadable": previewed["unreadable"],
        "problems": previewed["problems"],
    }
