"""
goals.py
--------
Something being saved for, and whether this month helped.

The design decision worth stating: **a goal has no balance.** There is no
second ledger here recording deposits, because a tracker with two sets of
books drifts, and the one that drifts is always the one nobody reconciles.
Progress is *derived* -- from what the budgets said you could spend and what
the transactions say you did.

So "saved" here means exactly one thing: the money your own caps left over.
If the caps total EUR 800 and the month's spending came to EUR 640, the month
contributed EUR 160. That is a real figure with a checkable derivation, and
it goes down if you spend more, which is the property that makes it worth
looking at.

It is deliberately not "income minus spending". This app sees a bank export
of card purchases, not a salary, so it does not know what came in -- and a
savings figure computed from an unknown income would be confident and wrong.
"""
import datetime as dt
import sqlite3

import budgets
import money

# A goal has to be worth tracking. Below this it is a purchase, not a goal,
# and the interface fills up with things that will be met next Tuesday.
MINIMUM_TARGET_MINOR = 100


class GoalError(ValueError):
    """A goal could not be created or changed as asked."""


def add(connection, name, target_minor, currency=money.BASE, due_on=None):
    """Create a goal. Returns its id."""
    name = (name or "").strip()
    if not name:
        raise GoalError("a goal needs a name")

    try:
        target = int(target_minor)
    except (TypeError, ValueError) as bad:
        raise GoalError(f"unreadable target: {target_minor!r}") from bad
    if target < MINIMUM_TARGET_MINOR:
        raise GoalError(
            f"a target of {money.format(target, currency)} is too small to "
            f"track; the minimum is "
            f"{money.format(MINIMUM_TARGET_MINOR, currency)}")

    code = (currency or money.BASE).strip().upper()
    if code not in money.CURRENCIES:
        raise GoalError(f"unknown currency: {currency!r}")

    due = _as_due(due_on)

    try:
        cursor = connection.execute(
            "INSERT INTO goals (name, target_minor, currency, due_on, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (name, target, code, due,
             dt.datetime.now().isoformat(timespec="seconds")))
    except sqlite3.IntegrityError as clash:
        raise GoalError(f"there is already a goal called {name!r}") from clash
    connection.commit()
    return cursor.lastrowid


def _as_due(due_on):
    """An ISO date string, or None. Raises rather than silently dropping it.

    A due date quietly discarded because it was mistyped is worse than a
    refusal: the goal looks saved and the deadline is simply gone.
    """
    if due_on in (None, ""):
        return None
    if isinstance(due_on, dt.date):
        return due_on.isoformat()
    try:
        return dt.date.fromisoformat(str(due_on).strip()).isoformat()
    except ValueError as bad:
        raise GoalError(f"unreadable due date: {due_on!r}") from bad


def every(connection):
    """Every goal, soonest deadline first, undated last."""
    rows = connection.execute("SELECT * FROM goals").fetchall()
    return sorted((dict(row) for row in rows),
                  key=lambda g: (g["due_on"] is None, g["due_on"] or "",
                                 g["name"]))


def get(connection, goal_id):
    row = connection.execute("SELECT * FROM goals WHERE id = ?",
                             (goal_id,)).fetchone()
    return dict(row) if row else None


def remove(connection, goal_id):
    cursor = connection.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    connection.commit()
    return cursor.rowcount > 0


def update(connection, goal_id, name=None, target_minor=None, due_on=None):
    """Change a goal. Returns whether anything changed."""
    if not get(connection, goal_id):
        raise GoalError("no such goal")

    sets, args = [], []
    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise GoalError("a goal needs a name")
        sets.append("name = ?")
        args.append(cleaned)
    if target_minor is not None:
        target = int(target_minor)
        if target < MINIMUM_TARGET_MINOR:
            raise GoalError("that target is too small to track")
        sets.append("target_minor = ?")
        args.append(target)
    if due_on is not None:
        sets.append("due_on = ?")
        args.append(_as_due(due_on))

    if not sets:
        return False
    args.append(goal_id)
    connection.execute(f"UPDATE goals SET {', '.join(sets)} WHERE id = ?",
                       args)
    connection.commit()
    return True


# ---------------------------------------------------------------- progress

def contributed(connection, month=None, today=None):
    """What a month's underspend contributed, in euros.

    Derived from the caps, so it is checkable: the sum of every cap, less the
    euro spending in that month. Zero rather than negative when the month
    overspent -- an overspend is a debt to the month, not a withdrawal from a
    goal that has no balance to withdraw from, and showing "saved: -EUR 90"
    would imply one.

    Returns None when no caps are set at all. With nothing budgeted there is
    no "left over" to speak of, and inventing a figure from zero caps would
    make every month look like a total loss.
    """
    caps = budgets.caps(connection)
    if not caps:
        return None

    allowed = sum(caps.values())
    status = budgets.status(connection, month=month, today=today)
    spent = sum(row["spent_eur"] for row in status
                if row["category"] in caps)

    return {
        "month": month or _this_month(today),
        "allowed_minor": allowed,
        "allowed_text": money.format(allowed),
        "spent_minor": spent,
        "spent_text": money.format(spent),
        "saved_minor": max(0, allowed - spent),
        "saved_text": money.format(max(0, allowed - spent)),
        "overspent_minor": max(0, spent - allowed),
        "overspent_text": money.format(max(0, spent - allowed)),
        "categories": len(caps),
    }


def progress(connection, month=None, today=None):
    """Every goal with what this month did for it.

    The monthly contribution is applied to goals in deadline order, soonest
    first, and not divided between them. Splitting it evenly would show four
    goals each a quarter met and none of them reachable, which tells you
    nothing about what to do; filling the nearest deadline first at least
    answers "what is this month paying for".
    """
    month_figure = contributed(connection, month=month, today=today)
    remaining = month_figure["saved_minor"] if month_figure else 0
    today = today or dt.date.today()

    out = []
    for goal in every(connection):
        applied = 0
        if goal["currency"] == money.BASE and remaining > 0:
            applied = min(remaining, goal["target_minor"])
            remaining -= applied

        share = (applied / goal["target_minor"]) if goal["target_minor"] else 0
        out.append(dict(goal, **{
            "this_month_minor": applied,
            "this_month_text": money.format(applied, goal["currency"]),
            "target_text": money.format(goal["target_minor"],
                                        goal["currency"]),
            "share": round(min(1.0, share), 4),
            "percent": round(min(100.0, share * 100), 1),
            "days_left": _days_left(goal["due_on"], today),
            "monthly_needed_minor": _needed(goal, today),
            "monthly_needed_text": money.format(_needed(goal, today) or 0,
                                                goal["currency"]),
            # A goal in a currency this month's euro underspend cannot fund.
            # Said rather than shown as zero progress, which would read as
            # "you saved nothing" instead of "this is measured in dollars".
            "fundable": goal["currency"] == money.BASE,
        }))
    return out


def _needed(goal, today):
    """What must be set aside monthly to hit the deadline, or None."""
    left = _days_left(goal["due_on"], today)
    if left is None or left <= 0:
        return None
    months = max(1, round(left / 30.44))
    return int(-(-goal["target_minor"] // months))    # ceiling division


def _days_left(due_on, today):
    if not due_on:
        return None
    try:
        return (dt.date.fromisoformat(due_on) - today).days
    except ValueError:                            # pragma: no cover
        return None


def _this_month(today=None):
    return (today or dt.date.today()).strftime("%Y-%m")
