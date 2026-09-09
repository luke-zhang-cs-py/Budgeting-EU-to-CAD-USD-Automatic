"""Trends, subscriptions and goals -- the three things built on the ledger.

Grouped in one file because they share a fixture and a theme: each turns
stored transactions into a claim about the future or about what is unusual,
and each has a way of being confidently wrong. The tests are mostly about
the refusals -- what these modules decline to assert when there is not
enough evidence.
"""
import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budgets    # noqa: E402
import db         # noqa: E402
import goals      # noqa: E402
import ledger     # noqa: E402
import money      # noqa: E402
import trends     # noqa: E402
import upcoming   # noqa: E402

TODAY = dt.date(2026, 9, 9)
THIS = "2026-09"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    connection = db.connect(str(tmp_path))
    yield connection
    connection.close()


def spend(conn, month, amount, description, category=None, day="05"):
    return ledger.add(conn, f"{month}-{day}", description, -abs(amount),
                      category=category)


# ============================================================ subscriptions

def subs(conn, merchant, months, amount, day="11"):
    for month in months:
        spend(conn, month, amount, merchant, day=day)


def test_a_steady_subscription_that_has_landed_is_reported_as_landed(conn):
    subs(conn, "NETFLIX.COM", ("2026-06", "2026-07", "2026-08", THIS), 1399)
    row = upcoming.status(conn, THIS, TODAY)[0]
    assert row["state"] == "landed"
    assert row["expected_minor"] == 1399


def test_one_not_yet_seen_this_month_is_expected(conn):
    subs(conn, "NETFLIX.COM", ("2026-06", "2026-07", "2026-08"), 1399)
    row = upcoming.status(conn, THIS, TODAY)[0]
    assert row["state"] == "expected"
    assert row["months_since_seen"] == 1


def test_one_not_seen_for_months_is_lapsed_not_overdue(conn):
    """A cancelled subscription must stop being reported as owing money.
    Insisting forever that Audible is overdue is how a page like this stops
    being read."""
    subs(conn, "AUDIBLE UK", ("2026-03", "2026-04", "2026-05"), 799)
    row = upcoming.status(conn, THIS, TODAY)[0]
    assert row["state"] == "lapsed"


def test_the_outstanding_total_covers_only_what_has_not_landed(conn):
    subs(conn, "NETFLIX.COM", ("2026-06", "2026-07", "2026-08", THIS), 1399)
    subs(conn, "SPOTIFY AB", ("2026-06", "2026-07", "2026-08"), 999, day="14")
    out = upcoming.outstanding(conn, THIS, TODAY)
    assert out["count"] == 1
    assert out["expected_minor"] == 999


def test_a_price_rise_is_reported_with_what_it_costs_a_year(conn):
    """The case the whole module is worth having for: a few euros a month is
    invisible in a total and permanent once missed."""
    subs(conn, "SPOTIFY AB", ("2026-05", "2026-06"), 999)
    subs(conn, "SPOTIFY AB", ("2026-07", "2026-08"), 1499)
    rise = upcoming.rises(conn)[0]["rise"]
    assert rise["was_minor"] == 999
    assert rise["now_minor"] == 1499
    assert rise["by_minor"] == 500
    assert rise["yearly_minor"] == 6000
    assert rise["percent"] == pytest.approx(50.1, abs=0.2)


def test_a_price_fall_is_not_reported_as_a_rise(conn):
    subs(conn, "GYM", ("2026-05", "2026-06"), 4999)
    subs(conn, "GYM", ("2026-07", "2026-08"), 2999)
    assert upcoming.rises(conn) == []


def test_a_tiny_wobble_is_not_a_price_change(conn):
    """A foreign subscription moves a few cents with the exchange rate. That
    is not a price rise and does not deserve a line on a page."""
    subs(conn, "SOMETHING", ("2026-05", "2026-06"), 999)
    subs(conn, "SOMETHING", ("2026-07", "2026-08"), 1010)
    assert upcoming.rises(conn) == []


def test_a_single_odd_month_is_not_a_new_price(conn):
    """The false positive that got through first. Transport at 60, 62, 59 and
    then 20 is not a subscription whose price fell to 20 -- and while it was
    treated as one, its 20 was in the projected monthly cost of
    subscriptions.
    """
    for month, amount in (("2026-06", 6000), ("2026-07", 6200),
                          ("2026-08", 5900), (THIS, 2000)):
        spend(conn, month, amount, "BVG TICKET", category="Transport",
              day="07")
    assert [r["merchant"] for r in upcoming.status(conn, THIS, TODAY)] == []


