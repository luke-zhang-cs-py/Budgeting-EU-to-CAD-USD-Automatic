"""
db.py
-----
SQLite schema and connections.

Three tables and no ORM. The data is a list of purchases, a cap per category
and a list of keyword rules; an ORM would be more machinery than the problem
has.

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
    created_at   TEXT    NOT NULL
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
        connection.commit()
    return connection
