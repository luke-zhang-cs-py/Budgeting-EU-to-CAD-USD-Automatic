"""Cards, their fees, and what each has cost.

The theme running through these: **a balance must say how it was arrived at.**
A card that bills Canadian dollars has an exact figure for anything imported
from a statement and only an estimate for a euro purchase typed by hand, and
blending the two into one number that looks exact is the failure this module
is shaped to avoid.
"""
import datetime as dt
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cards     # noqa: E402
import db        # noqa: E402
import fxcost    # noqa: E402
import fxrates   # noqa: E402
import ledger    # noqa: E402
import money     # noqa: E402

# Plausible ECB figures for the days used below, so the arithmetic in the
# assertions is checkable rather than invented.
#
# The header is lowercase and the columns are in money.TARGETS order, because
# that is what fxrates.load reads. Written as "Date,USD,CAD" this parsed to
# nothing at all -- load skips a row whose "date" key is missing -- and every
# conversion here failed with "no rate cache" rather than with a wrong number.
RATES = ("date,CAD,USD\n"
         "2026-09-08,1.6350,1.1650\n"
         "2026-09-07,1.6340,1.1640\n")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    fxrates.reset()
    (tmp_path / fxrates.CACHE_NAME).write_text(RATES, encoding="utf-8")
    connection = db.connect(str(tmp_path))
    yield connection
    connection.close()
    fxrates.reset()


def visa(conn, **kwargs):
    options = {"name": "CIBC Dividend Visa", "currency": "CAD",
               "mask": "••4417"}
    options.update(kwargs)
    return cards.add(conn, **options)


# --------------------------------------------------------------- the basics

def test_a_card_is_created_with_the_published_fee_by_default(conn):
    card = cards.get(conn, visa(conn))
    assert card["currency"] == "CAD"
    assert card["fee_bp"] == cards.DEFAULT_FEE_BP == 250
    assert card["fee_percent"] == 2.5
    assert card["label"] == "CIBC Dividend Visa ..4417"


def test_the_mask_keeps_only_the_last_four_digits(conn):
    """"••4417" and "xxxx 4417" are the same card. Storing the decoration
    would stop by_mask matching a screenshot that decorated it differently."""
    for given in ("••4417", "xxxx 4417", "4417", "**** **** **** 4417"):
        cid = cards.add(conn, f"card {given}", "CAD", mask=given)
        assert cards.get(conn, cid)["mask"] == "4417"


def test_the_fee_is_basis_points_so_no_float_reaches_an_amount(conn):
    """250 not 0.025. A fee held as a float puts one in the middle of a
    figure somebody is about to be charged."""
    card = cards.get(conn, visa(conn, fee_bp=239))
    assert isinstance(card["fee_bp"], int)
    assert card["fee_bp"] == 239


@pytest.mark.parametrize("fee,why", [
    (-1, "negative"),
    (2500, "25% -- 2.5 written as 2500 rather than 250"),
    (10001, "absurd"),
])
def test_an_implausible_fee_is_refused(conn, fee, why):
    """The typo this guards: 2500 meant as 2.5% silently makes every estimate
    ten times too expensive, and nothing else would notice."""
    with pytest.raises(cards.CardError):
        visa(conn, fee_bp=fee)


def test_a_card_with_no_foreign_fee_is_allowed(conn):
    """A euro account charges nothing to spend euros, and zero is a real
    answer rather than a missing one."""
    assert cards.get(conn, cards.add(conn, "N26", "EUR", fee_bp=0))[
        "fee_bp"] == 0


@pytest.mark.parametrize("name", ["", "   ", None])
def test_a_card_needs_a_name(conn, name):
    with pytest.raises(cards.CardError):
        cards.add(conn, name, "CAD")


def test_an_unknown_currency_is_refused(conn):
    with pytest.raises(cards.CardError):
        cards.add(conn, "Yen card", "JPY")


def test_two_cards_cannot_share_a_name(conn):
    visa(conn)
    with pytest.raises(cards.CardError):
        visa(conn)


# ------------------------------------------------------------ finding a card

def test_a_screenshots_mask_finds_the_card(conn):
    cid = visa(conn)
    assert cards.by_mask(conn, "4417")["id"] == cid
    assert cards.by_mask(conn, "••4417")["id"] == cid