def test_the_monthly_cost_excludes_what_has_lapsed(conn):
    """Counting a cancelled subscription in a running cost is how the figure
    stops being believed."""
    subs(conn, "NETFLIX.COM", ("2026-07", "2026-08", THIS), 1399)
    subs(conn, "AUDIBLE UK", ("2026-03", "2026-04", "2026-05"), 799,
         day="20")
    cost = upcoming.monthly_cost(conn)
    assert cost["count"] == 1
    assert cost["monthly_minor"] == 1399
    assert cost["yearly_minor"] == 1399 * 12
    assert cost["projected"] is True, "a year of this has not happened yet"


def test_two_months_is_not_a_subscription(conn):
    subs(conn, "TESCO", ("2026-08", THIS), 4550)
    assert upcoming.status(conn, THIS, TODAY) == []


def test_nothing_recorded_is_an_empty_report_not_an_error(conn):
    assert upcoming.status(conn, THIS, TODAY) == []
    assert upcoming.outstanding(conn, THIS, TODAY)["count"] == 0
    assert upcoming.monthly_cost(conn)["monthly_minor"] == 0
    assert upcoming.rises(conn) == []


def test_expected_sorts_above_landed_and_lapsed(conn):
    """What is still to come is the actionable part of the list."""
    subs(conn, "NETFLIX.COM", ("2026-07", "2026-08", THIS), 1399)
    subs(conn, "SPOTIFY AB", ("2026-06", "2026-07", "2026-08"), 999,
         day="14")
    subs(conn, "AUDIBLE UK", ("2026-03", "2026-04", "2026-05"), 799,
         day="20")
    assert [r["state"] for r in upcoming.status(conn, THIS, TODAY)] == [
        "expected", "landed", "lapsed"]


# =================================================================== trends

def test_a_month_is_compared_with_the_one_before(conn):
    spend(conn, "2026-07", 20000, "REWE", category="Groceries")
    spend(conn, "2026-08", 25000, "REWE", category="Groceries")
    rows = trends.by_month(conn)
    assert [r["month"] for r in rows] == ["2026-07", "2026-08"]
    assert rows[0]["change_minor"] is None, "nothing precedes the first month"
    assert rows[1]["change_minor"] == 5000
    assert rows[1]["change_text"] == "+" + money.format(5000)


def test_no_change_prints_without_a_sign(conn):
    """Written as a plain else-branch this printed "-EUR 0.00" for a month
    that spent exactly what the one before it did, which reads as a decrease
    that did not happen."""
    spend(conn, "2026-07", 20000, "REWE", category="Groceries")
    spend(conn, "2026-08", 20000, "REWE", category="Groceries")
    assert trends.by_month(conn)[1]["change_text"] == money.format(0)


def test_spending_is_reported_positive(conn):
    spend(conn, "2026-08", 20000, "REWE", category="Groceries")
    assert trends.by_month(conn)[0]["spent_minor"] == 20000


def test_income_is_not_counted_as_a_month_of_spending(conn):
    ledger.add(conn, "2026-08-01", "SALARY", 250000, category="Income")
    spend(conn, "2026-08", 20000, "REWE", category="Groceries")
    assert trends.by_month(conn)[0]["spent_minor"] == 20000


def test_a_category_above_its_own_average_is_a_mover(conn):
    for month in ("2026-06", "2026-07", "2026-08"):
        spend(conn, month, 25000, "REWE", category="Groceries")
    spend(conn, THIS, 38000, "REWE", category="Groceries")

    found = [r for r in trends.movers(conn, THIS) if
             r["category"] == "Groceries"][0]
    assert found["direction"] == "up"
    assert found["average_minor"] == 25000
    assert found["change_minor"] == 13000
    assert found["months"] == 3, "its own months, not counting this one"


def test_this_month_is_left_out_of_its_own_average(conn):
    """Including it drags the mean towards the figure being tested, which
    understates every change and understates the largest ones most."""
    for month in ("2026-06", "2026-07", "2026-08"):
        spend(conn, month, 10000, "REWE", category="Groceries")
    spend(conn, THIS, 50000, "REWE", category="Groceries")
    found = [r for r in trends.movers(conn, THIS)
             if r["category"] == "Groceries"][0]
    assert found["average_minor"] == 10000, "not (10+10+10+50)/4"


def test_a_category_that_fell_is_reported_too(conn):
    """"You spent less on transport" answers "why was this month cheap" as
    much as an overspend answers the opposite."""
    for month in ("2026-06", "2026-07", "2026-08"):
        spend(conn, month, 6000, "BVG", category="Transport", day="07")
    spend(conn, THIS, 2000, "BVG", category="Transport", day="07")
    found = [r for r in trends.movers(conn, THIS)
             if r["category"] == "Transport"][0]
    assert found["direction"] == "down"
    assert found["change_minor"] == -4000


