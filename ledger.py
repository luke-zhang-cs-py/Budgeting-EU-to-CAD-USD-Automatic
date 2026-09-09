"""
ledger.py
---------
The transactions themselves: adding, categorising, searching, and the
duplicate rule.

Everything is stored in euros, because that is the currency the money was
actually spent in and it is the only figure that never changes. CAD and USD
are derived at read time from the rate that applied on the transaction date,
so a past month's report is the same every time it is opened. Storing the
converted figures instead would freeze whatever rate happened to be cached on
import day, and there would be no way to tell later whether a number was
wrong or just old.
"""
import datetime as dt
import hashlib
import re

import db
import fxcost
import fxrates
import money

# Categories offered out of the box. Not enforced -- a category is any string,
# so nobody has to fight the list -- but having sensible defaults is the
# difference between a tracker somebody uses and one they abandon on setup.
DEFAULT_CATEGORIES = (
    "Groceries", "Eating out", "Transport", "Housing", "Utilities",
    "Health", "Shopping", "Entertainment", "Travel", "Fees", "Income",
    db.UNCATEGORISED,
)

# Bank exports are shouty and full of terminal noise. Normalising before
# fingerprinting means "TESCO STORES 4823  DUBLIN" and "Tesco Stores 4823
# Dublin" are recognised as the same purchase rather than imported twice.
_NOISE = re.compile(r"\b(card|visa|mastercard|debit|credit|pos|contactless|"
                    r"purchase|payment|transaction|ref|reference|auth)\b",
                    re.IGNORECASE)
_NON_WORD = re.compile(r"[^a-z0-9]+")
_LONG_DIGITS = re.compile(r"\d{4,}")


def normalise(description):
    """A description reduced to what identifies the purchase.

    Long digit runs go first: card terminal ids and reference numbers differ
    between two exports of the same transaction, so keeping them would defeat
    duplicate detection entirely.
    """
    text = (description or "").lower()
    text = _LONG_DIGITS.sub(" ", text)
    text = _NOISE.sub(" ", text)
    return _NON_WORD.sub(" ", text).strip()


def merchant_of(description):
    """A short, readable name for grouping and for the recurring check."""
    words = [w for w in normalise(description).split() if len(w) > 1]
    return " ".join(words[:3]).title() if words else ""


