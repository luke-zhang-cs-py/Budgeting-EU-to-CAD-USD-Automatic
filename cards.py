"""
cards.py
--------
The cards and accounts that pay for things, and what each one has cost.

The reason this exists as its own table rather than a setting: **the foreign
transaction fee is a property of the card, not of the purchase.** The same
euro coffee costs 2.5% more on a CIBC Visa than on a euro account, so a
tracker with one global fee is either wrong for one of them or silently
averaging across both. Attributing a purchase to the card that paid for it is
what makes the fee arithmetic exact.

The fee is stored in basis points -- an integer, 250 for CIBC's published
2.50% -- for the same reason every amount in this project is an integer. It
starts as a default and is meant to be corrected: once statements have been
imported, `measured_fee` reports what the card actually charged, which for
the CIBC Visa came out at 2.39-2.49% against the published 2.5%.

Balances are reported with their own uncertainty attached. A card that bills
in Canadian dollars has an exact figure for any purchase imported from a
statement, because the statement says what it billed. For a purchase typed in
euros it does not, so the euro amount is converted at the reference rate for
the day -- which is an estimate, and is counted separately and labelled as
one rather than being quietly added to the exact figures.
"""
import datetime as dt
import sqlite3

import fxcost
import fxrates
import money

# CIBC's published foreign-transaction fee, in basis points. A default to be
# corrected by measurement, not a fact -- see fxcost.TYPICAL_CARD_FEE_BP,
# which is the same figure and the one place it is written down.
DEFAULT_FEE_BP = fxcost.TYPICAL_CARD_FEE_BP

# A fee above this is not a card fee. Guards a typo -- 2500 meant as 2.5%
# rather than 250 -- from silently making every estimate 25% too high.
MAX_FEE_BP = 1000

# How many statement-priced purchases a card needs before its measured markup
# is worth showing. Below this one unusual rounding dominates the average.
ENOUGH_TO_MEASURE = 3


class CardError(ValueError):
    """A card could not be created or changed as asked."""


def _clean_name(value):
    name = (value or "").strip()
    if not name:
        raise CardError("a card needs a name")
    return name


def _clean_mask(value):
    """The last four digits, whatever decoration came with them.

    "••4417", "xxxx 4417" and "4417" are the same card, and storing the
    decoration would stop by_mask matching a screenshot that decorated it
    some other way.
    """
    return "".join(c for c in str(value or "") if c.isdigit())[-4:]


def _clean_fee(value):
    fee = DEFAULT_FEE_BP if value is None else int(value)
    if not 0 <= fee <= MAX_FEE_BP:
        raise CardError(
            f"a fee of {fee} basis points is not plausible; "
            f"{DEFAULT_FEE_BP} is 2.5% and the limit here is {MAX_FEE_BP}")
    return fee


# One cleaner per editable column, so `add` and `update` cannot disagree
# about what a valid value is. Written out in both, they already differed --
# update's fee message had lost half its explanation -- and a rule enforced
# on creation but not on correction is a rule with a way round it.
CLEANERS = {
    "name": _clean_name,
    "mask": _clean_mask,
    "fee_bp": _clean_fee,
    "opening_minor": lambda value: int(value),
}


def add(connection, name, currency, mask="", fee_bp=None,
        opening_minor=0):
    """Create a card. Returns its id.

    `currency` is what the card *bills* in, which is not the currency you
    spend in and is the whole point: a CIBC Visa bills CAD for a purchase made
    in euros, and that difference is where the fee lives.
    """
    name = _clean_name(name)
    fee = _clean_fee(fee_bp)
    mask = _clean_mask(mask)

    code = (currency or "").strip().upper()
    if code not in money.CURRENCIES:
        raise CardError(f"unknown currency: {currency!r}")

    try:
        cursor = connection.execute(
            "INSERT INTO cards (name, mask, currency, fee_bp, opening_minor, "
            "archived, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (name, mask, code, fee, int(opening_minor),
             dt.datetime.now().isoformat(timespec="seconds")))
    except sqlite3.IntegrityError as clash:
        raise CardError(f"there is already a card called {name!r}") from clash
    connection.commit()
    return cursor.lastrowid


def every(connection, include_archived=False):
    """Every card, newest last. Not called `all`, which is a builtin."""
    where = "" if include_archived else "WHERE archived = 0"
    return [_as_dict(row) for row in connection.execute(
        f"SELECT * FROM cards {where} ORDER BY archived, id")]


def get(connection, card_id):
    """One card, or None."""
    row = connection.execute("SELECT * FROM cards WHERE id = ?",
                             (card_id,)).fetchone()
    return _as_dict(row) if row else None