def test_two_cards_with_the_same_mask_match_neither(conn):
    """Attributing a purchase to the wrong card puts the wrong fee on it, so
    an ambiguous match is no match."""
    cards.add(conn, "One", "CAD", mask="4417")
    cards.add(conn, "Two", "USD", mask="4417")
    assert cards.by_mask(conn, "4417") is None


def test_an_archived_card_is_not_matched(conn):
    cid = visa(conn)
    cards.archive(conn, cid)
    assert cards.by_mask(conn, "4417") is None


@pytest.mark.parametrize("mask", ["", None, "44", "notdigits"])
def test_an_unusable_mask_matches_nothing(conn, mask):
    visa(conn)
    assert cards.by_mask(conn, mask) is None


# ------------------------------------------------------------- changing them

def test_a_card_can_be_corrected(conn):
    cid = visa(conn)
    assert cards.update(conn, cid, fee_bp=239, name="CIBC Visa")
    card = cards.get(conn, cid)
    assert card["fee_bp"] == 239
    assert card["name"] == "CIBC Visa"


def test_updating_nothing_changes_nothing(conn):
    assert cards.update(conn, visa(conn)) is False


def test_an_archived_card_keeps_its_history(conn):
    """A closed card is still where last year's money went, so it is hidden
    rather than deleted."""
    cid = visa(conn)
    tid, _ = ledger.add(conn, "2026-09-08", "REWE", -5230)
    cards.attribute(conn, tid, cid)
    cards.archive(conn, cid)

    assert [c["id"] for c in cards.every(conn)] == []
    assert [c["id"] for c in cards.every(conn, include_archived=True)] == [cid]
    assert conn.execute("SELECT card_id FROM transactions WHERE id = ?",
                        (tid,)).fetchone()["card_id"] == cid


def test_deleting_a_card_detaches_its_purchases_rather_than_losing_them(conn):
    """Done explicitly, not left to ON DELETE SET NULL: a database migrated
    from before cards existed has a plain integer column with no foreign key,
    because SQLite cannot add one with ALTER."""
    cid = visa(conn)
    tid, _ = ledger.add(conn, "2026-09-08", "REWE", -5230)
    cards.attribute(conn, tid, cid)

    cards.remove(conn, cid)
    row = conn.execute("SELECT * FROM transactions WHERE id = ?",
                       (tid,)).fetchone()
    assert row is not None, "the purchase was deleted with the card"
    assert row["card_id"] is None


def test_acting_on_a_card_that_is_not_there_is_an_error(conn):
    for call in (lambda: cards.update(conn, 999, name="x"),
                 lambda: cards.archive(conn, 999),
                 lambda: cards.remove(conn, 999),
                 lambda: cards.attribute(conn, 1, 999)):
        with pytest.raises(cards.CardError):
            call()


def test_a_purchase_can_be_detached_from_every_card(conn):
    cid = visa(conn)
    tid, _ = ledger.add(conn, "2026-09-08", "REWE", -5230)
    cards.attribute(conn, tid, cid)
    assert cards.attribute(conn, tid, None)
    assert conn.execute("SELECT card_id FROM transactions WHERE id = ?",
                        (tid,)).fetchone()["card_id"] is None


# ------------------------------------------------------------------ balances

def test_a_statement_figure_is_exact_and_a_typed_one_is_an_estimate(conn):
    """The distinction the whole module exists for. One purchase came from a
    statement that said what the card billed; the other was typed in euros
    and has to be converted, which is an estimate. They are counted
    separately so a reader is never shown an estimate dressed as a fact.
    """
    cid = visa(conn)
    exact, _ = ledger.add(conn, "2026-09-08", "REWE FROM STATEMENT", -5230,
                          charged_minor=8594, charged_currency="CAD")
    typed, _ = ledger.add(conn, "2026-09-08", "CASH COFFEE", -350)
    for tid in (exact, typed):
        cards.attribute(conn, tid, cid)

    found = cards.balances(conn, month="2026-09")[0]
    assert found["exact_rows"] == 1
    assert found["exact_minor"] == 8594
    assert found["estimated_rows"] == 1
    # 3.50 EUR at the 2026-09-08 rate of 1.6350 is CA$5.72.
    assert found["estimated_minor"] == money.convert(350, Decimal("1.6350"))
    assert found["estimated_minor"] > 0, "spending is reported positive"
    assert found["spent_minor"] == found["exact_minor"] + \
        found["estimated_minor"]


