"""
budgets.py
----------
A monthly cap per category, and how the month is going against it.

One model, chosen deliberately: you say "Groceries, 400 a month", and this
reports spent, remaining, and whether that is ahead of or behind the pace the
month needs. No rollover, no envelopes, no forecasting. Those are the features
that make budgeting apps get abandoned during setup, and the simple version
answers the question people actually ask, which is "can I afford this".

Caps are held in euros, because euros are what gets spent. A cap in CAD would
move every day the rate moved, so "am I over budget" would depend on the
exchange rate rather than on spending -- and you could go over budget without
buying anything.
"""
import calendar
import datetime as dt

import db
import fxrates
import ledger
import money

# Below this fraction of the month elapsed, pace is not reported. Three days
# into January, "you are 900% over pace" is arithmetically true and useless:
# one weekly shop always looks like a catastrophe.
MIN_ELAPSED_FOR_PACE = 0.15

# How close to the cap counts as "close" rather than "fine". A warning at 100%
# is not a warning, it is a receipt.
WARN_AT = 0.80


def set_cap(connection, category, cap_eur):
    """Set or replace a monthly cap. A cap of 0 removes it.

    Zero means "no budget" rather than "budget of nothing", because a cap of
    nothing would put every category permanently over.
    """
    cap = int(cap_eur)
    if cap < 0:
        raise ValueError("a cap cannot be negative")
    if cap == 0:
        connection.execute("DELETE FROM budgets WHERE category = ?",
                           (category,))
    else:
        connection.execute(
            "INSERT INTO budgets (category, cap_eur) VALUES (?, ?) "
            "ON CONFLICT(category) DO UPDATE SET cap_eur = excluded.cap_eur",
            (category, cap))
    connection.commit()


def caps(connection):
    """{category: cents}."""
    return {row["category"]: row["cap_eur"] for row in connection.execute(
        "SELECT category, cap_eur FROM budgets ORDER BY category")}


def _elapsed(month, today=None):
    """(fraction_of_month_gone, days_left). Zero and 0 for a past month."""
    today = today or dt.date.today()
    year, mon = (int(part) for part in month.split("-"))
    days = calendar.monthrange(year, mon)[1]
    if (today.year, today.month) > (year, mon):
        return 1.0, 0
    if (today.year, today.month) < (year, mon):
        return 0.0, days
    return today.day / days, days - today.day


def status(connection, month=None, today=None):
    """Every budgeted category this month, plus anything spent without a cap.

    Spending is reported as a positive number here even though it is stored
    negative. A budget line reading "-450 of 400" is harder to read than
    "450 of 400", and the sign carries no information once the row is labelled
    as spending.
    """
    month = month or dt.date.today().strftime("%Y-%m")
    spent = ledger.totals_by_category(connection, month)
    limits = caps(connection)
    fraction, days_left = _elapsed(month, today)

    rows = []
    for category in sorted(set(limits) | set(spent)):
        cap = limits.get(category, 0)
        # Stored negative for an expense; a refund-heavy category can be
        # positive, which is not overspending and must not read as such.
        used = max(0, -spent.get(category, 0))
        remaining = cap - used if cap else None
        share = (used / cap) if cap else None

        pace = None
        if cap and fraction >= MIN_ELAPSED_FOR_PACE and fraction < 1.0:
            # What would have been spent by now at an even rate.
            expected = cap * fraction
            pace = "over" if used > expected else "under"

        rows.append({
            "category": category,
            "cap_eur": cap or None,
            "cap_text": money.format(cap, "EUR") if cap else "",
            "spent_eur": used,
            "spent_text": money.format(used, "EUR"),
            "remaining_eur": remaining,
            "remaining_text": money.format(remaining, "EUR")
            if remaining is not None else "",
            "share": round(share, 4) if share is not None else None,
            "percent": int(round(share * 100)) if share is not None else None,
            "state": _state(cap, used, share),
            "pace": pace,
            "days_left": days_left,
        })
    return rows


def _state(cap, used, share):
    """One word for the row, so the UI does not re-derive the thresholds."""
    if not cap:
        return "untracked"
    if used > cap:
        return "over"
    if share is not None and share >= WARN_AT:
        return "close"
    return "fine"


def summary(connection, month=None, today=None, directory=None):
    """The figures for the top of the page.

    Totals are converted from the euro total rather than summed from
    per-transaction conversions. The two differ by a cent or two, and the
    honest choice is the one that matches the euro figure it is labelled as,
    since the euro figure is the one that is not an estimate.
    """
    month = month or dt.date.today().strftime("%Y-%m")
    rows = status(connection, month, today)
    spent = sum(row["spent_eur"] for row in rows)
    budgeted = sum(row["cap_eur"] or 0 for row in rows)
    fraction, days_left = _elapsed(month, today)

    out = {
        "month": month,
        "spent_eur": spent,
        "spent_text": money.format(spent, "EUR"),
        "budgeted_eur": budgeted or None,
        "budgeted_text": money.format(budgeted, "EUR") if budgeted else "",
        "remaining_eur": (budgeted - spent) if budgeted else None,
        "remaining_text": money.format(budgeted - spent, "EUR")
        if budgeted else "",
        "over": [r["category"] for r in rows if r["state"] == "over"],
        "close": [r["category"] for r in rows if r["state"] == "close"],
        "untracked": [r["category"] for r in rows
                      if r["state"] == "untracked" and r["spent_eur"] > 0],
        "days_left": days_left,
        "elapsed": round(fraction, 3),
        "uncategorised_eur": next(
            (r["spent_eur"] for r in rows
             if r["category"] == db.UNCATEGORISED), 0),
    }
    out.update(_converted_total(spent, month, today, directory))
    return out


def _converted_total(spent, month, today=None, directory=None):
    """The month's total in each target currency, plus the rate date.

    Split out of summary() so that function stays about the budget rather than
    about exchange rates. Both figures blank rather than zero when there is no
    rate: a blank says "not known", and a zero says "nothing was spent".
    """
    out = {"rate_date": ""}
    on = _last_day_seen(month, today)

    # Clamped to what has actually been published, which a *total* may do and a
    # transaction may not.
    #
    # A purchase carries a date, so converting it at a rate that did not exist
    # when the money was spent is hindsight and fxrates rightly refuses it. A
    # month-to-date total carries no date -- "today" here just means "now" --
    # so the honest rate is the latest one published at or before now.
    #
    # Without this the CAD and USD headline figures went blank every weekend,
    # every public holiday, and every morning before the ECB publishes around
    # 16:00 CET: roughly two days in seven, on a screen whose whole purpose is
    # those two figures. Found by running the app rather than by reading it.
    published = fxrates.newest(directory)
    if published and on > published:
        on = published

    for currency, result in fxrates.convert_all(spent, on, directory).items():
        key = currency.lower()
        out[f"spent_{key}"] = result["cents"]
        out[f"spent_{key}_text"] = money.format(result["cents"], currency)
        if result["used"]:
            out["rate_date"] = result["used"].isoformat()
    return out


def _last_day_seen(month, today=None):
    """The date to convert a month's total at: today if the month is current,
    otherwise the month's last day. Converting January's total at today's rate
    would make a closed month's figure drift."""
    today = today or dt.date.today()
    year, mon = (int(part) for part in month.split("-"))
    if (today.year, today.month) == (year, mon):
        return today
    return dt.date(year, mon, calendar.monthrange(year, mon)[1])
