"""
upcoming.py
-----------
Subscriptions: what is due, what has landed, and what quietly got dearer.

Built on ledger.recurring, which does the detection. This module only asks
the calendar questions about what it found, so there is one definition of
"recurring" in the project rather than two that drift.

The framing is *expected*, never *scheduled*. Nothing here knows a billing
date -- it knows a charge has appeared in each of the last several months and
has not appeared in this one yet. That is a useful thing to be told and a
different claim from "due on the 14th", and the difference matters the first
time a subscription is cancelled: this reports it as not-yet-seen for a month
or two and then stops reporting it, rather than insisting a payment is
overdue forever.

Nothing here is ever added to a total. A predicted charge that gets counted
as spending is a figure with imaginary money in it.
"""
import datetime as dt

import ledger
import money

# Months without a sighting before a subscription is treated as finished
# rather than late. Two, because one covers an annual plan billed late and a
# statement imported before the month closed, and beyond that "you have not
# paid Netflix in a quarter" is almost always a cancellation.
FORGET_AFTER_MONTHS = 2

# A rise smaller than this is a rounding or an FX wobble on a foreign
# subscription, not a price change worth a line on a page.
NOTABLE_RISE_MINOR = 50


def status(connection, month=None, today=None):
    """Every detected subscription, with where it stands this month.

    `state` is one of:
      landed   -- a charge for this month is already in the ledger
      expected -- seen in recent months, not yet this one
      lapsed   -- not seen for FORGET_AFTER_MONTHS, so probably cancelled
    """
    today = today or dt.date.today()
    month = month or today.strftime("%Y-%m")

    out = []
    for found in ledger.recurring(connection):
        seen_months = {row["month"] for row in found["series"]}
        gap = _months_between(found["last_seen"], month)

        if month in seen_months:
            state = "landed"
        elif gap > FORGET_AFTER_MONTHS:
            state = "lapsed"
        else:
            state = "expected"

        rise = _rise(found)
        out.append(dict(found, **{
            "state": state,
            "months_since_seen": gap,
            "expected_minor": abs(found["latest_eur"]),
            "expected_text": money.format(abs(found["latest_eur"])),
            "rise": rise,
        }))

    order = {"expected": 0, "landed": 1, "lapsed": 2}
    return sorted(out, key=lambda row: (order[row["state"]],
                                        -row["expected_minor"]))


def outstanding(connection, month=None, today=None):
    """Only what has not landed yet, with the total still to come.

    The total is labelled as expected everywhere it is shown. It is the one
    figure on this page that is not a record of anything that happened.
    """
    rows = [row for row in status(connection, month, today)
            if row["state"] == "expected"]
    total = sum(row["expected_minor"] for row in rows)
    return {
        "rows": rows,
        "count": len(rows),
        "expected_minor": total,
        "expected_text": money.format(total),
    }


def monthly_cost(connection):
    """What the live subscriptions cost a month, and a year.

    Lapsed ones are excluded: counting a cancelled subscription in a running
    cost is how a figure like this stops being believed. The annual number is
    twelve times the monthly one and is labelled as a projection, because
    that is what it is -- not a sum of anything observed.
    """
    live = [row for row in status(connection)
            if row["state"] in ("landed", "expected")]
    monthly = sum(row["expected_minor"] for row in live)
    return {
        "count": len(live),
        "monthly_minor": monthly,
        "monthly_text": money.format(monthly),
        "yearly_minor": monthly * 12,
        "yearly_text": money.format(monthly * 12),
        "projected": True,
    }


def rises(connection):
    """Subscriptions that now cost more than they did, dearest jump first.

    The case this whole module is worth having for: a charge that goes up by
    a few euros is invisible in a monthly total and permanent once it is
    missed.
    """
    found = [row for row in status(connection) if row["rise"]]
    return sorted(found, key=lambda row: -row["rise"]["by_minor"])


def _rise(found):
    """{by_minor, by_text, percent} if the price went up, else None.

    Only up. A subscription that got cheaper is not a thing anybody needs
    warning about, and reporting both directions in a list called "rises"
    would be the kind of label that quietly stops being read.
    """
    changed = found.get("changed") or {}
    if not changed:
        return None

    # Stored negative for an expense, so a *rise* is a fall in the number.
    # Getting this backwards would report every increase as a saving, which
    # is the sort of sign error a reader would believe.
    was, now = abs(changed["from"]), abs(changed["to"])
    if now - was < NOTABLE_RISE_MINOR:
        return None
    return {
        "was_minor": was,
        "was_text": money.format(was),
        "now_minor": now,
        "now_text": money.format(now),
        "by_minor": now - was,
        "by_text": money.format(now - was),
        "percent": round((now - was) / was * 100, 1) if was else None,
        "yearly_minor": (now - was) * 12,
        "yearly_text": money.format((now - was) * 12),
    }


def _months_between(earlier, later):
    """Whole months from one YYYY-MM to another. Negative if `later` is first."""
    try:
        y1, m1 = (int(part) for part in earlier.split("-")[:2])
        y2, m2 = (int(part) for part in later.split("-")[:2])
    except (ValueError, AttributeError):          # pragma: no cover
        return 0
    return (y2 * 12 + m2) - (y1 * 12 + m1)
