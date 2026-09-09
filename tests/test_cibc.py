"""A Canadian card used in Europe.

Two things this covers that nothing else did.

**The file has no header row.** CIBC's Download Transactions gives you bare
rows, so the importer's first line was being consumed as column names -- which
ate a real purchase and left nothing recognisable to map, making the file
entirely unimportable. Which columns are which is now worked out from the
values, because CIBC alone exports at least three shapes and a per-bank
profile would have to guess which one you downloaded.

**The card does not bill euros.** CIBC converts at the Visa rate and adds
2.5%, so a euro purchase arrives already in CAD, at a rate you did not choose
and were not told. When the original euro figure is recoverable from the
description, keeping both makes the conversion cost measurable -- and the
measured figure landing near 2.5% is the check that the parse and the rate
lookup are both right.
"""
import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db          # noqa: E402
import fxcost      # noqa: E402
import fxrates     # noqa: E402
import importers   # noqa: E402
import layout      # noqa: E402
import ledger      # noqa: E402
import money       # noqa: E402

# The three layouts CIBC is documented to produce, all headerless.
CARD = ("2026-09-02,REWE SAGT DANKE 4821,85.94,,4506********1234\n"
        "2026-09-04,DB VERTRIEB GMBH,65.53,,4506********1234\n"
        "2026-09-07,PAYMENT THANK YOU,,500.00,4506********1234\n")

CHEQUING = ("2026-09-02,POS PURCHASE REWE,85.94,,2914.06\n"
            "2026-09-04,POS PURCHASE DB VERTRIEB,65.53,,2848.53\n"
            "2026-09-05,PAYROLL DEPOSIT,,2400.00,5248.53\n")

SIGNED = ("2026-09-02,REWE SAGT DANKE,-85.94,2914.06\n"
          "2026-09-04,DB VERTRIEB GMBH,-65.53,2848.53\n"
          "2026-09-05,PAYROLL DEPOSIT,2400.00,5248.53\n")

FOREIGN = ("2026-09-02,REWE SAGT DANKE 52.30 EUR,85.94,,4506********1234\n"
           "2026-09-04,DB VERTRIEB GMBH 39.90 EUR,65.53,,4506********1234\n")