def test_a_small_category_that_doubled_is_not_news(conn):
    """A cash floor as well as a proportional one. EUR 4 becoming EUR 8 is up
    100% and is noise."""
    for month in ("2026-06", "2026-07", "2026-08"):
        spend(conn, month, 400, "KIOSK", category="Snacks", day="03")
    spend(conn, THIS, 800, "KIOSK", category="Snacks", day="03")
    assert [r["category"] for r in trends.movers(conn, THIS)] == []


def test_a_large_category_moving_a_little_is_not_news_either(conn):
    """And a proportional floor as well as a cash one, or every big category
    is reported every month."""
    for month in ("2026-06", "2026-07", "2026-08"):
        spend(conn, month, 100000, "RENT", category="Housing", day="01")
    spend(conn, THIS, 101200, "RENT", category="Housing", day="01")
    assert [r["category"] for r in trends.movers(conn, THIS)] == []


def test_a_category_can_be_drilled_into(conn):
    """The point of a number on a dashboard is to be interrogable."""
    spend(conn, THIS, 5230, "REWE SAGT DANKE", category="Groceries")
    spend(conn, THIS, 1200, "ALDI", category="Groceries", day="07")
    found = trends.drilldown(conn, "Groceries", THIS)
    assert found["count"] == 2
    assert found["spent_minor"] == 6430


def test_the_drilldown_of_a_category_with_nothing_in_it_is_empty(conn):
    found = trends.drilldown(conn, "Groceries", THIS)
    assert found["count"] == 0
    assert found["spent_minor"] == 0


def test_a_categorys_history_covers_the_window_including_empty_months(conn):
    spend(conn, THIS, 5230, "REWE", category="Groceries")
    history = trends.category(conn, "Groceries", limit=4)
    assert len(history) == 4
    assert history[-1]["month"] == dt.date.today().strftime("%Y-%m")
    assert [h["spent_minor"] for h in history[:-1]] == [0, 0, 0]


def test_an_empty_ledger_has_no_trend(conn):
    assert trends.by_month(conn) == []
    assert trends.movers(conn, THIS) == []


def test_the_month_arithmetic_crosses_a_year(conn):
    """The off-by-one that would make January's window run back into a
    thirteenth month."""
    assert trends._months_back("2026-02", 4) == [
        "2025-11", "2025-12", "2026-01", "2026-02"]
    assert trends._months_back("2026-01", 2) == ["2025-12", "2026-01"]


# ==================================================================== goals

def test_a_goal_tracks_the_underspend_against_its_caps(conn):
    """"Saved" means exactly one thing here: the money your own caps left
    over. It is derived, so it is checkable."""
    budgets.set_cap(conn, "Groceries", 30000)
    spend(conn, THIS, 5230, "REWE", category="Groceries")
    gid = goals.add(conn, "Flight home", 60000, due_on="2026-12-20")

    figure = goals.contributed(conn, THIS, TODAY)
    assert figure["allowed_minor"] == 30000
    assert figure["spent_minor"] == 5230
    assert figure["saved_minor"] == 24770

    progress = goals.progress(conn, THIS, TODAY)[0]
    assert progress["id"] == gid
    assert progress["this_month_minor"] == 24770
    assert progress["percent"] == pytest.approx(41.3, abs=0.1)


def test_an_overspent_month_contributes_nothing_rather_than_a_negative(conn):
    """A goal has no balance to withdraw from, so "saved: -EUR 90" would
    imply one."""
    budgets.set_cap(conn, "Groceries", 5000)
    spend(conn, THIS, 14000, "REWE", category="Groceries")
    figure = goals.contributed(conn, THIS, TODAY)
    assert figure["saved_minor"] == 0
    assert figure["overspent_minor"] == 9000


def test_with_no_caps_there_is_nothing_to_have_saved(conn):
    """Inventing a figure from zero caps would make every month look like a
    total loss."""
    goals.add(conn, "Flight home", 60000)
    assert goals.contributed(conn, THIS, TODAY) is None
    assert goals.progress(conn, THIS, TODAY)[0]["this_month_minor"] == 0


def test_the_month_fills_the_nearest_deadline_first(conn):
    """Splitting it evenly would show four goals each a quarter met and none
    of them reachable, which tells you nothing about what to do."""
    budgets.set_cap(conn, "Groceries", 30000)
    goals.add(conn, "Soon", 10000, due_on="2026-10-01")
    goals.add(conn, "Later", 50000, due_on="2027-06-01")
    got = {g["name"]: g["this_month_minor"]
           for g in goals.progress(conn, THIS, TODAY)}
    assert got["Soon"] == 10000, "filled to its target and no further"
    assert got["Later"] == 20000, "the remainder"


