"""Which date a figure is converted at.

This is the whole subject of the bug that made the CAD and USD headline blank
every weekend: a rule that is correct for a transaction was applied to a
total. The two have genuinely different answers, so both are pinned here
exhaustively rather than by example.

    A transaction carries a date. Converting it at a rate that did not exist
    when the money was spent is hindsight, so a future-dated purchase is
    refused and a weekend purchase looks *backwards* to the last published
    business day.

    A month-to-date total carries no date. "Today" there means "now", so the
    honest rate is the latest one published at or before now -- which on a
    Sunday is Friday's, and is never nothing.

The cases below are the real ECB calendar: no weekend rows at all, and gaps of
up to five days over Easter and Christmas.
"""
import calendar
import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budgets   # noqa: E402
import db        # noqa: E402
import fxrates   # noqa: E402
import ledger    # noqa: E402
import money     # noqa: E402

CAD = "1.6033"
USD = "1.1614"


def write_cache(directory, dates, cad=CAD, usd=USD):
    """A rate cache holding exactly `dates`, newest-first on disk."""
    path = fxrates.cache_path(str(directory))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("date,CAD,USD\n")
        for day in sorted(dates, reverse=True):
            handle.write(f"{day},{cad},{usd}\n")
    fxrates.reset()
    return path


def business_days(first, last):
    """Every weekday from `first` to `last`, which is the shape refresh()
    writes: contiguous published days, weekends absent.

    Tests that sweep a whole month need this rather than a handful of dates.
    A five-day cache leaves the start of the month before the earliest rate,
    and the lookback then finds nothing -- a state the real app never reaches,
    because refresh() always writes the full history back to 1999.
    """
    first = dt.date.fromisoformat(first)
    last = dt.date.fromisoformat(last)
    out, day = [], first
    while day <= last:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += dt.timedelta(days=1)
    return out


@pytest.fixture
def wallet(tmp_path, monkeypatch):
    """A ledger with no rates yet; each test writes the cache it needs."""
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    fxrates.reset()
    connection = db.connect(str(tmp_path))
    yield connection, tmp_path
    connection.close()
    fxrates.reset()


def spend(conn, iso, cents=10000):
    ledger.add(conn, iso, f"SHOP {iso}", -cents, category="Groceries")


# ==========================================================================
#  fxrates.newest -- what "has actually been published"
# ==========================================================================

def test_newest_is_the_latest_date_in_the_cache(wallet):
    _conn, tmp = wallet
    write_cache(tmp, ["2026-09-04", "2026-09-07", "2026-09-08"])
    assert fxrates.newest(str(tmp)) == dt.date(2026, 9, 8)


def test_newest_does_not_trust_the_file_order(wallet):
    """The writer emits newest-first, but nothing should depend on that -- a
    hand-edited or differently-sorted cache must still give the true maximum."""
    _conn, tmp = wallet
    path = fxrates.cache_path(str(tmp))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("date,CAD,USD\n")
        for day in ("2026-09-04", "2026-09-08", "2026-09-07"):   # shuffled
            handle.write(f"{day},{CAD},{USD}\n")
    fxrates.reset()
    assert fxrates.newest(str(tmp)) == dt.date(2026, 9, 8)


def test_newest_is_none_when_there_is_no_cache(wallet):
    """A fresh clone. The clamp has to cope with this rather than crash."""
    _conn, tmp = wallet
    assert fxrates.newest(str(tmp)) is None


def test_newest_is_none_when_the_cache_holds_no_usable_rows(wallet):
    _conn, tmp = wallet
    path = fxrates.cache_path(str(tmp))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("date,CAD,USD\nnot-a-date,,\n")
    fxrates.reset()
    assert fxrates.newest(str(tmp)) is None


# ==========================================================================
#  _last_day_seen -- which date a month's total belongs to
# ==========================================================================

def test_the_current_month_is_dated_today(wallet):
    assert budgets._last_day_seen("2026-09", dt.date(2026, 9, 9)) == \
        dt.date(2026, 9, 9)


def test_a_closed_month_is_dated_its_own_last_day(wallet):
    """Not today. Otherwise a finished month's figure drifts every time it is
    opened, and last month's report never matches itself."""
    assert budgets._last_day_seen("2026-02", dt.date(2026, 9, 9)) == \
        dt.date(2026, 2, 28)