def by_mask(connection, mask):
    """The card whose last four match, or None.

    Used to attribute a screenshot: a card app's transaction detail usually
    shows "••4417", and matching it saves the person choosing. Returns None
    when two cards share a mask rather than picking one, because attributing a
    purchase to the wrong card puts the wrong fee on it.
    """
    digits = "".join(c for c in str(mask or "") if c.isdigit())[-4:]
    if len(digits) != 4:
        return None
    rows = connection.execute(
        "SELECT * FROM cards WHERE mask = ? AND archived = 0",
        (digits,)).fetchall()
    return _as_dict(rows[0]) if len(rows) == 1 else None


def update(connection, card_id, name=None, mask=None, fee_bp=None,
           opening_minor=None):
    """Change a card in place. Returns whether anything changed."""
    if not get(connection, card_id):
        raise CardError("no such card")

    given = {"name": name, "mask": mask, "fee_bp": fee_bp,
             "opening_minor": opening_minor}
    sets, args = [], []
    for column, value in given.items():
        if value is None:
            continue                      # not being changed
        sets.append(f"{column} = ?")
        args.append(CLEANERS[column](value))

    if not sets:
        return False
    args.append(card_id)
    try:
        connection.execute(
            f"UPDATE cards SET {', '.join(sets)} WHERE id = ?", args)
    except sqlite3.IntegrityError as clash:
        raise CardError("another card already has that name") from clash
    connection.commit()
    return True


def archive(connection, card_id, archived=True):
    """Hide a closed card without deleting its history.

    Archived rather than removed, because its purchases are real and deleting
    the card would either take them with it or orphan them. A closed card is
    still where last year's money went.
    """
    if not get(connection, card_id):
        raise CardError("no such card")
    connection.execute("UPDATE cards SET archived = ? WHERE id = ?",
                       (1 if archived else 0, card_id))
    connection.commit()
    return True


def remove(connection, card_id):
    """Delete a card, detaching its purchases first.

    The detach is explicit rather than left to ON DELETE SET NULL: a database
    migrated from before cards existed has a plain card_id column with no
    foreign key on it, because SQLite cannot add one with ALTER. Relying on
    the constraint would work on a new file and silently leave dangling ids on
    an old one.
    """
    if not get(connection, card_id):
        raise CardError("no such card")
    connection.execute(
        "UPDATE transactions SET card_id = NULL WHERE card_id = ?",
        (card_id,))
    connection.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    connection.commit()
    return True


def attribute(connection, transaction_id, card_id):
    """Say which card paid for a purchase. `card_id` of None detaches it."""
    if card_id is not None and not get(connection, card_id):
        raise CardError("no such card")
    cursor = connection.execute(
        "UPDATE transactions SET card_id = ? WHERE id = ?",
        (card_id, transaction_id))
    connection.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------- balances

def balances(connection, month=None, directory=None):
    """What each card has cost, in the currency it bills in.

    Spending is positive here and a refund is negative, so the figures net --
    see _billed_in_card_currency, which is where the two different sign
    conventions in the schema are reconciled.

    Every figure carries how it was arrived at. `exact_minor` is the sum of
    what statements said the card billed; `estimated_minor` is euro purchases
    converted at the reference rate for their own day, which is an estimate
    and is kept apart from the exact total rather than blended into it. A
    reader who is not told which is which will assume the whole figure is
    exact.
    """
    out = []
    for card in every(connection, include_archived=True):
        rows = _rows_for(connection, card["id"], month)
        exact, estimated, unknown = 0, 0, 0
        exact_rows, estimated_rows = 0, 0

        for row in rows:
            billed = _billed_in_card_currency(row, card, directory)
            if billed is None:
                unknown += 1
                continue
            amount, was_exact = billed
            if was_exact:
                exact += amount
                exact_rows += 1
            else:
                estimated += amount
                estimated_rows += 1

        spent = exact + estimated
        out.append(dict(card, **{
            "rows": len(rows),
            "exact_minor": exact,
            "exact_rows": exact_rows,
            "estimated_minor": estimated,
            "estimated_rows": estimated_rows,
            "unconvertible_rows": unknown,
            "spent_minor": spent,
            "spent_text": money.format(spent, card["currency"]),
            "balance_minor": card["opening_minor"] + spent,
            "balance_text": money.format(
                card["opening_minor"] + spent, card["currency"]),
        }))
    return out


def _rows_for(connection, card_id, month):
    args = [card_id]
    clause = "WHERE card_id = ? AND category != 'Income'"
    if month:
        clause += " AND spent_on LIKE ?"
        args.append(f"{month}-%")
    return connection.execute(
        f"SELECT * FROM transactions {clause}", args).fetchall()


