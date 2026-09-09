"""Reading bank CSVs of whatever shape.

The formats here are the real ones: a Revolut-style signed Amount column, a
UK-style Paid out / Paid in pair, and a German semicolon file with decimal
commas. Each has a way of going silently wrong, and those are the tests.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db          # noqa: E402
import fxrates     # noqa: E402
import importers   # noqa: E402
import ledger      # noqa: E402


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    fxrates.reset()
    connection = db.connect(str(tmp_path))
    yield connection
    connection.close()
    fxrates.reset()


SIGNED = (
    "Type,Completed Date,Description,Amount,Currency\n"
    "CARD_PAYMENT,2026-02-02,Tesco Stores 4823,-45.50,EUR\n"
    "CARD_PAYMENT,2026-02-03,DB Bahn Ticket,-39.90,EUR\n"
    "TOPUP,2026-02-01,Salary,2500.00,EUR\n"
)

IN_OUT = (
    "Date,Description,Paid out,Paid in,Balance\n"
    "02/02/2026,TESCO STORES 4823,45.50,,1200.00\n"
    "03/02/2026,DB BAHN TICKET,39.90,,1160.10\n"
    "01/02/2026,SALARY,,2500.00,3660.10\n"
)

GERMAN = (
    "Buchungstag;Buchungstext;Betrag;Waehrung\n"
    "02.02.2026;REWE SAGT DANKE;-45,50;EUR\n"
    "03.02.2026;DB VERTRIEB GMBH;-1.234,56;EUR\n"
)


# ----------------------------------------------------------------- sniffing

def test_a_comma_file_is_read_as_commas():
    found = importers.sniff(SIGNED)
    assert found["delimiter"] == ","
    assert len(found["rows"]) == 3


def test_a_semicolon_file_is_not_split_on_its_decimal_commas():
    """A German export is semicolon-separated *because* the comma is its
    decimal point. Guessing comma turns every amount into two columns."""
    found = importers.sniff(GERMAN)
    assert found["delimiter"] == ";"
    assert found["mapping"]["amount"] == "Betrag"
    assert len(found["rows"]) == 2


def test_the_columns_are_guessed_from_the_header_names():
    mapping = importers.sniff(SIGNED)["mapping"]
    assert mapping["date"] == "Completed Date"
    assert mapping["description"] == "Description"
    assert mapping["amount"] == "Amount"
    assert mapping["currency"] == "Currency"


def test_separate_in_and_out_columns_are_recognised():
    mapping = importers.sniff(IN_OUT)["mapping"]
    assert mapping["amount_out"] == "Paid out"
    assert mapping["amount_in"] == "Paid in"
    assert mapping["expenses_positive"] is True


def test_german_headers_are_recognised():
    mapping = importers.sniff(GERMAN)["mapping"]
    assert mapping["date"] == "Buchungstag"
    assert mapping["description"] == "Buchungstext"
    assert mapping["currency"] == "Waehrung"


def test_an_empty_file_is_refused_clearly():
    for bad in ("", "   \n\n"):
        with pytest.raises(importers.ImportProblem):
            importers.sniff(bad)


def test_a_byte_order_mark_does_not_corrupt_the_first_header():
    """Exports from Excel carry one, and it turns "Date" into "\\ufeffDate",
    so every lookup of that column silently returns nothing."""
    found = importers.sniff("﻿" + SIGNED)
    assert found["headers"][0] == "Type"
    assert found["mapping"]["date"] == "Completed Date"


def test_bytes_are_accepted_as_well_as_text():
    found = importers.sniff(SIGNED.encode("utf-8"))
    assert len(found["rows"]) == 3


# ------------------------------------------------------------------- signs

def test_a_signed_amount_column_keeps_its_signs():
    out = importers.preview(importers.sniff(SIGNED))
    amounts = {e["description"]: e["amount_eur"] for e in out["rows"]}
    assert amounts["Tesco Stores 4823"] == -4550
    assert amounts["Salary"] == 250000


def test_separate_columns_make_paid_out_negative_and_paid_in_positive():
    """Both columns hold unsigned figures; the sign is which column it is in.
    Read the wrong way, every expense becomes income and the budget reports a
    surplus."""
    out = importers.preview(importers.sniff(IN_OUT))
    amounts = {e["description"]: e["amount_eur"] for e in out["rows"]}
    assert amounts["TESCO STORES 4823"] == -4550
    assert amounts["SALARY"] == 250000


def test_an_unsigned_amount_column_can_be_told_expenses_are_positive():
    text = ("Date,Description,Amount\n"
            "2026-02-02,TESCO,45.50\n")
    sniffed = importers.sniff(text)
    mapping = dict(sniffed["mapping"], expenses_positive=True)
    out = importers.preview(sniffed, mapping)
    assert out["rows"][0]["amount_eur"] == -4550


def test_the_german_decimal_comma_survives_the_import():
    """-1.234,56 is one thousand two hundred euros, not one euro twenty-three.
    This is the factor-of-a-thousand error."""
    out = importers.preview(importers.sniff(GERMAN))
    amounts = {e["description"]: e["amount_eur"] for e in out["rows"]}
    assert amounts["REWE SAGT DANKE"] == -4550
    assert amounts["DB VERTRIEB GMBH"] == -123456


# ---------------------------------------------------------------- currency

def test_a_row_in_another_currency_is_refused_not_treated_as_euros():
    """A 200 SEK lunch booked as EUR 200 is a twenty-fold error that looks
    entirely plausible, so nothing downstream would ever flag it."""
    text = ("Date,Description,Amount,Currency\n"
            "2026-02-02,TESCO,-45.50,EUR\n"
            "2026-02-03,STOCKHOLM LUNCH,-200.00,SEK\n")
    out = importers.preview(importers.sniff(text))
    assert out["readable"] == 1
    assert out["unreadable"] == 1
    assert "SEK" in out["problems"][0]["why"]


def test_a_file_with_no_currency_column_is_taken_at_its_word():
    """Most single-currency exports have no such column."""
    text = ("Date,Description,Amount\n2026-02-02,TESCO,-45.50\n")
    assert importers.preview(importers.sniff(text))["readable"] == 1


# ----------------------------------------------------------------- preview

def test_the_preview_totals_only_the_spending():
    out = importers.preview(importers.sniff(SIGNED))
    assert out["spending_total"] == -8540      # the salary is not spending
    assert out["readable"] == 3


def test_bad_rows_are_listed_rather_than_stopping_at_the_first():
    """"Column looks wrong" is the useful message, and one row cannot show
    it."""
    text = ("Date,Description,Amount\n"
            "2026-02-02,GOOD,-45.50\n"
            "not-a-date,BAD DATE,-1.00\n"
            "2026-02-04,,-1.00\n")
    out = importers.preview(importers.sniff(text))
    assert out["readable"] == 1
    assert out["unreadable"] == 2
    assert {p["row"] for p in out["problems"]} == {3, 4}


def test_a_mostly_unreadable_file_says_the_mapping_is_wrong(monkeypatch):
    """Rather than importing the one row that happened to parse."""
    text = ("A,B,C\n" + "".join(f"x{i},y{i},z{i}\n" for i in range(10)))
    sniffed = importers.sniff(text)
    mapping = dict(sniffed["mapping"], date="A", description="B", amount="C")
    with pytest.raises(importers.ImportProblem) as caught:
        importers.preview(sniffed, mapping)
    assert "mapping" in str(caught.value)


def test_a_missing_date_or_description_choice_is_refused():
    sniffed = importers.sniff(SIGNED)
    for field in ("date", "description"):
        with pytest.raises(importers.ImportProblem):
            importers.preview(sniffed, dict(sniffed["mapping"], **{field: None}))


def test_the_preview_writes_nothing(conn):
    """The whole point of a preview."""
    importers.preview(importers.sniff(SIGNED))
    assert ledger.transactions(conn) == []


# ------------------------------------------------------------------ loading

def test_loading_records_the_rows(conn):
    out = importers.load(conn, importers.preview(importers.sniff(SIGNED)),
                         source="import:revolut")
    assert out["added"] == 3
    assert out["duplicate"] == 0
    assert len(ledger.transactions(conn)) == 3
    assert ledger.transactions(conn)[0]["source"] == "import:revolut"


def test_importing_the_same_file_twice_adds_nothing(conn):
    previewed = importers.preview(importers.sniff(SIGNED))
    importers.load(conn, previewed)
    again = importers.load(conn, importers.preview(importers.sniff(SIGNED)))
    assert again["added"] == 0
    assert again["duplicate"] == 3
    assert len(ledger.transactions(conn)) == 3


def test_the_same_purchase_from_two_different_bank_formats_is_one_row(conn):
    """The strongest form of the duplicate rule: the signed export and the
    in/out export of the same February, imported in turn."""
    importers.load(conn, importers.preview(importers.sniff(SIGNED)))
    before = len(ledger.transactions(conn))
    out = importers.load(conn, importers.preview(importers.sniff(IN_OUT)))
    assert len(ledger.transactions(conn)) == before
    assert out["added"] == 0
    assert out["duplicate"] == 3


def test_the_file_categories_are_ignored_by_default(conn):
    """A bank's idea of "Shopping" rarely matches yours, and a rule you wrote
    is a better guide than a category somebody else's algorithm assigned."""
    text = ("Date,Description,Amount,Category\n"
            "2026-02-02,TESCO,-45.50,Bills\n")
    previewed = importers.preview(importers.sniff(text))
    importers.load(conn, previewed)
    assert ledger.transactions(conn)[0]["category"] == db.UNCATEGORISED


def test_the_file_categories_can_be_used_on_request(conn):
    text = ("Date,Description,Amount,Category\n"
            "2026-02-02,TESCO,-45.50,Bills\n")
    previewed = importers.preview(importers.sniff(text))
    importers.load(conn, previewed, use_file_categories=True)
    assert ledger.transactions(conn)[0]["category"] == "Bills"


def test_rules_are_applied_to_imported_rows(conn):
    ledger.add_rule(conn, "tesco", "Groceries")
    importers.load(conn, importers.preview(importers.sniff(SIGNED)))
    tesco = ledger.transactions(conn, search="tesco")[0]
    assert tesco["category"] == "Groceries"


def test_a_tiny_file_reports_its_bad_rows_instead_of_refusing_itself():
    """Two bad rows out of three is 67%, which says nothing about the column
    mapping -- it is probably just two bad rows. The ratio only means
    something once there are enough rows to judge."""
    text = ("Date,Description,Amount\n"
            "2026-02-02,GOOD,-45.50\n"
            "not-a-date,BAD DATE,-1.00\n")
    out = importers.preview(importers.sniff(text))
    assert out["readable"] == 1
    assert out["unreadable"] == 1