@pytest.mark.parametrize("month,last", [
    ("2026-01", 31), ("2026-02", 28), ("2026-04", 30),
    ("2028-02", 29),                      # a leap February
    ("2026-12", 31),
])
def test_month_ends_are_taken_from_the_calendar_not_assumed(month, last):
    """A hardcoded 30 or 31 would silently date February's total in March."""
    seen = budgets._last_day_seen(month, dt.date(2029, 1, 1))
    assert seen.day == last
    assert seen.day == calendar.monthrange(seen.year, seen.month)[1]


def test_a_future_month_is_dated_its_last_day_and_then_clamped(wallet):
    """Nothing has been spent in it and nothing has been published for it, so
    the figure is zero at the newest known rate rather than an error."""
    conn, tmp = wallet
    write_cache(tmp, ["2026-09-08"])
    out = budgets.summary(conn, "2027-03", today=dt.date(2026, 9, 9),
                          directory=str(tmp))
    assert out["spent_eur"] == 0
    assert out["spent_cad_text"] == "CA$0.00"
    assert out["rate_date"] == "2026-09-08"


def test_a_future_month_reports_no_time_elapsed(wallet):
    """The branch of _elapsed nothing reached: a month that has not started is
    0% gone with all its days left, not 100% gone."""
    conn, _tmp = wallet
    fraction, days_left = budgets._elapsed("2027-03", dt.date(2026, 9, 9))
    assert fraction == 0.0
    assert days_left == 31
    assert budgets.summary(conn, "2027-03",
                           today=dt.date(2026, 9, 9))["elapsed"] == 0.0


# ==========================================================================
#  The clamp -- the bug itself, across the real calendar
# ==========================================================================

# Published business days around a normal week and the two long gaps.
NORMAL_WEEK = ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10",
               "2026-09-11"]                      # Mon-Fri
EASTER_2026 = ["2026-04-01", "2026-04-02", "2026-04-07"]   # 2 Apr -> 7 Apr
CHRISTMAS_2025 = ["2025-12-23", "2025-12-24", "2025-12-29"]


@pytest.mark.parametrize("viewed,expected,note", [
    ("2026-09-11", "2026-09-11", "Friday, its own rate"),
    ("2026-09-12", "2026-09-11", "Saturday -> Friday"),
    ("2026-09-13", "2026-09-11", "Sunday -> Friday"),
    ("2026-09-14", "2026-09-11", "Monday morning, before the ECB publishes"),
])
def test_a_weekend_total_converts_at_the_last_published_rate(wallet, viewed,
                                                             expected, note):
    """The bug. Every one of these showed an empty CAD and USD figure."""
    conn, tmp = wallet
    write_cache(tmp, NORMAL_WEEK)
    spend(conn, "2026-09-08")
    out = budgets.summary(conn, "2026-09", today=dt.date.fromisoformat(viewed),
                          directory=str(tmp))
    assert out["spent_cad_text"] == "CA$160.33", note
    assert out["spent_usd_text"] == "US$116.14", note
    assert out["rate_date"] == expected, note


@pytest.mark.parametrize("viewed,expected", [
    ("2026-04-03", "2026-04-02"),   # Good Friday
    ("2026-04-04", "2026-04-02"),   # Saturday
    ("2026-04-05", "2026-04-02"),   # Easter Sunday
    ("2026-04-06", "2026-04-02"),   # Easter Monday, four days back
])
def test_the_longest_gap_in_the_series_is_crossed(wallet, viewed, expected):
    """Easter 2026 runs 2 April straight to 7 April. Four consecutive days
    with no rate of their own is the worst case the real calendar contains."""
    conn, tmp = wallet
    write_cache(tmp, ["2026-04-01", "2026-04-02"])
    spend(conn, "2026-04-01")
    out = budgets.summary(conn, "2026-04", today=dt.date.fromisoformat(viewed),
                          directory=str(tmp))
    assert out["rate_date"] == expected
    assert out["spent_cad_text"] == "CA$160.33"


@pytest.mark.parametrize("viewed", ["2025-12-25", "2025-12-26", "2025-12-27",
                                    "2025-12-28"])
def test_the_christmas_gap_is_crossed(wallet, viewed):
    conn, tmp = wallet
    write_cache(tmp, ["2025-12-23", "2025-12-24"])
    spend(conn, "2025-12-23")
    out = budgets.summary(conn, "2025-12", today=dt.date.fromisoformat(viewed),
                          directory=str(tmp))
    assert out["rate_date"] == "2025-12-24"
    # Positive: summary reports spending as a magnitude, because
    # "-450 of 400" is harder to read than "450 of 400".
    assert out["spent_cad"] == 16033