def test_a_goal_in_another_currency_says_it_cannot_be_funded_from_euros(conn):
    """Shown as unfundable rather than as zero progress, which would read as
    "you saved nothing" instead of "this is measured in dollars"."""
    budgets.set_cap(conn, "Groceries", 30000)
    goals.add(conn, "Canadian rent", 200000, currency="CAD")
    row = goals.progress(conn, THIS, TODAY)[0]
    assert row["fundable"] is False
    assert row["this_month_minor"] == 0


def test_the_monthly_amount_needed_to_hit_a_deadline(conn):
    goals.add(conn, "Flight home", 60000, due_on="2026-12-20")
    row = goals.progress(conn, THIS, TODAY)[0]
    assert row["days_left"] == 102
    assert row["monthly_needed_minor"] == 20000


def test_a_goal_with_no_deadline_needs_no_monthly_amount(conn):
    goals.add(conn, "Someday", 60000)
    row = goals.progress(conn, THIS, TODAY)[0]
    assert row["days_left"] is None
    assert row["monthly_needed_minor"] is None


def test_goals_are_ordered_by_deadline_with_undated_last(conn):
    goals.add(conn, "Someday", 10000)
    goals.add(conn, "Later", 10000, due_on="2027-01-01")
    goals.add(conn, "Soon", 10000, due_on="2026-10-01")
    assert [g["name"] for g in goals.every(conn)] == ["Soon", "Later",
                                                      "Someday"]


@pytest.mark.parametrize("name", ["", "   ", None])
def test_a_goal_needs_a_name(conn, name):
    with pytest.raises(goals.GoalError):
        goals.add(conn, name, 60000)


def test_a_target_too_small_to_track_is_refused(conn):
    with pytest.raises(goals.GoalError):
        goals.add(conn, "Coffee", 50)


def test_an_unreadable_target_is_refused(conn):
    with pytest.raises(goals.GoalError):
        goals.add(conn, "Thing", "not a number")


def test_a_mistyped_deadline_is_refused_rather_than_dropped(conn):
    """Quietly discarding it is worse than refusing: the goal looks saved and
    the deadline is simply gone."""
    with pytest.raises(goals.GoalError):
        goals.add(conn, "Flight", 60000, due_on="20th of December")


def test_two_goals_cannot_share_a_name(conn):
    goals.add(conn, "Flight home", 60000)
    with pytest.raises(goals.GoalError):
        goals.add(conn, "Flight home", 70000)


def test_a_goal_can_be_changed_and_removed(conn):
    gid = goals.add(conn, "Flight home", 60000)
    assert goals.update(conn, gid, target_minor=70000, name="Flight")
    assert goals.get(conn, gid)["target_minor"] == 70000
    assert goals.update(conn, gid) is False
    assert goals.remove(conn, gid)
    assert goals.get(conn, gid) is None
    assert goals.remove(conn, gid) is False


def test_changing_a_goal_that_is_not_there_is_an_error(conn):
    with pytest.raises(goals.GoalError):
        goals.update(conn, 999, name="x")


# ------------------------------------------------- the remaining corners

def test_a_goal_in_a_currency_this_app_does_not_know_is_refused(conn):
    with pytest.raises(goals.GoalError):
        goals.add(conn, "Yen fund", 60000, currency="JPY")


def test_a_deadline_can_be_given_as_a_date_object(conn):
    gid = goals.add(conn, "Flight", 60000, due_on=dt.date(2026, 12, 20))
    assert goals.get(conn, gid)["due_on"] == "2026-12-20"


def test_a_goals_name_target_and_deadline_can_each_be_corrected(conn):
    gid = goals.add(conn, "Flight", 60000)
    with pytest.raises(goals.GoalError):
        goals.update(conn, gid, name="  ")
    with pytest.raises(goals.GoalError):
        goals.update(conn, gid, target_minor=1)
    goals.update(conn, gid, due_on="2026-12-20")
    assert goals.get(conn, gid)["due_on"] == "2026-12-20"


def test_the_contribution_defaults_to_the_current_month(conn):
    budgets.set_cap(conn, "Groceries", 30000)
    figure = goals.contributed(conn)
    assert figure["month"] == dt.date.today().strftime("%Y-%m")


def test_a_category_with_only_one_prior_month_is_not_a_mover(conn):
    """One month of history is not an average to be unusual against."""
    spend(conn, "2026-08", 10000, "NEW SHOP", category="Shopping")
    spend(conn, THIS, 90000, "NEW SHOP", category="Shopping")
    assert [r["category"] for r in trends.movers(conn, THIS)] == []


def test_a_steady_price_over_no_months_at_all_is_not_steady(conn):
    """A guard on ledger._steady. _price_shape only ever hands it slices of
    at least MIN_AT_EACH_LEVEL, so this is unreachable from the app -- but an
    empty list would divide by zero rather than answer, which is a worse
    failure than the False it gives now."""
    assert ledger._steady([], 200) is False
