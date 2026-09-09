"""The .qfx file CIBC actually offers, and why it is the better one.

There is no way to sync with CIBC programmatically -- no open-banking API
(Phase 1 read access has no operational date), no OFX Direct Connect (CIBC
supports Web Connect only), and no aggregator that does not either need a
commercial agreement or log into your online banking on your behalf. So the
file is the interface.

But the CSV is the worse of the two files CIBC exports. OFX carries three
things the CSV throws away:

    <FITID>     the bank's own id for the transaction
    <CURSYM>    the currency the purchase was actually made in
    <CURRATE>   the exact rate the bank converted at

Which means the euro amount is the bank's own arithmetic rather than a regex
over a description, and "have I seen this" can be an exact question.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db          # noqa: E402
import fxrates     # noqa: E402
import importers   # noqa: E402
import ledger      # noqa: E402
import money       # noqa: E402
import ofx         # noqa: E402

# The SGML dialect, with the unclosed leaf tags real files have.
QFX = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>CAD
<STMTTRN><TRNTYPE>POS<DTPOSTED>20260902120000.000[-5:EST]<TRNAMT>-85.94
<FITID>2026090200001<NAME>REWE SAGT DANKE<MEMO>REWE SAGT DANKE 4821 BERLIN
<ORIGCURRENCY><CURRATE>1.64321<CURSYM>EUR</ORIGCURRENCY></STMTTRN>
<STMTTRN><TRNTYPE>POS<DTPOSTED>20260903<TRNAMT>-4.85
<FITID>2026090300002<NAME>TIM HORTONS 1234</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260907<TRNAMT>500.00
<FITID>2026090700003<NAME>PAYMENT THANK YOU</STMTTRN>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""

# The XML dialect, version 2, properly closed.
OFX2 = """<?xml version="1.0" encoding="UTF-8"?>
<?OFX OFXHEADER="200" VERSION="220"?>
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
  <CURDEF>EUR</CURDEF>
  <BANKTRANLIST>
    <STMTTRN>
      <TRNTYPE>POS</TRNTYPE>
      <DTPOSTED>20260902</DTPOSTED>
      <TRNAMT>-52.30</TRNAMT>
      <FITID>X1</FITID>
      <NAME>REWE SAGT DANKE</NAME>
    </STMTTRN>
  </BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""


