"""
db.py
-----
SQLite schema and connections.

Four tables and no ORM: purchases, a cap per category, keyword rules, and the
files the watched folder has already read. An ORM would be more machinery than
the problem has.

The important part of the schema is the UNIQUE constraint on
transactions.fingerprint. Re-importing a statement that overlaps one already
loaded is the single most common way a spending tracker corrupts itself --
you export January-to-March, then February-to-April, and February is now
counted twice, so every total and every budget is wrong in a way that looks
like overspending. Enforcing it in the database rather than in Python means
it holds no matter which code path does the inserting.
"""
import contextlib
import os
import sqlite3
import threading

import paths

DB_NAME = "wallet.db"

# How much of a sha256 to keep for the identity columns -- transactions.
# fingerprint and imports.digest. 32 hex characters is 128 bits, which is far
# more than enough to identify a purchase or a file and short enough to read
# in a query. Named here because both those columns are declared in this
# schema, and because the length was written out as a bare [:32] in three
# places across two modules.
DIGEST_CHARS = 32

# Money never crosses this boundary as a float; see money.py. Amounts are
# INTEGER cents, and SQLite stores them exactly.
SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    spent_on     TEXT    NOT NULL,          -- ISO date, YYYY-MM-DD
    description  TEXT    NOT NULL,
    merchant     TEXT    NOT NULL DEFAULT '',
    amount_eur   INTEGER NOT NULL,          -- cents; negative is a refund
    category     TEXT    NOT NULL DEFAULT 'Uncategorised',
    source       TEXT    NOT NULL DEFAULT 'manual',
    fingerprint  TEXT    NOT NULL UNIQUE,
    created_at   TEXT    NOT NULL,

    -- What the card actually billed, when that differs from the euro amount.
    --
    -- A Canadian card used in Europe does not charge you euros: CIBC converts
    -- at the Visa rate and adds 2.5%, so its export shows CAD. Keeping the
    -- billed figure beside the euro one is what lets the app say what the
    -- conversion cost, rather than silently reporting one currency as the
    -- other. Null for an ordinary euro purchase, which is most of them.
    charged_minor    INTEGER,
    charged_currency TEXT,

    -- The bank's own id for the transaction, from an OFX <FITID>.
    --
    -- Authoritative where the fingerprint is a guess. Two identical coffees on
    -- one day are one purchase to a fingerprint of date, amount and
    -- description, and genuinely two to the bank -- which says so, with two
    -- different ids. Null for a CSV row and for manual entry, which is why
    -- the fingerprint has to stay.
    fitid            TEXT UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_tx_date     ON transactions(spent_on);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);

CREATE TABLE IF NOT EXISTS budgets (
    category   TEXT    PRIMARY KEY,
    cap_eur    INTEGER NOT NULL CHECK (cap_eur >= 0)
);

CREATE TABLE IF NOT EXISTS rules (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword  TEXT    NOT NULL UNIQUE,       -- matched case-insensitively
    category TEXT    NOT NULL
);

-- Files the watched folder has already read, identified by a digest of their
-- contents rather than their name. UNIQUE for the same reason
-- transactions.fingerprint is: unattended import re-reads the folder every
-- minute, and "have I seen this" has to be settled by the database rather
-- than by remembering. Renaming an export does not make it new; editing one
-- does.
CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT    NOT NULL,
    digest      TEXT    NOT NULL UNIQUE,
    added       INTEGER NOT NULL DEFAULT 0,
    duplicate   INTEGER NOT NULL DEFAULT 0,
    unreadable  INTEGER NOT NULL DEFAULT 0,
    at          TEXT    NOT NULL
);
"""

UNCATEGORISED = "Uncategorised"

_lock = threading.Lock()


def db_path(directory=None):
    """Where the database lives.

    The directory decision belongs to paths, not here. This module resolved it
    and so did fxrates, each reading WALLET_DATA for itself -- so nothing
    forced the database and the rate cache to agree on a location.
    """
    return os.path.join(paths.data_dir(directory), DB_NAME)


@contextlib.contextmanager
def session(directory=None):
    """A connection that is actually closed when the block ends.

    `with sqlite3.connect(...) as conn:` looks like it closes and does not --
    sqlite3's context manager scopes a *transaction*, committing or rolling
    back, and leaves the connection open. Every request therefore leaked one,
    which pytest reported as `ResourceWarning: unclosed database` thirty-six
    times a run. A server left up long enough runs out of file handles.
    """
    connection = connect(directory)
    try:
        yield connection
    finally:
        connection.close()


def connect(directory=None):
    """A connection with the schema applied and foreign keys on.

    Rows come back as sqlite3.Row so callers read by name. Positional access
    to a widening table is how a column gets read as the wrong field.
    """
    path = db_path(directory)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    with _lock:
        connection.executescript(SCHEMA)
        _migrate(connection)
        connection.commit()
    return connection


# Columns added after the first release. CREATE TABLE IF NOT EXISTS does
# nothing to a table that already exists, so a database created before these
# were added would be missing them and every query naming one would fail.
# Additive only: SQLite can add a nullable column in place, and nothing here
# ever drops or rewrites a column, because the file holds the only copy of
# somebody's spending history.
LATER_COLUMNS = {
    "transactions": (
        ("charged_minor", "INTEGER"),
        ("charged_currency", "TEXT"),
        # No UNIQUE here: SQLite cannot add a uniqueness constraint to an
        # existing table with ALTER. New databases get it from the schema
        # above; migrated ones get the column and rely on the lookup in
        # ledger.add, which is where the check actually happens.
        ("fitid", "TEXT"),
    ),
}


def _migrate(connection):
    """Add any column this version expects and the file does not have."""
    for table, columns in LATER_COLUMNS.items():
        have = {row["name"] for row in
                connection.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue                        # the table itself is new
        for name, kind in columns:
            if name not in have:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