def test_a_euro_card_needs_no_conversion_so_everything_is_exact(conn):
    cid = cards.add(conn, "N26", "EUR", fee_bp=0)
    tid, _ = ledger.add(conn, "2026-09-08", "REWE", -5230)
    cards.attribute(conn, tid, cid)
    found = [c for c in cards.balances(conn) if c["id"] == cid][0]
    assert found["exact_rows"] == 1
    assert found["estimated_rows"] == 0
    assert found["spent_minor"] == 5230


def test_a_row_that_cannot_be_converted_is_counted_not_dropped(conn):
    """A purchase dated before the rate history is unconvertible. Silently
    omitting it would make the balance quietly too low; it is reported as
    unconvertible instead."""
    cid = visa(conn)
    tid, _ = ledger.add(conn, "1998-01-02", "ANCIENT", -1000)
    cards.attribute(conn, tid, cid)
    found = cards.balances(conn)[0]
    assert found["unconvertible_rows"] == 1
    assert found["rows"] == 1
    assert found["spent_minor"] == 0


def test_a_refund_nets_off_rather_than_adding_to_the_spend(conn):
    """The bug the sign handling exists for.

    charged_minor is stored as a magnitude by the importers -- abs(amount) --
    so it carries no direction, while amount_eur is signed. Taken at face
    value an exact row contributed +8594 and an estimated one -572, so the
    balance summed a purchase and a purchase as though one were a refund.

    Here the refund really is one, and it has to reduce the total.
    """
    cid = visa(conn)
    bought, _ = ledger.add(conn, "2026-09-08", "REWE PURCHASE", -5230,
                           charged_minor=8594, charged_currency="CAD")
    refund, _ = ledger.add(conn, "2026-09-08", "REWE REFUND", 5230,
                           charged_minor=8594, charged_currency="CAD")
    for tid in (bought, refund):
        cards.attribute(conn, tid, cid)

    found = cards.balances(conn, month="2026-09")[0]
    assert found["exact_rows"] == 2
    assert found["exact_minor"] == 0, "a purchase and its refund cancel"
    assert found["spent_minor"] == 0


def test_a_euro_refund_nets_off_too(conn):
    cid = cards.add(conn, "N26", "EUR", fee_bp=0)
    for amount in (-5230, 5230):
        tid, _ = ledger.add(conn, "2026-09-08", f"REWE {amount}", amount)
        cards.attribute(conn, tid, cid)
    found = [c for c in cards.balances(conn) if c["id"] == cid][0]
    assert found["spent_minor"] == 0


def test_the_opening_balance_is_added(conn):
    visa(conn, opening_minor=-12000)
    found = cards.balances(conn)[0]
    assert found["balance_minor"] == -12000
    assert found["balance_text"] == money.format(-12000, "CAD")


def test_a_card_with_nothing_on_it_reports_zero_not_an_error(conn):
    visa(conn)
    found = cards.balances(conn)[0]
    assert found["rows"] == 0
    assert found["spent_minor"] == 0


def test_income_is_left_out_of_a_card_balance(conn):
    """Same reason the ledger leaves it out of spending: a salary would swamp
    the figure and stop it meaning "what this card has cost"."""
    cid = visa(conn)
    tid, _ = ledger.add(conn, "2026-09-08", "SALARY", 250000,
                        category="Income")
    cards.attribute(conn, tid, cid)
    assert cards.balances(conn)[0]["rows"] == 0


def test_a_month_narrows_the_balance(conn):
    cid = visa(conn)
    for day in ("2026-09-08", "2026-08-08"):
        tid, _ = ledger.add(conn, day, f"REWE {day}", -1000,
                            charged_minor=1650, charged_currency="CAD")
        cards.attribute(conn, tid, cid)
    assert cards.balances(conn, month="2026-09")[0]["rows"] == 1
    assert cards.balances(conn)[0]["rows"] == 2