@pytest.fixture
def wallet(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    fxrates.reset()
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("date,CAD,USD\n2026-09-07,1.6040,1.1620\n"
                     "2026-09-02,1.6033,1.1614\n")
    connection = db.connect(str(tmp_path))
    yield connection, str(tmp_path)
    connection.close()
    fxrates.reset()


# ==========================================================================
#  Reading the file
# ==========================================================================

def test_it_is_recognised_by_content_not_by_extension():
    """CIBC names its download .qfx, others .ofx, and a synced folder renames
    things. The name is the least reliable thing about a file."""
    assert ofx.looks_like_ofx(QFX) is True
    assert ofx.looks_like_ofx(OFX2) is True
    assert ofx.looks_like_ofx("Date,Description,Amount\n2026-09-02,X,-1\n") is False


def test_the_sgml_dialect_with_unclosed_tags_reads():
    """Version 1 files leave leaf tags open, and real banks are looser still.
    A strict parser fails on them; the useful behaviour is to read what is
    there."""
    found = ofx.transactions(QFX)
    assert len(found) == 3
    assert found[0]["spent_on"] == "2026-09-02"
    assert found[0]["amount"] == -8594
    assert found[0]["fitid"] == "2026090200001"


def test_the_xml_dialect_reads_the_same_way():
    found = ofx.transactions(OFX2)
    assert len(found) == 1
    assert found[0]["amount"] == -5230
    assert found[0]["fitid"] == "X1"


def test_the_account_currency_is_read_from_curdef():
    """The other half of the ORIGCURRENCY pair -- what the purchase was
    converted *into*. Without it the importer would have to be told."""
    assert ofx.account_currency(QFX) == "CAD"
    assert ofx.account_currency(OFX2) == "EUR"
    assert ofx.account_currency("<OFX></OFX>") is None


def test_a_timestamp_and_timezone_reduce_to_a_date():
    """The ledger stores days. A time component would make two purchases on
    one date look like different dates to everything downstream."""
    assert ofx.transactions(QFX)[0]["spent_on"] == "2026-09-02"


def test_the_original_currency_and_rate_are_read_from_the_block():
    """Not scraped from a description. The bank states both."""
    first = ofx.transactions(QFX)[0]
    assert first["origin_currency"] == "EUR"
    assert str(first["origin_rate"]) == "1.64321"


def test_a_domestic_row_has_no_origin():
    """And must not inherit the previous row's. The currency block is
    stripped per transaction for exactly this reason."""
    second = ofx.transactions(QFX)[1]
    assert second["origin_currency"] is None
    assert second["origin_rate"] is None


def test_a_repeated_name_in_the_memo_is_not_doubled():
    """CIBC often writes NAME as the prefix of MEMO, so joining them blindly
    gives "REWE SAGT DANKE REWE SAGT DANKE 4821 BERLIN"."""
    assert ofx.transactions(QFX)[0]["description"] == \
        "REWE SAGT DANKE 4821 BERLIN"


@pytest.mark.parametrize("name,memo,expected", [
    ("REWE", "REWE 4821 BERLIN", "REWE 4821 BERLIN"),
    ("REWE 4821 BERLIN", "REWE", "REWE 4821 BERLIN"),
    ("REWE", "", "REWE"),
    ("", "REWE 4821", "REWE 4821"),
    ("TESCO", "CARD 1234", "TESCO CARD 1234"),
])
def test_how_name_and_memo_are_combined(name, memo, expected):
    assert ofx._describe(name, memo) == expected


def test_a_zero_rate_is_treated_as_no_rate():
    """Some banks emit <CURRATE>0</CURRATE> on domestic rows. Dividing by it
    later would raise, where the honest answer is "not a conversion"."""
    text = QFX.replace("<CURRATE>1.64321", "<CURRATE>0")
    assert ofx.transactions(text)[0]["origin_rate"] is None


def test_a_row_with_an_unreadable_date_is_reported_not_dropped():
    text = QFX.replace("<DTPOSTED>20260903", "<DTPOSTED>notadate")
    found = ofx.transactions(text)
    assert len(found) == 3
    assert found[1]["problem"]
    assert "DTPOSTED" in found[1]["problem"]


def test_something_that_is_not_ofx_is_refused():
    with pytest.raises(ofx.OfxProblem):
        ofx.transactions("Date,Description,Amount\n2026-09-02,X,-1.00\n")


def test_an_ofx_file_with_no_transactions_is_refused():
    """A statement for a period with no activity. Nothing to import, and
    saying so beats returning an empty success."""
    with pytest.raises(ofx.OfxProblem):
        ofx.transactions("<OFX><BANKMSGSRSV1></BANKMSGSRSV1></OFX>")


def test_bytes_are_accepted():
    assert len(ofx.transactions(QFX.encode("utf-8"))) == 3


# ==========================================================================
#  Importing it
# ==========================================================================

def test_the_importer_routes_an_ofx_file_without_being_told():
    sniffed = importers.sniff(QFX)
    assert sniffed["ofx"] is True
    assert sniffed["account_currency"] == "CAD"
    assert sniffed["headers"] == []


def test_the_euro_amount_comes_from_the_banks_own_rate(wallet):
    """85.94 CAD at a disclosed 1.64321 is 52.30 EUR, exactly. No regex over
    a description, and no guess."""
    conn, directory = wallet
    out = importers.preview(importers.sniff(QFX))
    rewe = next(e for e in out["rows"] if "REWE" in e["description"])
    assert rewe["amount_eur"] == -5230
    assert rewe["charged_minor"] == 8594
    assert rewe["charged_currency"] == "CAD"
    assert rewe["fitid"] == "2026090200001"


def test_a_domestic_row_on_a_canadian_account_is_refused(wallet):
    """A Tim Hortons coffee has no euro figure to recover. Refused for the
    reason that has always applied: a guessed conversion is worse than none."""
    conn, _directory = wallet
    out = importers.preview(importers.sniff(QFX))
    assert out["readable"] == 1
    refused = [p["why"] for p in out["problems"]]
    assert any("not in EUR" in why for why in refused)
    assert any("CAD" in why for why in refused)


def test_a_euro_account_imports_every_row_as_is(wallet):
    """No conversion happened, so there is nothing to record beside the euro
    figure and no FX cost to report."""
    conn, directory = wallet
    importers.load(conn, importers.preview(importers.sniff(OFX2)))
    row = ledger.transactions(conn, directory=directory)[0]
    assert row["amount_eur"] == -5230
    assert row["charged_minor"] is None
    assert row["fx"] is None


def test_the_conversion_cost_is_measured_against_the_ecb_rate(wallet):
    conn, directory = wallet
    importers.load(conn, importers.preview(importers.sniff(QFX)))
    row = ledger.transactions(conn, directory=directory)[0]
    assert row["fx"]["billed_text"] == "CA$85.94"
    assert row["fx"]["reference_text"] == "CA$83.85"
    assert row["fx"]["cost_text"] == "CA$2.09"
    assert 2.4 < row["fx"]["percent"] < 2.6


def test_the_watched_folder_accepts_a_qfx(wallet):
    """It only looked at .csv, .txt and .tsv, so the better of the two files
    CIBC offers was being ignored."""
    import sources
    conn, directory = wallet
    inbox = sources.inbox_dir(directory)
    os.makedirs(inbox, exist_ok=True)
    with open(os.path.join(inbox, "cibc.qfx"), "w", encoding="utf-8") as handle:
        handle.write(QFX)
    results = sources.scan(conn, directory)
    assert results[0]["status"] == "imported"
    assert results[0]["added"] == 1
    # Two refused, not one: the domestic coffee and the card payment. A
    # "PAYMENT THANK YOU" is you settling the card bill, which is not euro
    # spending, so declining it is right rather than a gap.
    assert results[0]["unreadable"] == 2


# ==========================================================================
#  What the bank's id buys
# ==========================================================================

def test_the_same_file_twice_adds_nothing(wallet):
    conn, directory = wallet
    importers.load(conn, importers.preview(importers.sniff(QFX)))
    again = importers.load(conn, importers.preview(importers.sniff(QFX)))
    assert again["added"] == 0
    assert again["duplicate"] == 1
    assert len(ledger.transactions(conn, directory=directory)) == 1


def test_the_bank_id_is_matched_before_the_fingerprint(wallet):
    """An exact question instead of a comparison of dates and descriptions.
    Here the description differs, so only the id can catch it."""
    conn, directory = wallet
    ledger.add(conn, "2026-09-02", "REWE SAGT DANKE 4821 BERLIN", -5230,
               fitid="2026090200001")
    again = ledger.add(conn, "2026-09-02", "COMPLETELY DIFFERENT TEXT", -1,
                       fitid="2026090200001")
    assert again[1] == "duplicate"
    assert len(ledger.transactions(conn, directory=directory)) == 1


def test_a_csv_row_then_its_ofx_row_does_not_double(wallet):
    """The case that decided the rule. The CSV row carries no id, so only the
    fingerprint can bridge them -- and the row then adopts the id, making
    every later comparison the exact one."""
    conn, directory = wallet
    ledger.add(conn, "2026-09-04", "REWE SAGT DANKE", -5230)
    second = ledger.add(conn, "2026-09-04", "REWE SAGT DANKE", -5230,
                        fitid="BRIDGED")
    assert second[1] == "duplicate"
    rows = ledger.transactions(conn, directory=directory)
    assert len(rows) == 1
    assert rows[0]["fitid"] == "BRIDGED", "the id was not adopted"


def test_an_ofx_row_then_its_csv_row_does_not_double_either(wallet):
    """The other order. The stored row keeps the plain fingerprint, so the
    id-less CSV row still collides with it."""
    conn, directory = wallet
    ledger.add(conn, "2026-09-06", "LIDL BERLIN", -2210, fitid="OFXFIRST")
    second = ledger.add(conn, "2026-09-06", "LIDL BERLIN", -2210)
    assert second[1] == "duplicate"
    assert len(ledger.transactions(conn, directory=directory)) == 1


def test_two_identical_purchases_still_need_force(wallet):
    """Stated so nobody mistakes it for an oversight.

    A new bank id does *not* override a fingerprint collision. The tempting
    rule -- "the bank says this is distinct, so insert it" -- would correctly
    separate two coffees, and would also re-open the doubling above, because
    CSV rows have no id to match. Overlapping re-imports are the normal way
    this app is used; two identical same-day purchases are not.
    """
    conn, directory = wallet
    ledger.add(conn, "2026-09-07", "CAFE NERO", -250, fitid="C1")
    assert ledger.add(conn, "2026-09-07", "CAFE NERO", -250,
                      fitid="C2")[1] == "duplicate"
    assert len(ledger.transactions(conn, directory=directory)) == 1

    assert ledger.add(conn, "2026-09-07", "CAFE NERO", -250, fitid="C3",
                      force=True)[1] == "added"
    assert len(ledger.transactions(conn, directory=directory)) == 2


def test_an_older_database_gains_the_id_column(tmp_path, monkeypatch):
    """A ledger created before the column existed. Additive, because the file
    holds the only copy of somebody's spending."""
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    import sqlite3
    path = db.db_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, "
                "spent_on TEXT, description TEXT, merchant TEXT, "
                "amount_eur INTEGER, category TEXT, source TEXT, "
                "fingerprint TEXT UNIQUE, created_at TEXT)")
    old.commit()
    old.close()

    connection = db.connect(str(tmp_path))
    try:
        columns = {row[1] for row in
                   connection.execute("PRAGMA table_info(transactions)")}
        assert "fitid" in columns
        ledger.add(connection, "2026-09-02", "WORKS", -100, fitid="Z1")
        assert ledger.transactions(connection,
                                   directory=str(tmp_path))[0]["fitid"] == "Z1"
    finally:
        connection.close()