@pytest.fixture
def wallet(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    fxrates.reset()
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("date,CAD,USD\n2026-09-04,1.6040,1.1620\n"
                     "2026-09-02,1.6033,1.1614\n")
    connection = db.connect(str(tmp_path))
    yield connection, str(tmp_path)
    connection.close()
    fxrates.reset()


# ==========================================================================
#  A file with no header row
# ==========================================================================

def test_a_headerless_file_keeps_all_of_its_rows():
    """The first line is a purchase, not a set of column names. Reading it as
    a header silently lost it."""
    found = importers.sniff(CARD)
    assert found["headerless"] is True
    assert len(found["rows"]) == 3


def test_a_file_with_a_header_row_is_still_read_as_one():
    found = importers.sniff("Date,Description,Amount\n2026-09-02,TESCO,-1.00\n")
    assert found["headerless"] is False
    assert found["headers"] == ["Date", "Description", "Amount"]
    assert len(found["rows"]) == 1


@pytest.mark.parametrize("text,label", [
    (CARD, "credit card: debit, credit, card number"),
    (CHEQUING, "chequing: debit, credit, running balance"),
    (SIGNED, "one signed amount, plus a balance"),
])
def test_every_documented_cibc_layout_imports(text, label):
    """A profile per bank would have to guess which of these you downloaded.
    The values do not have to guess."""
    out = importers.preview(importers.sniff(text))
    assert out["readable"] == 3, label
    assert out["unreadable"] == 0, label
    spent = {e["description"]: e["amount_eur"] for e in out["rows"]}
    assert min(spent.values()) == -8594, label     # the largest purchase
    assert max(spent.values()) > 0, label          # the credit stayed positive


def test_a_running_balance_is_not_mistaken_for_an_amount():
    """A balance is filled on every row; money out and money in are each
    blank whenever the other is used. That sparseness is the tell."""
    mapping = importers.sniff(CHEQUING)["mapping"]
    assert mapping["amount_out"] == "column 3"
    assert mapping["amount_in"] == "column 4"
    assert "column 5" not in mapping.values(), "the balance was picked up"


def test_a_masked_card_number_is_not_mistaken_for_the_description():
    """It is short and repeats; a merchant name is long and varies, and it is
    the one a reader needs in order to recognise the purchase."""
    mapping = importers.sniff(CARD)["mapping"]
    assert mapping["description"] == "column 2"


def test_a_description_containing_a_figure_is_still_the_description():
    """money.parse strips everything that is not a digit, which is right for
    reading a cell already known to be an amount and wrong for deciding which
    column is the amount: "REWE SAGT DANKE 52.30 EUR" parsed to 52.30, so the
    description column was classified as numeric and the file was left with
    no description column at all."""
    mapping = importers.sniff(FOREIGN)["mapping"]
    assert mapping["description"] == "column 2"
    assert mapping["date"] == "column 1"


@pytest.mark.parametrize("cell,is_amount", [
    ("85.94", True), ("-1.234,56", True), ("52.30 EUR", True),
    ("(12,34)", True), ("0.00", True), ("€52.30", True),
    ("REWE SAGT DANKE 52.30 EUR", False),
    ("4506********1234", False), ("PAYROLL DEPOSIT", False),
    ("2026-09-02", False),
])
def test_what_counts_as_a_figure(cell, is_amount):
    assert layout.is_amount(cell) is is_amount


def test_a_headerless_file_of_one_row_still_works():
    out = importers.preview(importers.sniff("2026-09-02,TESCO,-45.50\n"))
    assert out["readable"] == 1
    assert out["rows"][0]["amount_eur"] == -4550


def test_the_inferred_mapping_can_still_be_corrected():
    """It is a guess, and it is shown in the preview for that reason. For a
    headerless file that matters more, not less."""
    sniffed = importers.sniff(SIGNED)
    corrected = dict(sniffed["mapping"], description="column 1",
                     date="column 1")
    out = importers.preview(sniffed, corrected)
    assert out["readable"] == 3


# ==========================================================================
#  Reading the original amount back out of a description
# ==========================================================================

@pytest.mark.parametrize("description,expected", [
    ("REWE SAGT DANKE 52.30 EUR", (5230, "EUR")),
    ("REWE SAGT DANKE EUR 52.30", (5230, "EUR")),
    ("LIDL BERLIN 22,10 EUR", (2210, "EUR")),
    ("SPOTIFY 10.99 EUR MONTHLY", (1099, "EUR")),
])
def test_the_original_amount_is_recovered(description, expected):
    assert fxcost.foreign_amount(description, "CAD") == expected


@pytest.mark.parametrize("description", [
    "TIM HORTONS 1234",                 # no currency at all
    "PAYMENT THANK YOU",                # no figure
    "REWE SAGT DANKE 85.94 CAD",        # restates the charge, not an original
    "",
    None,
])
def test_nothing_is_invented_when_there_is_no_original(description):
    """An FX cost computed from a guessed original is worse than none."""
    assert fxcost.foreign_amount(description, "CAD") is None


def test_the_billed_currency_is_never_read_as_the_original():
    """A CAD line reading "85.94 CAD" is restating the charge. Treating it as
    an original would produce a nonsense zero-cost comparison."""
    assert fxcost.foreign_amount("SHOP 85.94 CAD", "CAD") is None
    assert fxcost.foreign_amount("SHOP 85.94 CAD", "EUR") == (8594, "CAD")


# ==========================================================================
#  What the conversion cost
# ==========================================================================

def test_the_comparison_measures_the_markup():
    """EUR 52.30 at the ECB rate of 1.6033 is CA$83.85. CIBC billed CA$85.94.
    The difference is what the conversion cost."""
    out = fxcost.compare(8594, "CAD", -5230, "1.6033")
    assert out["billed_text"] == "CA$85.94"
    assert out["reference_text"] == "CA$83.85"
    assert out["cost_text"] == "CA$2.09"
    assert 2.4 < out["percent"] < 2.6
    assert out["plausible"] is True


def test_the_measured_cost_lands_near_the_published_fee():
    """CIBC publishes 2.5%. The app measures rather than assumes, so a
    measured figure near the published one is the check that both the parse
    and the rate lookup are right."""
    out = fxcost.compare(8594, "CAD", -5230, "1.6033")
    assert abs(out["share"] - float(fxcost.TYPICAL_CARD_FEE)) < 0.005


def test_an_implausible_markup_is_flagged_rather_than_reported():
    """A 40% markup is not a fee, it is a misread amount -- most likely the
    original was parsed from the wrong part of the description."""
    out = fxcost.compare(12000, "CAD", -5230, "1.6033")
    assert out["plausible"] is False


def test_a_missing_piece_gives_no_comparison_rather_than_a_zero():
    assert fxcost.compare(None, "CAD", -5230, "1.6033") is None
    assert fxcost.compare(8594, "CAD", None, "1.6033") is None
    assert fxcost.compare(8594, "CAD", -5230, None) is None
    assert fxcost.compare(8594, "CAD", 0, "1.6033") is None


def test_the_summary_says_how_many_rows_it_covers():
    """"You paid CA$14 in conversion fees" means something different over
    three purchases than over forty, and a reader who is not told will assume
    it covers everything."""
    rows = [
        {"charged_currency": "CAD",
         "fx": fxcost.compare(8594, "CAD", -5230, "1.6033")},
        {"charged_currency": "CAD",
         "fx": fxcost.compare(6553, "CAD", -3990, "1.6040")},
        {"charged_currency": None, "fx": None},
    ]
    out = fxcost.summarise(rows)
    assert out["available"] is True
    assert out["rows"] == 2
    assert out["of"] == 3
    assert out["cost_text"] == "CA$3.62"
    assert 2.3 < out["percent"] < 2.6


def test_the_summary_reports_nothing_to_report():
    out = fxcost.summarise([{"charged_currency": None, "fx": None}])
    assert out["available"] is False
    assert out["rows"] == 0
    assert out["of"] == 1


def test_an_implausible_row_is_left_out_of_the_summary():
    rows = [{"charged_currency": "CAD",
             "fx": fxcost.compare(12000, "CAD", -5230, "1.6033")}]
    assert fxcost.summarise(rows)["available"] is False


# ==========================================================================
#  End to end
# ==========================================================================

def test_a_cad_billed_euro_purchase_imports_with_its_conversion_cost(wallet):
    conn, directory = wallet
    sniffed = importers.sniff(FOREIGN)
    mapping = dict(sniffed["mapping"], billed_currency="CAD")
    out = importers.load(conn, importers.preview(sniffed, mapping),
                         source="cibc")
    assert out["added"] == 2

    rows = ledger.transactions(conn, directory=directory)
    rewe = next(r for r in rows if "REWE" in r["description"])
    # Stored in euros -- that is the currency the purchase was made in, and
    # the only figure that does not depend on somebody's exchange rate.
    assert rewe["amount_eur"] == -5230
    assert rewe["charged_minor"] == 8594
    assert rewe["charged_currency"] == "CAD"
    assert rewe["fx"]["cost_text"] == "CA$2.09"
    assert 2.4 < rewe["fx"]["percent"] < 2.6


def test_the_month_total_is_in_euros_not_the_billed_currency(wallet):
    """The ledger holds euros, so the budget is in euros. Mixing the billed
    CAD figure into it would double-count the conversion."""
    conn, directory = wallet
    sniffed = importers.sniff(FOREIGN)
    importers.load(conn, importers.preview(
        sniffed, dict(sniffed["mapping"], billed_currency="CAD")))
    total = money.total(r["amount_eur"] for r in
                        ledger.transactions(conn, directory=directory))
    assert total == -(5230 + 3990)


def test_a_cad_row_with_no_euro_original_is_still_refused(wallet):
    """The rule that predates all of this: a 200 SEK lunch booked as EUR 200
    is a twenty-fold error that looks entirely plausible. A domestic Canadian
    purchase has no euro amount to recover, so it is declined rather than
    guessed at."""
    conn, _directory = wallet
    text = ("Date,Description,Amount,Currency\n"
            "2026-09-02,TIM HORTONS 1234,-4.85,CAD\n")
    out = importers.preview(importers.sniff(text))
    assert out["readable"] == 0
    assert out["unreadable"] == 1
    assert "EUR" in out["problems"][0]["why"]


def test_an_ordinary_euro_purchase_carries_no_conversion_cost(wallet):
    """Most rows. There is nothing to compare, and inventing a zero would
    imply the conversion was free rather than absent."""
    conn, directory = wallet
    ledger.add(conn, "2026-09-02", "REWE SAGT DANKE", -5230)
    row = ledger.transactions(conn, directory=directory)[0]
    assert row["charged_minor"] is None
    assert row["fx"] is None


def test_an_older_database_gains_the_new_columns(tmp_path, monkeypatch):
    """CREATE TABLE IF NOT EXISTS does nothing to a table that already
    exists, so a ledger created before these columns were added would be
    missing them and every query naming one would fail. The file holds the
    only copy of somebody's spending, so the migration is additive.
    """
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    import sqlite3
    path = db.db_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, "
                "spent_on TEXT, description TEXT, merchant TEXT, "
                "amount_eur INTEGER, category TEXT, source TEXT, "
                "fingerprint TEXT UNIQUE, created_at TEXT)")
    old.execute("INSERT INTO transactions (spent_on, description, merchant, "
                "amount_eur, category, source, fingerprint, created_at) "
                "VALUES ('2026-09-02','OLD ROW','Old Row',-1000,"
                "'Groceries','manual','abc','2026-09-02T00:00:00')")
    old.commit()
    old.close()

    connection = db.connect(str(tmp_path))
    try:
        columns = {row[1] for row in
                   connection.execute("PRAGMA table_info(transactions)")}
        assert "charged_minor" in columns
        assert "charged_currency" in columns
        # And the row that was already there survived untouched.
        kept = connection.execute(
            "SELECT description, amount_eur FROM transactions").fetchone()
        assert kept["description"] == "OLD ROW"
        assert kept["amount_eur"] == -1000
    finally:
        connection.close()


