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
import os
import sqlite3
import threading

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


def data_dir(directory=None):
    """Where the database and the rate cache live.

    Resolved per call, never captured at import. WALLET_DATA moves both, which
    is what lets a test run against a temporary directory without touching the
    real ledger.
    """
    return directory or os.environ.get("WALLET_DATA") or \
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def db_path(directory=None):
    return os.path.join(data_dir(directory), DB_NAME)


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