def test_the_money_never_becomes_a_float_on_the_way_through(wallet):
    """The rate arrives as a Decimal from the file and the division happens
    in money.convert, so the euro figure is exact rather than 52.299999."""
    conn, directory = wallet
    importers.load(conn, importers.preview(importers.sniff(QFX)))
    row = ledger.transactions(conn, directory=directory)[0]
    assert isinstance(row["amount_eur"], int)
    assert row["amount_eur"] == -5230
    assert money.format(row["amount_eur"], "EUR") == "-€52.30"


# ------------------------------------------------- the remaining branches

def test_a_row_the_reader_could_not_parse_is_refused_by_the_importer():
    """ofx.transactions reports a bad row rather than dropping it, so the
    importer has to carry that reason through to the preview."""
    text = QFX.replace("<TRNAMT>-4.85", "<TRNAMT>notanamount")
    out = importers.preview(importers.sniff(text))
    assert any("amount" in p["why"].lower() or "read" in p["why"].lower()
               for p in out["problems"])


def test_a_row_with_no_payee_or_memo_is_refused():
    """A description is what makes a purchase recognisable later, and the
    ledger requires one."""
    text = QFX.replace("<NAME>TIM HORTONS 1234", "<NAME>")
    out = importers.preview(importers.sniff(text))
    assert any(p["why"] == "no description" for p in out["problems"])


