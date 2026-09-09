"""Monthly caps: spent, remaining, pace, and the warning states."""
import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budgets   # noqa: E402
import db        # noqa: E402
import fxrates   # noqa: E402
import ledger    # noqa: E402


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    fxrates.reset()
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("date,CAD,USD\n2026-02-28,1.6033,1.1614\n"
                     "2026-02-10,1.6033,1.1614\n"
                     "2026-02-02,1.6033,1.1614\n")
    connection = db.connect(str(tmp_path))
    yield connection
    connection.close()
    fxrates.reset()


def spend(conn, day, what, euros_cents, category):
    ledger.add(conn, f"2026-02-{day:02d}", what, -euros_cents,
               category=category)


def row_for(rows, category):
    return next(r for r in rows if r["category"] == category)


# ------------------------------------------------------------------- caps

def test_a_cap_is_set_and_read_back(conn):
    budgets.set_cap(conn, "Groceries", 40000)
    assert budgets.caps(conn) == {"Groceries": 40000}


def test_a_cap_of_zero_removes_it_rather_than_forbidding_all_spending(conn):
    """A cap of nothing would put the category permanently over."""
    budgets.set_cap(conn, "Groceries", 40000)
    budgets.set_cap(conn, "Groceries", 0)
    assert budgets.caps(conn) == {}


def test_a_negative_cap_is_refused(conn):
    with pytest.raises(ValueError):
        budgets.set_cap(conn, "Groceries", -100)


def test_setting_a_cap_twice_replaces_it(conn):
    budgets.set_cap(conn, "Groceries", 40000)
    budgets.set_cap(conn, "Groceries", 50000)
    assert budgets.caps(conn) == {"Groceries": 50000}


# ----------------------------------------------------------------- status

def test_spending_is_reported_positive_against_the_cap(conn):
    """Stored negative, shown positive. "-450 of 400" is harder to read than
    "450 of 400", and the sign says nothing once the row is labelled."""
    budgets.set_cap(conn, "Groceries", 40000)
    spend(conn, 2, "TESCO", 12000, "Groceries")
    spend(conn, 10, "LIDL", 8000, "Groceries")
    row = row_for(budgets.status(conn, "2026-02"), "Groceries")
    assert row["spent_eur"] == 20000
    assert row["remaining_eur"] == 20000
    assert row["percent"] == 50
    assert row["state"] == "fine"


def test_going_over_is_flagged(conn):
    budgets.set_cap(conn, "Groceries", 10000)
    spend(conn, 2, "TESCO", 14000, "Groceries")
    row = row_for(budgets.status(conn, "2026-02"), "Groceries")
    assert row["state"] == "over"
    assert row["remaining_eur"] == -4000
    assert row["percent"] == 140


def test_getting_close_is_flagged_before_the_cap_not_at_it(conn):
    """A warning at 100% is not a warning, it is a receipt."""
    budgets.set_cap(conn, "Groceries", 10000)
    spend(conn, 2, "TESCO", 8500, "Groceries")
    assert row_for(budgets.status(conn, "2026-02"),
                   "Groceries")["state"] == "close"


def test_spending_in_a_category_with_no_cap_is_shown_as_untracked(conn):
    """Not hidden. Money leaving an unbudgeted category is exactly what
    somebody needs to notice in order to budget it."""
    spend(conn, 2, "SOMETHING", 5000, "Shopping")
    row = row_for(budgets.status(conn, "2026-02"), "Shopping")
    assert row["state"] == "untracked"
    assert row["cap_eur"] is None
    assert row["spent_eur"] == 5000


def test_a_refund_heavy_category_is_not_overspending(conn):
    """Net positive means money came back. It must not read as over budget."""
    budgets.set_cap(conn, "Shopping", 10000)
    ledger.add(conn, "2026-02-02", "ZARA REFUND", 3000, category="Shopping")
    row = row_for(budgets.status(conn, "2026-02"), "Shopping")
    assert row["spent_eur"] == 0
    assert row["state"] == "fine"


# ------------------------------------------------------------------- pace

def test_pace_says_over_when_ahead_of_an_even_burn(conn):
    """Half the month gone, three quarters of the budget spent."""
    budgets.set_cap(conn, "Groceries", 40000)
    spend(conn, 10, "TESCO", 30000, "Groceries")
    row = row_for(budgets.status(conn, "2026-02", today=dt.date(2026, 2, 14)),
                  "Groceries")
    assert row["pace"] == "over"
    assert row["days_left"] == 14


def test_pace_says_under_when_behind_it(conn):
    budgets.set_cap(conn, "Groceries", 40000)
    spend(conn, 10, "TESCO", 5000, "Groceries")
    assert row_for(budgets.status(conn, "2026-02",
                                  today=dt.date(2026, 2, 14)),
                   "Groceries")["pace"] == "under"