def test_no_clamp_happens_when_todays_rate_exists(wallet):
    """The ordinary case must be untouched: same-day rate, no reaching back."""
    conn, tmp = wallet
    write_cache(tmp, NORMAL_WEEK)
    spend(conn, "2026-09-08")
    out = budgets.summary(conn, "2026-09", today=dt.date(2026, 9, 9),
                          directory=str(tmp))
    assert out["rate_date"] == "2026-09-09"


def test_a_closed_month_is_not_clamped_forward_to_today(wallet):
    """The clamp only ever moves the date *back*. If it moved a closed month's
    total to the newest rate, February would be converted at September's and
    would change every day."""
    conn, tmp = wallet
    write_cache(tmp, ["2026-02-27", "2026-02-28", "2026-09-08"])
    spend(conn, "2026-02-10")
    out = budgets.summary(conn, "2026-02", today=dt.date(2026, 9, 9),
                          directory=str(tmp))
    assert out["rate_date"] == "2026-02-28"


def test_a_closed_month_whose_end_was_never_published_reaches_back(wallet):
    """31 May 2026 is a Sunday, so no rate was published for it. The month's
    total still has to convert."""
    conn, tmp = wallet
    write_cache(tmp, ["2026-05-28", "2026-05-29"])          # Thu, Fri
    spend(conn, "2026-05-12")
    out = budgets.summary(conn, "2026-05", today=dt.date(2026, 9, 9),
                          directory=str(tmp))
    assert out["rate_date"] == "2026-05-29"
    assert out["spent_cad_text"] == "CA$160.33"


def test_a_badly_stale_cache_still_converts_and_says_when_from(wallet):
    """Months behind is not an error, it is a stale cache, and the figure is
    honest as long as the date is shown. Blanking it would hide the staleness
    rather than report it."""
    conn, tmp = wallet
    write_cache(tmp, ["2026-03-02"])
    spend(conn, "2026-09-08")
    out = budgets.summary(conn, "2026-09", today=dt.date(2026, 9, 9),
                          directory=str(tmp))
    assert out["spent_cad_text"] == "CA$160.33"
    assert out["rate_date"] == "2026-03-02"


def test_with_no_cache_at_all_the_figures_are_blank_not_zero(wallet):
    """A fresh clone before any refresh. Blank says "not known"; a zero would
    say "you spent nothing", which is a different and wrong claim."""
    conn, tmp = wallet
    spend(conn, "2026-09-08")
    out = budgets.summary(conn, "2026-09", today=dt.date(2026, 9, 9),
                          directory=str(tmp))
    assert out["spent_eur"] == 10000
    assert out["spent_text"] == "€100.00"
    assert out["spent_cad"] is None
    assert out["spent_cad_text"] == ""
    assert out["spent_usd"] is None
    assert out["rate_date"] == ""


def test_a_cache_of_one_day_is_enough(wallet):
    conn, tmp = wallet
    write_cache(tmp, ["2026-09-08"])
    spend(conn, "2026-09-08")
    out = budgets.summary(conn, "2026-09", today=dt.date(2026, 9, 30),
                          directory=str(tmp))
    assert out["rate_date"] == "2026-09-08"


def test_an_empty_month_still_converts_its_zero(wallet):
    """Zero euros is zero dollars, not a blank. The tiles should read
    CA$0.00 on a month with nothing in it."""
    conn, tmp = wallet
    write_cache(tmp, NORMAL_WEEK)
    out = budgets.summary(conn, "2026-09", today=dt.date(2026, 9, 13),
                          directory=str(tmp))
    assert out["spent_eur"] == 0
    assert out["spent_cad_text"] == "CA$0.00"
    assert out["spent_usd_text"] == "US$0.00"


def test_a_refund_heavy_month_converts_its_sign(wallet):
    """Spending is reported positive, so a month that netted money back is
    zero spent -- and zero converts, it does not blank."""
    conn, tmp = wallet
    write_cache(tmp, NORMAL_WEEK)
    ledger.add(conn, "2026-09-08", "ZARA REFUND", 3000, category="Shopping")
    out = budgets.summary(conn, "2026-09", today=dt.date(2026, 9, 13),
                          directory=str(tmp))
    assert out["spent_eur"] == 0
    assert out["spent_cad_text"] == "CA$0.00"


# ==========================================================================
#  The transaction path must NOT clamp -- regression guards
# ==========================================================================

