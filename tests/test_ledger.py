"""Transactions: the duplicate rule, categorisation, and the summaries.

The duplicate tests are the important ones. Re-importing an overlapping
statement is the *normal* way this app gets used -- you export
January-to-March, then February-to-April -- and if February lands twice then
every total and every budget is wrong in a way that reads as overspending
rather than as a bug.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db        # noqa: E402
import fxrates   # noqa: E402
import ledger    # noqa: E402
import money     # noqa: E402


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A ledger in a temporary directory, with a small real rate cache."""
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    fxrates.reset()
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("date,CAD,USD\n")
        for day in ("2026-03-02", "2026-02-02", "2026-01-02"):
            handle.write(f"{day},1.6033,1.1614\n")
    connection = db.connect(str(tmp_path))
    yield connection
    connection.close()
    fxrates.reset()


# -------------------------------------------------------------- duplicates

def test_the_same_purchase_twice_is_recorded_once(conn):
    first, how = ledger.add(conn, "2026-02-02", "TESCO STORES 4823", -4550)
    assert how == "added"
    second, how = ledger.add(conn, "2026-02-02", "TESCO STORES 4823", -4550)
    assert how == "duplicate"
    assert second == first
    assert len(ledger.transactions(conn)) == 1


def test_overlapping_statements_do_not_double_a_month(conn):
    """The whole point. Two exports sharing February, imported in turn."""
    january_to_march = [
        ("2026-01-15", "SPAR", -1200),
        ("2026-02-02", "TESCO STORES 4823", -4550),
        ("2026-02-20", "DB BAHN TICKET", -3990),
    ]
    february_to_april = [
        ("2026-02-02", "TESCO STORES 4823", -4550),   # already known
        ("2026-02-20", "DB BAHN TICKET", -3990),      # already known
        ("2026-03-02", "LIDL", -2210),                # new
    ]
    for on, what, amount in january_to_march:
        ledger.add(conn, on, what, amount, source="import:q1")
    added = sum(1 for on, what, amount in february_to_april
                if ledger.add(conn, on, what, amount,
                              source="import:q2")[1] == "added")

    assert added == 1
    assert len(ledger.transactions(conn)) == 4
    february = ledger.transactions(conn, month="2026-02")
    assert money.total(t["amount_eur"] for t in february) == -8540


def test_terminal_noise_does_not_defeat_duplicate_detection(conn):
    """The same purchase written two ways by two exports.

    Reference numbers and card wording differ between date ranges, so a
    fingerprint that kept them would treat these as separate purchases.
    """
    ledger.add(conn, "2026-02-02", "CARD PURCHASE TESCO STORES 4823 DUBLIN",
               -4550)
    _id, how = ledger.add(conn, "2026-02-02", "Tesco Stores 4823  Dublin",
                          -4550)
    assert how == "duplicate"


def test_a_genuine_second_identical_purchase_can_be_forced(conn):
    """Two 2.50 coffees from one shop on one day are one purchase to the
    fingerprint. Losing a real coffee is a small, visible error; doubling a
    month is a large, invisible one -- so the default is strict and this is
    the escape hatch."""
    ledger.add(conn, "2026-02-02", "CAFE NERO", -250)
    _id, how = ledger.add(conn, "2026-02-02", "CAFE NERO", -250, force=True)
    assert how == "added"
    assert len(ledger.transactions(conn)) == 2


def test_differing_amount_or_date_is_a_different_purchase(conn):
    ledger.add(conn, "2026-02-02", "TESCO", -4550)
    assert ledger.add(conn, "2026-02-02", "TESCO", -4551)[1] == "added"
    assert ledger.add(conn, "2026-02-03", "TESCO", -4550)[1] == "added"
    assert len(ledger.transactions(conn)) == 3


# ------------------------------------------------------------------- dates

@pytest.mark.parametrize("text", ["2026-02-02", "02/02/2026", "02.02.2026",
                                  "2026/02/02", "02-02-2026", "02 Feb 2026"])
def test_the_date_formats_banks_actually_export(conn, text):
    tid, _how = ledger.add(conn, text, "TESCO", -4550)
    row = ledger.transactions(conn)[0]
    assert row["spent_on"] == "2026-02-02"
    assert row["id"] == tid


def test_an_unreadable_date_raises(conn):
    with pytest.raises(ValueError):
        ledger.add(conn, "not-a-date", "TESCO", -4550)