def test_the_migration_runs_twice_without_complaint(tmp_path, monkeypatch):
    """connect() is called per request, so the migration runs constantly."""
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    for _ in range(3):
        connection = db.connect(str(tmp_path))
        connection.close()
    connection = db.connect(str(tmp_path))
    try:
        ledger.add(connection, dt.date(2026, 9, 2), "STILL WORKS", -100)
        assert len(ledger.transactions(connection,
                                       directory=str(tmp_path))) == 1
    finally:
        connection.close()


# ------------------------------------------------- the remaining branches

@pytest.mark.parametrize("description", [
    "SHOP 52.30 XYZ",        # a currency this app does not convert to
    "SHOP 0.00 EUR",         # a zero original is not an original
    "SHOP ..,, EUR",         # a figure that will not parse
])
def test_an_unusable_original_amount_is_ignored(description):
    assert fxcost.foreign_amount(description, "CAD") is None


def test_a_reference_that_rounds_to_nothing_gives_no_comparison():
    """A one-cent purchase at a tiny rate. Dividing by a zero reference would
    raise; reporting no comparison is the honest answer."""
    assert fxcost.compare(100, "CAD", 1, "0.001") is None


def test_an_all_blank_column_is_ignored_entirely():
    """CIBC's card export has an empty credit column on a spending-only
    month, and a column with nothing in it says nothing about its type."""
    text = ("2026-09-02,REWE,85.94,,\n"
            "2026-09-04,DB VERTRIEB,65.53,,\n")
    mapping = importers.sniff(text)["mapping"]
    assert mapping["date"] == "column 1"
    assert mapping["description"] == "column 2"
    assert importers.preview(importers.sniff(text))["readable"] == 2