def test_a_transaction_dated_after_the_newest_rate_is_still_refused(wallet):
    """The clamp is for totals only. A purchase really is dated, so converting
    it at a rate that did not exist yet is hindsight."""
    _conn, tmp = wallet
    write_cache(tmp, ["2026-09-08"])
    with pytest.raises(fxrates.RateError):
        fxrates.rate(dt.date(2026, 9, 9), "CAD", str(tmp))


def test_a_transaction_row_with_no_usable_rate_stays_blank(wallet):
    """And the row keeps its euros and carries the reason, rather than
    borrowing the clamp and inventing a figure."""
    conn, tmp = wallet
    write_cache(tmp, ["2026-09-08"])
    ledger.add(conn, "2026-09-30", "LATER PURCHASE", -5000)
    row = ledger.transactions(conn, month="2026-09",
                              directory=str(tmp))[0]
    assert row["amount_eur"] == -5000
    assert row["amount_cad"] is None
    assert row["rate_note"]


def test_a_weekend_transaction_looks_backwards_and_reports_the_lag(wallet):
    """The transaction rule, unchanged: reach back to the last business day
    and record how far. This is what the CSV's rate_lag_days column shows."""
    conn, tmp = wallet
    write_cache(tmp, NORMAL_WEEK)
    ledger.add(conn, "2026-09-13", "SUNDAY MARKET", -5000)   # Sunday
    row = ledger.transactions(conn, month="2026-09",
                              directory=str(tmp))[0]
    assert row["rate_date"] == "2026-09-11"
    assert row["rate_lag_days"] == 2
    assert row["amount_cad"] == -8017


def test_the_total_and_the_rows_can_legitimately_differ_in_rate_date(wallet):
    """Not a contradiction, and worth pinning so nobody "fixes" it.

    A purchase made on the 8th is converted at the 8th's rate for ever. The
    month total, viewed on the 13th, is converted at the newest published rate
    -- the 11th. Two different questions, two different dates, both labelled.
    """
    conn, tmp = wallet
    write_cache(tmp, NORMAL_WEEK)
    spend(conn, "2026-09-08")
    row = ledger.transactions(conn, month="2026-09", directory=str(tmp))[0]
    out = budgets.summary(conn, "2026-09", today=dt.date(2026, 9, 13),
                          directory=str(tmp))
    assert row["rate_date"] == "2026-09-08"
    assert out["rate_date"] == "2026-09-11"


def test_the_clamp_never_moves_a_date_forward(wallet):
    """The invariant, stated directly: whatever date a total converts at is on
    or before the date it belongs to, and on or before the newest published."""
    conn, tmp = wallet
    write_cache(tmp, business_days("2026-08-01", "2026-09-11"))
    spend(conn, "2026-09-08")
    published = fxrates.newest(str(tmp))
    for day in range(7, 30):
        viewed = dt.date(2026, 9, day)
        out = budgets.summary(conn, "2026-09", today=viewed,
                              directory=str(tmp))
        used = dt.date.fromisoformat(out["rate_date"])
        assert used <= viewed, viewed
        assert used <= published, viewed


def test_every_day_of_a_month_produces_a_figure(wallet):
    """The property the bug broke, swept rather than sampled: there is no day
    you can open the page on and see an empty total."""
    conn, tmp = wallet
    write_cache(tmp, business_days("2026-08-01", "2026-09-11"))
    spend(conn, "2026-09-08")
    for day in range(1, 31):
        out = budgets.summary(conn, "2026-09", today=dt.date(2026, 9, day),
                              directory=str(tmp))
        assert out["spent_cad_text"], f"blank on 2026-09-{day:02d}"
        assert out["spent_usd_text"], f"blank on 2026-09-{day:02d}"
        assert out["rate_date"], f"no rate date on 2026-09-{day:02d}"


def test_the_converted_total_matches_converting_the_euro_total(wallet):
    """The figure is the euro total converted once, not a sum of separately
    converted categories -- those differ by a cent or two and the tile is
    labelled as the euro figure's equivalent."""
    conn, tmp = wallet
    write_cache(tmp, NORMAL_WEEK)
    spend(conn, "2026-09-08", 3333)
    spend(conn, "2026-09-09", 6667)
    out = budgets.summary(conn, "2026-09", today=dt.date(2026, 9, 13),
                          directory=str(tmp))
    assert out["spent_eur"] == 10000
    assert out["spent_cad"] == money.convert(10000, CAD)