def test_pace_is_withheld_in_the_first_days_of_a_month(conn):
    """Two days in, one weekly shop is "900% over pace" -- true, useless, and
    it teaches people to ignore the indicator."""
    budgets.set_cap(conn, "Groceries", 40000)
    spend(conn, 1, "TESCO", 12000, "Groceries")
    assert row_for(budgets.status(conn, "2026-02", today=dt.date(2026, 2, 2)),
                   "Groceries")["pace"] is None


def test_a_finished_month_has_no_pace_and_no_days_left(conn):
    """Pace is advice about a month you can still change."""
    budgets.set_cap(conn, "Groceries", 40000)
    spend(conn, 10, "TESCO", 30000, "Groceries")
    row = row_for(budgets.status(conn, "2026-02", today=dt.date(2026, 5, 1)),
                  "Groceries")
    assert row["pace"] is None
    assert row["days_left"] == 0


# ---------------------------------------------------------------- summary

def test_the_summary_adds_up_the_month(conn):
    budgets.set_cap(conn, "Groceries", 40000)
    budgets.set_cap(conn, "Transport", 10000)
    spend(conn, 2, "TESCO", 20000, "Groceries")
    spend(conn, 3, "DB BAHN", 4000, "Transport")

    out = budgets.summary(conn, "2026-02", today=dt.date(2026, 2, 14),
                          directory=None)
    assert out["spent_eur"] == 24000
    assert out["budgeted_eur"] == 50000
    assert out["remaining_eur"] == 26000
    assert out["spent_text"] == "€240.00"
    assert out["over"] == []


def test_the_summary_names_what_is_over_and_close(conn):
    budgets.set_cap(conn, "Groceries", 10000)
    budgets.set_cap(conn, "Transport", 10000)
    spend(conn, 2, "TESCO", 14000, "Groceries")
    spend(conn, 3, "DB BAHN", 8500, "Transport")
    out = budgets.summary(conn, "2026-02", today=dt.date(2026, 2, 14))
    assert out["over"] == ["Groceries"]
    assert out["close"] == ["Transport"]


def test_the_summary_converts_the_month_total(conn):
    spend(conn, 2, "TESCO", 10000, "Groceries")
    out = budgets.summary(conn, "2026-02", today=dt.date(2026, 2, 28))
    assert out["spent_eur"] == 10000
    assert out["spent_cad"] == 16033      # EUR 100.00 -> CA$160.33
    assert out["spent_usd"] == 11614
    assert out["spent_cad_text"] == "CA$160.33"


def test_a_closed_month_converts_at_its_own_month_end(conn):
    """Converting January's total at today's rate would make a finished
    month's figure drift every day it is looked at."""
    spend(conn, 2, "TESCO", 10000, "Groceries")
    out = budgets.summary(conn, "2026-02", today=dt.date(2026, 6, 1))
    assert out["rate_date"] == "2026-02-28"


def test_the_summary_surfaces_uncategorised_spending(conn):
    """The number that tells you the rules need attention."""
    ledger.add(conn, "2026-02-02", "MYSTERY CHARGE", -4200)
    out = budgets.summary(conn, "2026-02", today=dt.date(2026, 2, 14))
    assert out["uncategorised_eur"] == 4200


def test_a_month_with_nothing_in_it_is_zero_not_an_error(conn):
    out = budgets.summary(conn, "2026-01", today=dt.date(2026, 2, 14))
    assert out["spent_eur"] == 0
    assert out["budgeted_eur"] is None
    assert budgets.status(conn, "2026-01") == []


def test_the_month_total_converts_on_a_weekend(conn):
    """The headline figures must not go blank two days in seven.

    A transaction carries a date, so fxrates refuses to convert one dated
    later than the newest published rate -- that would be hindsight. A
    month-to-date total carries no date: "today" means "now", so it clamps to
    the latest rate published at or before now.

    Before this, viewing the page on any Saturday, Sunday, public holiday, or
    weekday morning before the ECB publishes showed an empty CAD and USD
    total, on a screen that exists to show those two numbers.
    """
    spend(conn, 2, "TESCO", 10000, "Groceries")
    # The fixture's newest rate is Saturday 2026-02-28; view it on the Sunday.
    out = budgets.summary(conn, "2026-02", today=dt.date(2026, 3, 2))
    assert out["spent_cad_text"] == "CA$160.33"
    assert out["spent_usd_text"] == "US$116.14"
    assert out["rate_date"] == "2026-02-28"


def test_a_future_dated_transaction_is_still_refused(conn):
    """The clamp is only for totals. A purchase dated after the last published
    rate stays unconvertible, because that one really is a guess."""
    import fxrates
    with pytest.raises(fxrates.RateError):
        fxrates.rate(dt.date(2027, 1, 1), "CAD")