def test_archived_cards_still_appear_in_balances(conn):
    """Their history is real, and a total that silently excluded a closed
    card would not add up to what was spent."""
    cid = visa(conn)
    cards.archive(conn, cid)
    assert [c["id"] for c in cards.balances(conn)] == [cid]


# ---------------------------------------------- the fee, measured not told

def test_the_real_markup_is_measured_from_the_cards_own_rows(conn):
    """The published 2.5% is what CIBC says; this is what the rows say. The
    two differ because Visa converts at its own rate rather than the ECB
    reference this compares against -- which is the finding worth surfacing,
    not a discrepancy to hide.
    """
    cid = visa(conn)
    # 52.30 EUR billed as CA$85.94: the exact figures off a real statement.
    for n in range(3):
        tid, _ = ledger.add(conn, "2026-09-08", f"REWE {n}", -5230,
                            charged_minor=8594, charged_currency="CAD")
        cards.attribute(conn, tid, cid)

    found = cards.measured_fee(conn, cid)
    assert found["rows"] == 3
    # ECB 1.6350 gives CA$85.51; the card billed CA$85.94.
    assert found["published_bp"] == 250
    assert 0 < found["fee_bp"] < cards.MAX_FEE_BP
    assert found["percent"] == pytest.approx(0.5, abs=0.6)


def test_too_few_rows_is_not_a_measurement(conn):
    """An average over two purchases is not a measurement, and returning the
    published figure dressed as an observation would be worse than saying
    nothing."""
    cid = visa(conn)
    tid, _ = ledger.add(conn, "2026-09-08", "REWE", -5230,
                        charged_minor=8594, charged_currency="CAD")
    cards.attribute(conn, tid, cid)
    assert cards.measured_fee(conn, cid) is None


def test_rows_with_no_billed_figure_cannot_be_measured(conn):
    cid = visa(conn)
    for n in range(4):
        tid, _ = ledger.add(conn, "2026-09-08", f"CASH {n}", -1000)
        cards.attribute(conn, tid, cid)
    assert cards.measured_fee(conn, cid) is None


def test_measuring_a_card_that_is_not_there_is_an_error(conn):
    with pytest.raises(cards.CardError):
        cards.measured_fee(conn, 999)


# ------------------------------------------------------------ the estimate

def test_the_estimate_adds_the_fee_to_the_converted_amount(conn):
    """Convert, then take the fee on the Canadian figure -- which is how CIBC
    states it and how the measured statements bear out. The other order
    agrees on a coffee and drifts on a flight."""
    out = fxcost.estimate(5230, Decimal("1.6043"), 250)
    assert out["converted_minor"] == money.convert(5230, Decimal("1.6043"))
    assert out["fee_minor"] == round(out["converted_minor"] * 250 / 10000)
    assert out["total_minor"] == out["converted_minor"] + out["fee_minor"]


def test_the_estimate_reproduces_a_real_statement_line(conn):
    """The self-validating case. A CIBC statement disclosed 52.30 EUR at its
    own rate of 1.64321 and billed CA$85.94; converting at that rate with no
    fee added has to give exactly that, or the arithmetic is wrong."""
    out = fxcost.estimate(5230, "1.64321", 0)
    assert out["converted_minor"] == 8594
    assert out["fee_minor"] == 0


def test_the_effective_rate_is_what_makes_the_fee_real(conn):
    """The number a reader can hold against the mid-market rate they looked
    up: 1.6043 becomes about 1.6444 once 2.5% is on it."""
    out = fxcost.estimate(5230, Decimal("1.6043"), 250)
    assert Decimal(out["effective_rate"]) > Decimal("1.6043")
    assert Decimal(out["effective_rate"]) == pytest.approx(
        Decimal("1.6444"), abs=Decimal("0.001"))


def test_a_zero_fee_card_pays_only_the_conversion(conn):
    out = fxcost.estimate(5230, Decimal("1.6043"), 0)
    assert out["fee_minor"] == 0
    assert out["total_minor"] == out["converted_minor"]


def test_a_negative_fee_is_refused(conn):
    with pytest.raises(money.MoneyError):
        fxcost.estimate(5230, Decimal("1.6043"), -250)