def fingerprint(spent_on, amount_eur, description):
    """The identity of a purchase, for the UNIQUE constraint.

    Date plus amount plus normalised description. Deliberately not the bank's
    own reference: exports from different date ranges give the same
    transaction different references, and cash entries have none at all.

    The trade-off is honest and worth stating: two genuinely separate
    identical purchases on the same day -- two 2.50 coffees from one shop --
    look like one to this scheme. `force` on add() exists for exactly that,
    and the UI offers it, because silently dropping a real second coffee is a
    smaller and much more visible error than silently doubling February.
    """
    key = f"{spent_on}|{int(amount_eur)}|{normalise(description)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def as_date(value):
    """A date from whatever the importer produced. Raises on nonsense."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y",
                    "%Y/%m/%d", "%d-%m-%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"unreadable date: {value!r}")


# ------------------------------------------------------------------ writing

def add(connection, spent_on, description, amount_eur, category=None,
        source="manual", force=False, charged_minor=None,
        charged_currency=None):
    """Record one purchase. Returns (id, "added") or (existing_id, "duplicate").

    A duplicate is a normal outcome rather than an error: importing an
    overlapping statement is the expected way to use this, and the useful
    answer is "40 added, 12 already known".
    """
    on = as_date(spent_on)
    amount = int(amount_eur)
    description = (description or "").strip()
    if not description:
        raise ValueError("a transaction needs a description")

    mark = fingerprint(on.isoformat(), amount, description)
    if force:
        # A real second identical purchase. Salted so it gets its own row
        # without weakening the constraint for everything else.
        mark = hashlib.sha256((mark + str(dt.datetime.now())
                               ).encode("utf-8")).hexdigest()[:32]

    existing = connection.execute(
        "SELECT id FROM transactions WHERE fingerprint = ?", (mark,)).fetchone()
    if existing:
        return existing["id"], "duplicate"

    category = category or categorise(connection, description)
    cursor = connection.execute(
        "INSERT INTO transactions (spent_on, description, merchant, "
        "amount_eur, category, source, fingerprint, created_at, "
        "charged_minor, charged_currency) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (on.isoformat(), description, merchant_of(description), amount,
         category, source, mark, dt.datetime.now().isoformat(timespec="seconds"),
         int(charged_minor) if charged_minor else None,
         (charged_currency or "").upper() or None))
    connection.commit()
    return cursor.lastrowid, "added"


def recategorise(connection, transaction_id, category):
    connection.execute("UPDATE transactions SET category = ? WHERE id = ?",
                       (category, transaction_id))
    connection.commit()


def remove(connection, transaction_id):
    connection.execute("DELETE FROM transactions WHERE id = ?",
                       (transaction_id,))
    connection.commit()


# ------------------------------------------------------------------- rules

def add_rule(connection, keyword, category):
    """A keyword that assigns a category. Case-insensitive substring match."""
    keyword = (keyword or "").strip().lower()
    if not keyword:
        raise ValueError("a rule needs a keyword")
    connection.execute(
        "INSERT INTO rules (keyword, category) VALUES (?, ?) "
        "ON CONFLICT(keyword) DO UPDATE SET category = excluded.category",
        (keyword, category))
    connection.commit()


def rules(connection):
    return [dict(row) for row in connection.execute(
        "SELECT id, keyword, category FROM rules ORDER BY keyword")]


def remove_rule(connection, rule_id):
    connection.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    connection.commit()


def categorise(connection, description):
    """The category a description earns from the rules, else Uncategorised.

    Longest keyword first, so a specific rule beats a general one: "amazon
    fresh" -> Groceries wins over "amazon" -> Shopping regardless of which
    was added first. Insertion order deciding this would be surprising.
    """
    text = normalise(description)
    best = None
    for row in connection.execute("SELECT keyword, category FROM rules"):
        if row["keyword"] in text:
            if best is None or len(row["keyword"]) > len(best[0]):
                best = (row["keyword"], row["category"])
    return best[1] if best else db.UNCATEGORISED


def apply_rules(connection, only_uncategorised=True):
    """Re-run the rules over existing rows. Returns how many changed.

    Wanted every time a rule is added: the point of writing "netflix ->
    Entertainment" is to fix the Netflix charges already sitting in the
    ledger, not just future ones.
    """
    where = "WHERE category = ?" if only_uncategorised else ""
    args = (db.UNCATEGORISED,) if only_uncategorised else ()
    changed = 0
    for row in connection.execute(
            f"SELECT id, description, category FROM transactions {where}",
            args).fetchall():
        found = categorise(connection, row["description"])
        if found != row["category"] and found != db.UNCATEGORISED:
            connection.execute("UPDATE transactions SET category = ? "
                               "WHERE id = ?", (found, row["id"]))
            changed += 1
    connection.commit()
    return changed


# ------------------------------------------------------------------ reading

def _converted(row, directory=None):
    """One transaction as a dict, with CAD and USD and the rate used.

    A row whose rate cannot be established keeps its euro figure and reports
    the reason, rather than showing zero. Zero in a money column is
    indistinguishable from a free purchase.
    """
    out = dict(row)
    out["spent_on"] = row["spent_on"]
    out["amount_eur_text"] = money.format(row["amount_eur"], "EUR")
    out["rate_note"] = ""
    out["rate_date"] = ""
    out["rate_lag_days"] = None

    rates = fxrates.convert_all(row["amount_eur"], as_date(row["spent_on"]),
                                directory)
    for currency, result in rates.items():
        key = currency.lower()
        out[f"amount_{key}"] = result["cents"]
        out[f"amount_{key}_text"] = money.format(result["cents"], currency)
        out[f"rate_{key}"] = str(result["rate"]) if result["rate"] else ""
        if result["used"]:
            out["rate_date"] = result["used"].isoformat()
            out["rate_lag_days"] = result["lag_days"]
        elif result["why"]:
            out["rate_note"] = result["why"]

    # What the card charged, against what the reference rate says it should
    # have. Only for rows that were billed in something other than euros --
    # which for a Canadian card in Europe is all of them, and for an ordinary
    # euro purchase is none.
    out["fx"] = None
    billed_currency = out.get("charged_currency")
    if out.get("charged_minor") and billed_currency in rates:
        out["fx"] = fxcost.compare(out["charged_minor"], billed_currency,
                                   row["amount_eur"],
                                   rates[billed_currency]["rate"])
    return out


def transactions(connection, month=None, category=None, search=None,
                 directory=None, limit=None):
    """Transactions, newest first, converted.

    `month` is "YYYY-MM". Filtering by prefix rather than by a date range
    keeps it a string comparison SQLite can use the index for, and there is no
    month-length arithmetic to get wrong.
    """
    clauses, args = [], []
    if month:
        clauses.append("spent_on LIKE ?")
        args.append(f"{month}-%")
    if category:
        clauses.append("category = ?")
        args.append(category)
    if search:
        clauses.append("(LOWER(description) LIKE ? OR LOWER(category) LIKE ?)")
        args += [f"%{search.lower()}%"] * 2
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (f"SELECT * FROM transactions {where} "
           f"ORDER BY spent_on DESC, id DESC")
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    return [_converted(row, directory)
            for row in connection.execute(sql, args).fetchall()]


def months(connection):
    """Every month that has transactions, newest first, for the picker."""
    return [row["m"] for row in connection.execute(
        "SELECT DISTINCT substr(spent_on, 1, 7) AS m FROM transactions "
        "ORDER BY m DESC")]


def categories(connection):
    """Categories in use, plus the defaults, so the dropdown is never empty."""
    used = {row["category"] for row in connection.execute(
        "SELECT DISTINCT category FROM transactions")}
    return sorted(used | set(DEFAULT_CATEGORIES))


def totals_by_category(connection, month=None):
    """{category: cents} of spending. Refunds net off, income is excluded.

    Income is left out because mixing it in makes "spent" meaningless: a
    salary would swamp the categories and a budget comparison would be
    nonsense.
    """
    args = []
    where = "WHERE category != 'Income'"
    if month:
        where += " AND spent_on LIKE ?"
        args.append(f"{month}-%")
    return {row["category"]: row["total"] for row in connection.execute(
        f"SELECT category, SUM(amount_eur) AS total FROM transactions "
        f"{where} GROUP BY category ORDER BY total DESC", args)}


def monthly_totals(connection, limit=12):
    """[(month, cents)] newest first -- the trend line."""
    return [(row["m"], row["total"]) for row in connection.execute(
        "SELECT substr(spent_on, 1, 7) AS m, SUM(amount_eur) AS total "
        "FROM transactions WHERE category != 'Income' "
        "GROUP BY m ORDER BY m DESC LIMIT ?", (int(limit),))]


def recurring(connection, min_occurrences=3, tolerance_cents=200):
    """Charges that look like subscriptions.

    A merchant billing a similar amount in three or more distinct months is
    almost certainly a subscription. That is the useful definition rather than
    exact-amount matching, because prices rise and a 9.99 that became 10.99 is
    still the same subscription -- and it is the one somebody most wants
    flagged.

    Reported, never acted on. Guessing that a charge is recurring and hiding
    it from a total would be worse than not noticing.
    """
    rows = connection.execute(
        "SELECT merchant, substr(spent_on, 1, 7) AS m, "
        "       AVG(amount_eur) AS avg_cents, COUNT(*) AS n "
        "FROM transactions WHERE merchant != '' AND category != 'Income' "
        "GROUP BY merchant, m").fetchall()

    by_merchant = {}
    for row in rows:
        by_merchant.setdefault(row["merchant"], []).append(
            (row["m"], row["avg_cents"]))

    found = []
    for merchant, seen in by_merchant.items():
        if len(seen) < min_occurrences:
            continue
        amounts = [a for _m, a in seen]
        typical = sum(amounts) / len(amounts)
        if all(abs(a - typical) <= tolerance_cents for a in amounts):
            found.append({
                "merchant": merchant,
                "months": len(seen),
                "typical_eur": int(round(typical)),
                "typical_text": money.format(int(round(typical)), "EUR"),
                "last_seen": max(m for m, _a in seen),
            })
    return sorted(found, key=lambda f: -f["typical_eur"])
