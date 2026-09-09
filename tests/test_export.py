"""The output file: shape, and whether it adds up."""
import csv
import datetime as dt
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budgets   # noqa: E402
import db        # noqa: E402
import export    # noqa: E402
import fxrates   # noqa: E402
import ledger    # noqa: E402
import money     # noqa: E402


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    fxrates.reset()
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("date,CAD,USD\n"
                     "2026-02-06,1.6100,1.1700\n"
                     "2026-02-02,1.6033,1.1614\n")
    connection = db.connect(str(tmp_path))
    yield connection
    connection.close()
    fxrates.reset()


def rows_of(text):
    return list(csv.DictReader(io.StringIO(text)))


# ------------------------------------------------------------------- shape

def test_the_file_has_the_columns_a_reader_needs(conn):
    ledger.add(conn, "2026-02-02", "TESCO", -5000, category="Groceries")
    rows = rows_of(export.transactions_csv(conn))
    assert list(rows[0]) == export.COLUMNS
    assert rows[0]["date"] == "2026-02-02"
    assert rows[0]["category"] == "Groceries"
    assert rows[0]["amount_eur"] == "-50.00"
    assert rows[0]["amount_cad"] == "-80.17"
    assert rows[0]["amount_usd"] == "-58.07"


def test_amounts_have_no_thousands_separators(conn):
    """"1,234.56" is two fields to anything that splits on commas, which is
    the one thing a CSV reader reliably does."""
    ledger.add(conn, "2026-02-02", "RENT", -123456)
    row = rows_of(export.transactions_csv(conn))[0]
    assert row["amount_eur"] == "-1234.56"
    assert "," not in row["amount_eur"]


def test_the_rate_date_and_lag_are_recorded(conn):
    """Without them, a weekend purchase looks converted at a rate that does
    not exist and the arithmetic cannot be checked."""
    ledger.add(conn, "2026-02-05", "SUNDAY MARKET", -2000)
    row = rows_of(export.transactions_csv(conn))[0]
    assert row["date"] == "2026-02-05"
    assert row["rate_date"] == "2026-02-02"
    assert row["rate_lag_days"] == "3"
    assert row["rate_cad"] == "1.6033"


def test_a_same_day_rate_reports_a_lag_of_zero(conn):
    ledger.add(conn, "2026-02-02", "TESCO", -2000)
    assert rows_of(export.transactions_csv(conn))[0]["rate_lag_days"] == "0"


def test_a_row_with_no_rate_keeps_its_euros_and_carries_a_note(conn):
    """Rather than a zero, which reads as a free purchase."""
    ledger.add(conn, "2019-01-01", "ANCIENT", -5000)
    row = rows_of(export.transactions_csv(conn, month="2019-01"))[0]
    assert row["amount_eur"] == "-50.00"
    assert row["amount_cad"] == ""
    assert row["note"]


# ---------------------------------------------------------------- totalling

def test_the_file_adds_up_to_the_ledger(conn):
    """The property a reader most needs to trust."""
    for day, what, amount in (("02", "TESCO", -4550), ("03", "LIDL", -2210),
                              ("05", "REFUND", 1000)):
        ledger.add(conn, f"2026-02-{day}", what, amount)
    agrees, from_file, from_ledger = export.reconciles(conn, month="2026-02")
    assert agrees
    assert from_file == from_ledger == -5760


def test_the_euro_column_survives_a_round_trip(conn):
    """Written by this app, read back by money.parse -- the same figure."""
    for amount in (-4550, -123456, 5, -1, 250000):
        ledger.add(conn, "2026-02-02", f"ITEM {amount}", amount)
    for row in rows_of(export.transactions_csv(conn)):
        assert money.parse(row["amount_eur"]) == \
            next(t["amount_eur"] for t in ledger.transactions(conn)
                 if t["description"] == row["description"])


def test_converted_rows_total_to_the_converted_total(conn):
    """Rounding is done once per row, so the CAD column sums to whatever the
    rows say -- and the file shows the rows."""
    for day in ("02", "03"):
        ledger.add(conn, f"2026-02-{day}", f"ITEM {day}", -5000)
    rows = rows_of(export.transactions_csv(conn))
    cad = money.total(money.parse(r["amount_cad"]) for r in rows)
    assert cad == -16034            # two rows of -80.17


# ---------------------------------------------------------------- filtering

def test_the_file_honours_the_month_filter(conn):
    ledger.add(conn, "2026-02-02", "FEBRUARY", -1000)
    ledger.add(conn, "2026-01-02", "JANUARY", -2000)
    rows = rows_of(export.transactions_csv(conn, month="2026-02"))
    assert [r["description"] for r in rows] == ["FEBRUARY"]


def test_the_file_honours_category_and_search(conn):
    ledger.add(conn, "2026-02-02", "TESCO", -1000, category="Groceries")
    ledger.add(conn, "2026-02-03", "DB BAHN", -2000, category="Transport")
    assert len(rows_of(export.transactions_csv(conn,
                                               category="Transport"))) == 1
    assert len(rows_of(export.transactions_csv(conn, search="tesco"))) == 1


def test_an_empty_ledger_still_writes_a_header(conn):
    """A file with no header is not a CSV, and something downstream will
    choke on it rather than reporting nothing to report."""
    text = export.transactions_csv(conn)
    assert text.strip() == ",".join(export.COLUMNS)


# ------------------------------------------------------------------ summary

def test_the_summary_file_reports_budget_against_actual(conn):
    budgets.set_cap(conn, "Groceries", 40000)
    ledger.add(conn, "2026-02-02", "TESCO", -20000, category="Groceries")
    row = rows_of(export.summary_csv(conn, "2026-02",
                                     today=dt.date(2026, 2, 14)))[0]
    assert row["category"] == "Groceries"
    assert row["spent_eur"] == "200.00"
    assert row["cap_eur"] == "400.00"
    assert row["remaining_eur"] == "200.00"
    assert row["percent_of_cap"] == "50"
    assert row["state"] == "fine"


# -------------------------------------------------------------------- disk

def test_writing_to_disk_produces_no_blank_lines(conn):
    """csv writes its own line endings. Without newline="" every one becomes
    a blank line between rows on Windows."""
    ledger.add(conn, "2026-02-02", "TESCO", -1000)
    ledger.add(conn, "2026-02-03", "LIDL", -2000)
    path = os.path.join(os.path.dirname(fxrates.cache_path()), "out.csv")
    assert export.write(conn, path, month="2026-02") == 2
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    assert lines and all(line.strip() for line in lines)
    assert len(lines) == 3          # header plus two rows


def test_the_filename_says_what_it_holds(conn):
    assert export.filename("transactions", "2026-02") == \
        "wallet-transactions-2026-02.csv"
    assert export.filename("summary", "2026-02").endswith("2026-02.csv")