def test_a_transaction_needs_a_description(conn):
    with pytest.raises(ValueError):
        ledger.add(conn, "2026-02-02", "   ", -4550)


# ----------------------------------------------------------------- currency

def test_each_row_carries_cad_usd_and_the_rate_date(conn):
    ledger.add(conn, "2026-02-02", "TESCO", -5000)
    row = ledger.transactions(conn)[0]
    assert row["amount_eur"] == -5000
    assert row["amount_cad"] == -8017          # -EUR 50.00 -> -CA$80.17
    assert row["amount_usd"] == -5807
    assert row["rate_cad"] == "1.6033"
    assert row["rate_date"] == "2026-02-02"
    assert row["rate_lag_days"] == 0


def test_a_row_with_no_usable_rate_keeps_its_euros_and_says_why(conn):
    """Not zero. Zero in a money column looks like a free purchase."""
    ledger.add(conn, "2020-05-05", "OLD PURCHASE", -5000)
    row = ledger.transactions(conn, month="2020-05")[0]
    assert row["amount_eur"] == -5000
    assert row["amount_cad"] is None
    assert row["amount_cad_text"] == ""
    assert row["rate_note"]


def test_euros_are_what_is_stored_so_past_months_do_not_move(conn):
    """Converted figures are derived at read time. Storing them would freeze
    whatever rate was cached on import day."""
    ledger.add(conn, "2026-02-02", "TESCO", -5000)
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(transactions)")}
    assert "amount_eur" in columns
    assert not {c for c in columns if "cad" in c.lower() or "usd" in c.lower()}


# ------------------------------------------------------------------- rules

def test_a_rule_categorises_on_import(conn):
    ledger.add_rule(conn, "tesco", "Groceries")
    ledger.add(conn, "2026-02-02", "CARD PURCHASE TESCO STORES 4823", -4550)
    assert ledger.transactions(conn)[0]["category"] == "Groceries"


def test_no_rule_means_uncategorised_rather_than_a_guess(conn):
    ledger.add(conn, "2026-02-02", "SOMETHING NEW", -1000)
    assert ledger.transactions(conn)[0]["category"] == db.UNCATEGORISED


def test_the_more_specific_rule_wins_whatever_the_order(conn):
    """Insertion order deciding this would be surprising: "amazon fresh"
    should reach Groceries even though "amazon" was added first."""
    ledger.add_rule(conn, "amazon", "Shopping")
    ledger.add_rule(conn, "amazon fresh", "Groceries")
    assert ledger.categorise(conn, "AMAZON FRESH DUBLIN") == "Groceries"
    assert ledger.categorise(conn, "AMAZON MKTPLACE") == "Shopping"


def test_a_rule_is_case_insensitive(conn):
    ledger.add_rule(conn, "NETFLIX", "Entertainment")
    assert ledger.categorise(conn, "netflix.com monthly") == "Entertainment"


def test_adding_a_rule_fixes_the_rows_already_there(conn):
    """The reason somebody writes a rule is the charges already in the
    ledger, not the ones that have not happened yet."""
    ledger.add(conn, "2026-02-02", "NETFLIX.COM", -1399)
    ledger.add(conn, "2026-01-02", "NETFLIX.COM", -1399)
    assert ledger.transactions(conn)[0]["category"] == db.UNCATEGORISED

    ledger.add_rule(conn, "netflix", "Entertainment")
    assert ledger.apply_rules(conn) == 2
    assert all(t["category"] == "Entertainment"
               for t in ledger.transactions(conn))


def test_reapplying_rules_does_not_overwrite_a_manual_category(conn):
    """Someone who hand-corrects a category means it."""
    tid, _ = ledger.add(conn, "2026-02-02", "AMAZON", -2000)
    ledger.recategorise(conn, tid, "Health")
    ledger.add_rule(conn, "amazon", "Shopping")
    ledger.apply_rules(conn, only_uncategorised=True)
    assert ledger.transactions(conn)[0]["category"] == "Health"


def test_a_rule_can_be_replaced_and_removed(conn):
    ledger.add_rule(conn, "tesco", "Shopping")
    ledger.add_rule(conn, "tesco", "Groceries")     # same keyword, new answer
    assert len(ledger.rules(conn)) == 1
    assert ledger.categorise(conn, "TESCO") == "Groceries"
    ledger.remove_rule(conn, ledger.rules(conn)[0]["id"])
    assert ledger.categorise(conn, "TESCO") == db.UNCATEGORISED