def _billed_in_card_currency(row, card, directory=None):
    """(spend, was_exact) in the card's currency, or None.

    Spend is **positive for a purchase**, the way budgets.status reports it: a
    card line reading "-1,204.55" takes a moment to read and "1,204.55" does
    not. A refund comes back negative and so nets off, which is the property
    that keeps a month of returns from reading as a month of spending.

    Getting that consistent takes care, because the two sources of a figure
    store their signs differently. `amount_eur` is signed -- negative for an
    expense. `charged_minor` is a *magnitude*: the importers store abs(amount),
    so it carries no direction at all and the sign has to be taken from the
    euro amount beside it. Written without that, an exact row contributed
    +8594 while an estimated one contributed -572, and the balance was the
    sum of a spend and a refund that were both purchases.

    Exact when the statement disclosed what the card billed, in this card's
    currency. Otherwise the euro amount converted at the reference rate for
    the purchase date -- one conversion, through fxrates, which is where the
    business-day lag lives.
    """
    # A refund has a positive amount_eur, and reduces what the card has cost.
    direction = 1 if row["amount_eur"] <= 0 else -1

    charged = row["charged_minor"] if "charged_minor" in row.keys() else None
    charged_currency = (row["charged_currency"]
                        if "charged_currency" in row.keys() else None)
    if charged and charged_currency == card["currency"]:
        return abs(int(charged)) * direction, True

    if card["currency"] == money.BASE:
        return -int(row["amount_eur"]), True

    on = _day_of(row)
    if on is None:
        return None
    try:
        cents, _rate, _used = fxrates.convert(
            row["amount_eur"], on, card["currency"], directory)
    except (fxrates.RateError, money.MoneyError, ValueError):
        return None
    return -cents, False


def _day_of(row):
    """The purchase date as a date object, or None.

    fxrates wants a date and spent_on is stored as text, so the conversion
    has to happen at the caller -- passed straight through, the comparison
    against the newest published day raises TypeError comparing a str to a
    date, which is not a RateError and so was not caught by the handler
    below it.
    """
    try:
        return dt.date.fromisoformat(str(row["spent_on"]))
    except (ValueError, TypeError):
        return None


# ------------------------------------------------- the fee, measured not told

def measured_fee(connection, card_id, directory=None):
    """What this card's foreign markup really is, from its own statements.

    The published 2.5% is what CIBC says. This is what the rows say, and the
    two differ -- measurement put the Visa at 2.39-2.49%, because the network
    rate it converts at is not the ECB reference rate this app compares
    against. Reported with a count, because an average over two purchases is
    not a measurement.

    Returns None when there is not enough to measure, rather than falling back
    to the published figure dressed up as an observation.
    """
    card = get(connection, card_id)
    if not card:
        raise CardError("no such card")

    priced = []
    for row in _rows_for(connection, card_id, None):
        charged = row["charged_minor"] if "charged_minor" in row.keys() else None
        if not charged:
            continue
        on = _day_of(row)
        if on is None:
            continue
        try:
            _cents, rate, _used = fxrates.convert(
                row["amount_eur"], on, card["currency"], directory)
        except (fxrates.RateError, money.MoneyError, ValueError):
            continue
        seen = fxcost.compare(charged, card["currency"],
                              row["amount_eur"], rate)
        if seen and seen["plausible"]:
            priced.append(seen)

    if len(priced) < ENOUGH_TO_MEASURE:
        return None

    billed = sum(p["billed_minor"] for p in priced)
    # No divide-by-zero guard, because there is nothing to guard against:
    # fxcost.compare returns None rather than a row whose reference is zero,
    # and every reference it does return is money.convert(abs(...)), which is
    # positive. A `if not reference: return None` stood here and could never
    # fire -- an unreachable branch reads as a case somebody has thought
    # about, which is worse than no branch at all.
    reference = sum(p["reference_minor"] for p in priced)

    share = (billed - reference) / reference
    return {
        "rows": len(priced),
        "fee_bp": int(round(share * fxcost.BASIS_POINTS)),
        "percent": round(share * 100, 2),
        "published_bp": card["fee_bp"],
        "cost_minor": billed - reference,
        "cost_text": money.format(billed - reference, card["currency"]),
    }


def _as_dict(row):
    card = dict(row)
    card["fee_percent"] = card["fee_bp"] / fxcost.BASIS_POINTS * 100
    card["fee_text"] = f"{card['fee_percent']:.2f}%"
    card["label"] = (f"{card['name']} ..{card['mask']}" if card["mask"]
                     else card["name"])
    return card