def test_a_purchase_in_a_third_currency_is_refused():
    """A Swedish lunch on a Canadian card. Neither figure is euros, so there
    is nothing this app can honestly record -- the same rule that stops a
    200 SEK lunch becoming EUR 200."""
    text = QFX.replace("<CURSYM>EUR", "<CURSYM>SEK")
    out = importers.preview(importers.sniff(text))
    assert any("SEK" in p["why"] for p in out["problems"])
    assert out["readable"] == 0


def test_a_foreign_purchase_with_no_rate_disclosed_is_refused():
    """The euro figure comes from the bank's rate. Without one there is
    nothing to divide by, and inventing a rate is the thing this whole
    module exists to avoid."""
    text = QFX.replace("<CURRATE>1.64321", "<CURRATE>")
    out = importers.preview(importers.sniff(text))
    assert any("no rate" in p["why"] for p in out["problems"])


def test_a_currate_that_is_not_a_number_is_treated_as_absent():
    """Rather than raising out of the parser. A malformed rate is a row that
    cannot be converted, not a file that cannot be read."""
    text = QFX.replace("<CURRATE>1.64321", "<CURRATE>about one point six")
    assert ofx.transactions(text)[0]["origin_rate"] is None


def test_the_account_currency_reader_accepts_bytes():
    """sources hands it whatever came off disk."""
    assert ofx.account_currency(QFX.encode("utf-8")) == "CAD"