def test_estimating_without_a_rate_raises(conn):
    """A caller with no rate should be telling the reader "not right now",
    not asking for an estimate and getting a confident zero."""
    with pytest.raises(money.MoneyError):
        fxcost.estimate(5230, None, 250)


def test_the_published_fee_is_named_once(conn):
    """It appears in the schema default, in cards, and in fxcost. One of them
    has to be the source, or they drift and the app disagrees with itself
    about what CIBC charges."""
    assert cards.DEFAULT_FEE_BP is fxcost.TYPICAL_CARD_FEE_BP
    assert fxcost.TYPICAL_CARD_FEE_BP == int(
        fxcost.TYPICAL_CARD_FEE * fxcost.BASIS_POINTS)
    schema_default = [line for line in db.SCHEMA.splitlines()
                      if "fee_bp" in line and "DEFAULT" in line]
    assert schema_default, "the schema no longer defaults the fee"
    assert str(fxcost.TYPICAL_CARD_FEE_BP) in schema_default[0]


def test_the_dates_used_here_are_really_in_the_rate_cache(conn):
    """A guard on the fixture rather than the code. If the cache above stops
    covering these days, the conversions silently fall back to an earlier
    business day and the assertions above start checking arithmetic against
    a rate they did not intend."""
    assert fxrates.rate(dt.date(2026, 9, 8), "CAD")[1] == dt.date(2026, 9, 8)


# ------------------------------------------------- the correction branches

def test_a_correction_can_blank_neither_the_name_nor_the_fee(conn):
    cid = visa(conn)
    with pytest.raises(cards.CardError):
        cards.update(conn, cid, name="   ")
    with pytest.raises(cards.CardError):
        cards.update(conn, cid, fee_bp=9999)


def test_the_mask_and_the_opening_balance_can_be_corrected(conn):
    cid = visa(conn)
    cards.update(conn, cid, mask="xxxx 9911", opening_minor=-5000)
    card = cards.get(conn, cid)
    assert card["mask"] == "9911"
    assert card["opening_minor"] == -5000


def test_renaming_a_card_onto_another_name_is_refused(conn):
    visa(conn)
    other = cards.add(conn, "N26", "EUR")
    with pytest.raises(cards.CardError):
        cards.update(conn, other, name="CIBC Dividend Visa")


def test_a_row_with_an_unreadable_date_cannot_be_converted(conn):
    """spent_on is written by ledger.add and is always ISO, so this is a
    guard rather than a path the app takes. It is tested because the failure
    it prevents is a TypeError comparing a str to a date, which is not a
    RateError and so escapes the handler below it.

    The row deliberately has no billed figure: with one it takes the exact
    path and never reaches a conversion at all.
    """
    cid = visa(conn)
    tid, _ = ledger.add(conn, "2026-09-08", "CASH", -5230)
    cards.attribute(conn, tid, cid)
    conn.execute("UPDATE transactions SET spent_on = 'not a date' "
                 "WHERE id = ?", (tid,))
    conn.commit()

    found = cards.balances(conn)[0]
    assert found["unconvertible_rows"] == 1
    assert found["spent_minor"] == 0


def test_a_billed_row_with_an_unreadable_date_cannot_be_measured(conn):
    """The same guard on the measuring path, which needs a billed figure to
    have anything to compare."""
    cid = visa(conn)
    for n in range(4):
        tid, _ = ledger.add(conn, "2026-09-08", f"REWE {n}", -5230 - n,
                            charged_minor=8594, charged_currency="CAD")
        cards.attribute(conn, tid, cid)
        conn.execute("UPDATE transactions SET spent_on = 'not a date' "
                     "WHERE id = ?", (tid,))
    conn.commit()
    assert cards.measured_fee(conn, cid) is None


def test_a_purchase_older_than_the_rate_history_cannot_be_measured(conn):
    cid = visa(conn)
    for n in range(4):
        tid, _ = ledger.add(conn, "1998-01-0%d" % (n + 1), f"ANCIENT {n}",
                            -5230, charged_minor=8594,
                            charged_currency="CAD")
        cards.attribute(conn, tid, cid)
    assert cards.measured_fee(conn, cid) is None