def test_inferring_from_no_rows_at_all_returns_an_empty_mapping():
    assert layout.infer(["column 1"], []) == {
        "date": None, "description": None, "amount": None,
        "amount_out": None, "amount_in": None, "currency": None,
        "category": None}


def test_a_single_sparse_amount_column_is_taken_as_money_out():
    """One column, blank on some rows, no negatives -- a debit-only export."""
    text = ("2026-09-02,REWE,85.94,X\n"
            "2026-09-04,DB VERTRIEB,,X\n"
            "2026-09-05,LIDL,22.10,X\n")
    mapping = importers.sniff(text)["mapping"]
    assert mapping["amount_out"] == "column 3"
    assert mapping["expenses_positive"] is True


def test_a_row_whose_date_cell_is_blank_is_not_taken_as_the_header():
    """_looks_like_data skips empty cells before deciding."""
    assert layout.looks_like_data(["", "  ", "2026-09-02"]) is True
    assert layout.looks_like_data(["", "  ", ""]) is False


def test_the_migration_skips_a_table_that_does_not_exist_yet(tmp_path,
                                                             monkeypatch):
    """_migrate runs before the table is guaranteed to be there, so PRAGMA
    returning nothing has to mean "nothing to do" rather than an error."""
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    monkeypatch.setitem(db.LATER_COLUMNS, "not_a_table", (("x", "TEXT"),))
    connection = db.connect(str(tmp_path))
    try:
        assert connection.execute("PRAGMA table_info(not_a_table)").fetchall() == []
    finally:
        connection.close()


