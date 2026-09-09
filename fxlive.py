"""
fxlive.py
---------
The rate right now, for deciding whether to buy the thing.

Separate from fxrates on purpose, because the two answer different questions
and conflating them would corrupt the ledger.

**fxrates** answers *what did this cost me* -- the ECB reference rate on the
day a purchase was made, from a cached history going back to 1999. That is
what every stored transaction is converted at, and it must never move once
recorded.

**fxlive** answers *what would this cost me* -- fetched when you ask, for an
amount you are looking at in a shop. Nothing it returns is ever written to a
transaction.

A word on "real time", because the phrase promises more than any free source
delivers and more than the situation can use. Both sources here republish the
ECB's daily reference rate, fixed once each business day around 16:00 CET.
There is no free intraday EUR/CAD tick without an account, and chasing one
would be false precision anyway: **a card purchase is not settled at the rate
at the moment you tap.** Visa converts at its own rate on the day the
transaction reaches the network, which can be a day or two later, and CIBC
then adds its foreign-transaction fee on top. So the useful answer is the
latest published rate, labelled with the day it was published and the moment
it was fetched, plus the card's fee -- which is what this returns, rather
than a number implying a precision that does not exist.
"""
import datetime as dt
import json
import threading
import time
from decimal import Decimal, InvalidOperation

import fetch
import money

# Tried in order. Both are free and keyless; the first is the ECB's own
# figures republished, which keeps this consistent with the cached history
# every stored transaction is converted against.
SOURCES = (
    ("frankfurter",
     "https://api.frankfurter.dev/v1/latest?base=EUR&symbols=CAD,USD"),
    ("er-api", "https://open.er-api.com/v6/latest/EUR"),
)

# Short, because this is fetched while somebody waits on a page. The ECB
# history download gets two minutes; a rate quote does not deserve more than
# a few seconds before the page says "not right now" and carries on.
TIMEOUT = 6

# The underlying figure changes once a business day, so re-asking more often
# than this only adds latency and load. Fifteen minutes keeps a page snappy
# while still picking up the afternoon publication without a restart.
CACHE_SECONDS = 900

# A rate outside this is not a rate. EUR/CAD has spent its entire existence
# between about 1.2 and 1.8, so these bounds are wide enough to never
# reject a real move and tight enough to catch a source that starts
# returning an inverted quote or a placeholder.
SANE = {"CAD": (Decimal("1.0"), Decimal("2.5")),
        "USD": (Decimal("0.7"), Decimal("2.0"))}

_lock = threading.Lock()
_cached = None            # (monotonic_seconds, quote-dict)


def reset():
    """Forget the cached quote. For tests, and after a source change."""
    global _cached
    with _lock:
        _cached = None


def quote(timeout=TIMEOUT, now=None):
    """The latest published EUR rates, or None.

    None rather than an exception: every caller is drawing a page that is
    perfectly usable without a live rate, and the stored history is what the
    ledger relies on regardless. "Not right now" is a legitimate answer and
    the interface shows it as one.
    """
    global _cached
    with _lock:
        if _cached and (time.monotonic() - _cached[0]) < CACHE_SECONDS:
            return _cached[1]

    fresh = _ask_each_source(timeout, now)
    if fresh:
        with _lock:
            _cached = (time.monotonic(), fresh)
    return fresh


def _ask_each_source(timeout, now):
    for name, url in SOURCES:
        raw = fetch.get(url, timeout)
        if not raw:
            continue
        parsed = _read(raw, name, now)
        if parsed:
            return parsed
    return None


def _read(raw, source, now):
    """One source's JSON as a quote, or None if it is not usable.

    Both shapes carry `rates` and a date, under different names, so this
    reads what is there rather than assuming a shape -- a source that changes
    its envelope should make this return None, not raise.
    """
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None
    if not isinstance(body, dict):
        return None

    rates = body.get("rates")
    if not isinstance(rates, dict):
        return None

    kept = {}
    for code in money.TARGETS:
        value = _sane(code, rates.get(code))
        if value is not None:
            kept[code] = value
    if not kept:
        return None

    return {
        "rates": kept,
        "texts": {code: f"{value:.4f}" for code, value in kept.items()},
        "published": _published(body),
        "fetched_at": (now or dt.datetime.now()).isoformat(timespec="seconds"),
        "source": source,
        # Stated rather than implied. The figure is a daily reference rate,
        # and a caller that presents it as a live tick is misleading its
        # reader; the interface reads this flag rather than knowing it.
        "daily_reference": True,
    }


def _sane(code, value):
    """A rate as a Decimal, or None if it is missing or implausible."""
    if value is None:
        return None
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    low, high = SANE.get(code, (Decimal(0), Decimal("1e9")))
    if not low <= rate <= high:
        return None
    return rate


def _published(body):
    """The day the source says the rates are for, as an ISO date or None."""
    direct = body.get("date")
    if isinstance(direct, str) and len(direct) == 10:
        return direct
    stamp = body.get("time_last_update_utc")
    if isinstance(stamp, str):
        for shape in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S"):
            try:
                return dt.datetime.strptime(stamp.strip(), shape).date(
                    ).isoformat()
            except ValueError:
                continue
    return None
