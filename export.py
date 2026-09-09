"""
export.py
---------
The file. One row per transaction, euros alongside CAD and USD.

Two columns here earn their place and are easy to leave out:

  - **rate_date**, the business day whose rate was used. Without it a weekend
    purchase looks like it was converted at a rate that does not exist, and
    there is no way to check the arithmetic.
  - **rate_lag_days**, how far back that was. A lag of 0 is ordinary, 3 means
    a holiday weekend, and anything larger is worth a look.

The euro column is the record and the converted ones are derived, so the euro
column is written first. If the two ever disagree, euros is right.
"""
import csv
import datetime as dt
import io

import budgets
import ledger
import money

COLUMNS = [
    "date", "description", "merchant", "category",
    "amount_eur", "amount_cad", "amount_usd",
    "rate_cad", "rate_usd", "rate_date", "rate_lag_days",
    "source", "note",
]

SUMMARY_COLUMNS = [
    "month", "category", "spent_eur", "cap_eur", "remaining_eur",
    "percent_of_cap", "state",
]


def _plain(cents):
    """A bare number for the file: no symbol, no thousands separators.

    Grouping is for reading on a page. In a CSV, "1,234.56" is two fields to
    anything that splits on commas, which is the one thing a CSV reader
    reliably does.
    """
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    return f"{sign}{whole}.{frac:02d}"


def transactions_csv(connection, month=None, category=None, search=None,
                     directory=None):
    """The transaction file, as a string."""
    rows = ledger.transactions(connection, month=month, category=category,
                               search=search, directory=directory)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS,
                            lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({
            "date": row["spent_on"],
            "description": row["description"],
            "merchant": row["merchant"],
            "category": row["category"],
            "amount_eur": _plain(row["amount_eur"]),
            "amount_cad": _plain(row.get("amount_cad")),
            "amount_usd": _plain(row.get("amount_usd")),
            "rate_cad": row.get("rate_cad", ""),
            "rate_usd": row.get("rate_usd", ""),
            "rate_date": row.get("rate_date", ""),
            "rate_lag_days": "" if row.get("rate_lag_days") is None
            else row["rate_lag_days"],
            "source": row["source"],
            "note": row.get("rate_note", ""),
        })
    return buffer.getvalue()


def summary_csv(connection, month=None, today=None):
    """Budget versus actual, one row per category."""
    month = month or dt.date.today().strftime("%Y-%m")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=SUMMARY_COLUMNS,
                            lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in budgets.status(connection, month, today):
        writer.writerow({
            "month": month,
            "category": row["category"],
            "spent_eur": _plain(row["spent_eur"]),
            "cap_eur": _plain(row["cap_eur"]),
            "remaining_eur": _plain(row["remaining_eur"]),
            "percent_of_cap": "" if row["percent"] is None else row["percent"],
            "state": row["state"],
        })
    return buffer.getvalue()


def filename(kind="transactions", month=None):
    """A name that sorts and says what it holds."""
    stamp = month or dt.date.today().strftime("%Y-%m")
    return f"wallet-{kind}-{stamp}.csv"


def write(connection, path, month=None, directory=None):
    """Write the transaction file to disk. Returns the row count.

    newline="" because csv writes its own line endings; without it Windows
    turns every one into a blank line between rows.
    """
    text = transactions_csv(connection, month=month, directory=directory)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return max(0, len(text.strip().splitlines()) - 1)


def reconciles(connection, month=None, directory=None):
    """Does the file add up to the ledger it came from?

    Cheap to check, and it is the thing a reader most needs to be able to
    trust: the rows in the file sum to the euro figure the app reports. Run by
    the tests, and available from the page.
    """
    text = transactions_csv(connection, month=month, directory=directory)
    rows = list(csv.DictReader(io.StringIO(text)))
    from_file = money.total(money.parse(r["amount_eur"]) for r in rows
                            if r["amount_eur"])
    from_ledger = money.total(
        t["amount_eur"] for t in ledger.transactions(
            connection, month=month, directory=directory))
    return from_file == from_ledger, from_file, from_ledger