def test_a_numeric_column_with_some_junk_still_reports_its_signs():
    """A column classifies as numeric at an 80% threshold, so up to a fifth
    of its cells can be unparseable -- and the sign check reads every raw
    cell, not the filtered ones. Unlike the two guards this replaced, this
    branch is genuinely reachable."""
    rows = [[f"2026-09-0{n}", "SHOP", v, "X"] for n, v in
            enumerate(["-10.00", "-20.00", "-30.00", "-40.00", "n/a"], start=1)]
    headers = ["column 1", "column 2", "column 3", "column 4"]
    assert layout._has_negatives(rows, headers, "column 3") is True

    positive = [[f"2026-09-0{n}", "SHOP", v, "X"] for n, v in
                enumerate(["10.00", "20.00", "n/a", "40.00"], start=1)]
    assert layout._has_negatives(positive, headers, "column 3") is False


def test_a_headerless_file_with_no_figures_at_all_chooses_no_amount():
    """A date and a description and nothing numeric. There is no amount to
    guess at, so the mapping says so and preview refuses the rows rather than
    inventing zeroes."""
    text = ("2026-09-02,REWE SAGT DANKE\n"
            "2026-09-04,DB VERTRIEB GMBH\n")
    sniffed = importers.sniff(text)
    mapping = sniffed["mapping"]
    assert mapping["date"] == "column 1"
    assert mapping["description"] == "column 2"
    assert mapping["amount"] is None
    assert mapping["amount_out"] is None
    assert mapping["amount_in"] is None
    assert mapping["expenses_positive"] is False

    out = importers.preview(sniffed)
    assert out["readable"] == 0
    assert "no amount column" in out["problems"][0]["why"]
