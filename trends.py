"""
trends.py
---------
Whether this month is unusual, and which category made it so.

Everything here is built on ledger's existing figures -- `monthly_totals` and
`totals_by_category` -- rather than on new SQL. That is deliberate: those two
functions already encode what counts as spending in this app (refunds net
off, Income is excluded), and a second set of queries here would be free to
answer the same question differently. Twelve small queries against a local
SQLite file cost nothing worth optimising away for the privilege of being
able to disagree with the ledger.

The comparison offered is **against your own average**, not against a budget.
A budget says what you meant to do and `budgets.py` already reports on it.
This answers a different question -- is this month like my other months --
which is the one that catches a subscription you forgot about and a category
that has been creeping up for a quarter.

Everything is reported as a *change*, with the figures it came from beside
it. "Groceries up 31%" on its own is unreadable: up from what, over how many
months, and is one of those months the fortnight you were away.
"""
import datetime as dt

import ledger
import money

# How much history a comparison uses. A year is long enough for a seasonal
# category to average out and short enough that a habit abandoned two years
# ago is not still dragging on the mean.
WINDOW_MONTHS = 12

# A month needs this many others to compare against before "unusual" means
# anything. With one prior month every change is 100% of the evidence.
ENOUGH_MONTHS = 3

# Below this a change is noise -- a category averaging EUR 4 that came to
# EUR 8 is up 100% and is not news.
MATTERS_MINOR = 1000

# And a proportional floor, so a large category moving a few euros does not
# get reported either.
MATTERS_SHARE = 0.15


def by_month(connection, limit=WINDOW_MONTHS):
    """[{month, spent, change against the month before, against average}].

    Oldest first, because a trend is read left to right. Spending is reported
    positive for the same reason budgets does it: a line reading "-450, down
    from -510" takes a moment to work out and "450, down from 510" does not.
    """
    totals = ledger.monthly_totals(connection, limit=limit)
    rows = [(m, max(0, -cents)) for m, cents in reversed(totals)]
    if not rows:
        return []

    average = sum(spent for _m, spent in rows) / len(rows)
    out = []
    for index, (month, spent) in enumerate(rows):
        previous = rows[index - 1][1] if index else None
        out.append({
            "month": month,
            "spent_minor": spent,
            "spent_text": money.format(spent),
            "previous_minor": previous,
            "change_minor": (spent - previous) if previous is not None
            else None,
            "change_text": (_signed(spent - previous)
                            if previous is not None else ""),
            "change_share": (round((spent - previous) / previous, 4)
                             if previous else None),
            "average_minor": int(round(average)),
            "average_text": money.format(int(round(average))),
            "against_average": round(spent - average),
            "comparable": len(rows) >= ENOUGH_MONTHS,
        })
    return out


def movers(connection, month=None, limit=WINDOW_MONTHS):
    """Categories furthest from their own average this month, biggest first.

    Both directions in one list, because "you spent less on transport" is as
    much of an answer to "why was this month cheap" as an overspend is to why
    it was expensive.

    A category is only reported when the change clears both a cash floor and
    a proportional one. Either test alone produces noise: the cash floor
    alone reports a large category moving 2%, and the share floor alone
    reports a EUR 3 category that doubled.
    """
    month = month or _this_month()
    history = _history(connection, month, limit)
    if not history:
        return []

    this = ledger.totals_by_category(connection, month)
    out = []
    for category, series in history.items():
        if len(series) < ENOUGH_MONTHS - 1:
            continue
        average = sum(series) / len(series)
        spent = max(0, -this.get(category, 0))
        change = spent - average
        share = (change / average) if average else None

        if abs(change) < MATTERS_MINOR:
            continue
        if share is None or abs(share) < MATTERS_SHARE:
            continue

        out.append({
            "category": category,
            "spent_minor": spent,
            "spent_text": money.format(spent),
            "average_minor": int(round(average)),
            "average_text": money.format(int(round(average))),
            "months": len(series),
            "change_minor": int(round(change)),
            "change_text": _signed(int(round(change))),
            "change_share": round(share, 4),
            "percent": int(round(share * 100)),
            "direction": "up" if change > 0 else "down",
        })
    return sorted(out, key=lambda row: -abs(row["change_minor"]))[:limit]


def category(connection, name, limit=WINDOW_MONTHS):
    """One category month by month, oldest first -- the drilldown's chart."""
    month = _this_month()
    months = _months_back(month, limit)
    out = []
    for each in months:
        spent = max(0, -ledger.totals_by_category(connection, each).get(name, 0))
        out.append({"month": each, "spent_minor": spent,
                    "spent_text": money.format(spent)})
    return out


def drilldown(connection, name, month=None):
    """The transactions behind one category's figure for one month.

    The point of a number on a dashboard is to be interrogable. Reuses
    ledger.transactions so the rows are the same shape, and converted the
    same way, as everywhere else they are shown.
    """
    month = month or _this_month()
    rows = ledger.transactions(connection, month=month, category=name)
    spent = max(0, -sum(row["amount_eur"] for row in rows))
    return {
        "category": name,
        "month": month,
        "rows": rows,
        "count": len(rows),
        "spent_minor": spent,
        "spent_text": money.format(spent),
    }


def _history(connection, month, limit):
    """{category: [spent per earlier month]} -- this month excluded.

    This month is left out of its own average on purpose. Including it drags
    the mean towards the figure being tested, which understates every change
    and understates the largest ones most.
    """
    out = {}
    for each in _months_back(month, limit):
        if each >= month:
            continue
        for name, cents in ledger.totals_by_category(connection, each).items():
            out.setdefault(name, []).append(max(0, -cents))
    return out


def _months_back(month, count):
    """`count` month strings ending at `month`, oldest first."""
    year, mon = (int(part) for part in month.split("-")[:2])
    out = []
    for step in range(count - 1, -1, -1):
        total = year * 12 + (mon - 1) - step
        out.append(f"{total // 12:04d}-{total % 12 + 1:02d}")
    return out


def _signed(minor):
    """A change with its sign kept, which is the whole content of a change.

    Zero gets no sign. Written as a plain else-branch this printed "-EUR 0.00"
    for a month that spent exactly what the one before it did, which reads as
    a decrease that did not happen.
    """
    if not minor:
        return money.format(0)
    return ("+" if minor > 0 else "-") + money.format(abs(minor))


def _this_month():
    return dt.date.today().strftime("%Y-%m")