# --------------------------------------------------------------- summaries

def test_totals_by_category_leave_income_out(conn):
    """Mixing a salary into "spent" makes the figure meaningless."""
    ledger.add(conn, "2026-02-02", "TESCO", -4550, category="Groceries")
    ledger.add(conn, "2026-02-03", "LIDL", -2210, category="Groceries")
    ledger.add(conn, "2026-02-01", "SALARY", 250000, category="Income")
    totals = ledger.totals_by_category(conn, month="2026-02")
    assert totals == {"Groceries": -6760}
    assert "Income" not in totals


def test_a_refund_nets_off_rather_than_adding(conn):
    ledger.add(conn, "2026-02-02", "ZARA", -8000, category="Shopping")
    ledger.add(conn, "2026-02-10", "ZARA REFUND", 3000, category="Shopping")
    assert ledger.totals_by_category(conn, "2026-02") == {"Shopping": -5000}


def test_months_and_categories_are_listed_for_the_pickers(conn):
    ledger.add(conn, "2026-02-02", "TESCO", -4550, category="Groceries")
    ledger.add(conn, "2026-01-02", "SPAR", -1200, category="Groceries")
    assert ledger.months(conn) == ["2026-02", "2026-01"]
    assert "Groceries" in ledger.categories(conn)
    # Defaults are always offered, so the dropdown is never empty on day one.
    assert "Transport" in ledger.categories(conn)


def test_search_matches_description_and_category(conn):
    ledger.add(conn, "2026-02-02", "TESCO STORES", -4550, category="Groceries")
    ledger.add(conn, "2026-02-03", "DB BAHN", -3990, category="Transport")
    assert len(ledger.transactions(conn, search="tesco")) == 1
    assert len(ledger.transactions(conn, search="transport")) == 1
    assert len(ledger.transactions(conn, search="nothing here")) == 0


def test_the_monthly_trend_comes_back_newest_first(conn):
    ledger.add(conn, "2026-02-02", "TESCO", -4550)
    ledger.add(conn, "2026-01-02", "SPAR", -1200)
    assert ledger.monthly_totals(conn) == [("2026-02", -4550),
                                           ("2026-01", -1200)]


# --------------------------------------------------------------- recurring

def test_a_subscription_is_spotted_across_months(conn):
    for month in ("2026-01", "2026-02", "2026-03"):
        ledger.add(conn, f"{month}-02", "NETFLIX.COM", -1399)
    found = ledger.recurring(conn)
    assert len(found) == 1
    assert found[0]["months"] == 3
    assert found[0]["typical_eur"] == -1399


def test_a_price_rise_does_not_hide_a_subscription(conn):
    """9.99 becoming 10.99 is the same subscription, and it is the one
    somebody most wants flagged."""
    for month, amount in (("2026-01", -999), ("2026-02", -999),
                          ("2026-03", -1099)):
        ledger.add(conn, f"{month}-02", "SPOTIFY", amount)
    assert [f["merchant"] for f in ledger.recurring(conn)] == ["Spotify"]


def test_two_visits_are_not_a_subscription(conn):
    ledger.add(conn, "2026-01-02", "TESCO", -4550)
    ledger.add(conn, "2026-02-02", "TESCO", -4550)
    assert ledger.recurring(conn) == []


def test_wildly_varying_amounts_are_not_a_subscription(conn):
    """A shop visited monthly for different amounts is not a subscription."""
    for month, amount in (("2026-01", -1000), ("2026-02", -9500),
                          ("2026-03", -3300)):
        ledger.add(conn, f"{month}-02", "TESCO STORES", amount)
    assert ledger.recurring(conn) == []


# ---------------------------------------------------------------- deleting

def test_a_transaction_can_be_removed_and_then_reimported(conn):
    """Deleting must free the fingerprint, or a mistaken delete becomes
    permanent and the purchase can never be added back."""
    tid, _ = ledger.add(conn, "2026-02-02", "TESCO", -4550)
    ledger.remove(conn, tid)
    assert ledger.transactions(conn) == []
    assert ledger.add(conn, "2026-02-02", "TESCO", -4550)[1] == "added"
