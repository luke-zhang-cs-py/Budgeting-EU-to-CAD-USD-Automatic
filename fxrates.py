"""
fxrates.py
----------
Euro reference rates from the European Central Bank, cached locally.

The ECB publishes one file covering every business day back to 1999-01-04 --
7,088 days, 638 KB zipped, no API key and no account. That is the whole
rate problem solved by one download, so this uses it as the single source
rather than stitching together a "latest" endpoint and a history endpoint.
One format, one parser.

The part that needs care is that **the ECB publishes on business days only.**
There are no weekend rows at all, and the longest gap in the series is five
days: Easter 2026 runs 2 April straight to 7 April, and Christmas 2025 runs
24 December to 29 December. So a purchase made on Easter Sunday has no rate
of its own and must use the preceding published day -- four days earlier, not
one. Code that reaches for "yesterday" is wrong several times a year, and
wrong quietly, because it just fails to find a rate and something downstream
substitutes a zero or today's figure.

Every conversion therefore carries the date of the rate it used, and that
date goes in the output file. If a euro amount was converted at Thursday's
rate because it was spent on Sunday, the file says so.
"""
import csv
import datetime as dt
import io
import os
import subprocess
import threading
import urllib.error
import urllib.request
import zipfile
from decimal import Decimal

import money
import paths

HISTORY_URL = ("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip")

# Where the cache lives. Gitignored: it is derived data, it goes stale daily,
# and it is 150 KB of numbers anybody can re-download.
CACHE_NAME = "fx_rates.csv"

# The measured worst case is 5 days (Easter, Christmas). Ten gives room for a
# longer national holiday run without ever silently walking back far enough
# to use a materially different rate.
MAX_LOOKBACK_DAYS = 10

# ECB's own date format in the history file.
_DATE = "%Y-%m-%d"

_lock = threading.Lock()
_cache = None          # {date: {"CAD": Decimal, "USD": Decimal}}
_cache_path = None


class RateError(Exception):
    """No rate could be established for a date.

    Deliberately not "return None". A transaction that cannot be converted
    must be visible, because the alternative is a row of zeroes sitting in a
    budget total looking like a free purchase.
    """


def cache_path(directory=None):
    """Absolute path to the rate cache.

    Resolved per call rather than captured at import, so tests and the app can
    point at different directories. The face project had seven modules that
    captured their paths at import and a use() that consequently did nothing.
    """
    return os.path.join(paths.data_dir(directory), CACHE_NAME)


def _download(url, timeout=120):
    """The zip bytes, or None.

    urllib first, curl second -- not a preference. On some machines urllib
    cannot verify the chain because a local inspecting CA is not strict-OpenSSL
    clean, and certifi does not help since the interception is what fails.
    curl verifies differently and succeeds. Same reasoning as the transit
    project's realtime fetch.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        done = subprocess.run(["curl", "-sL", "--max-time", str(timeout), url],
                              capture_output=True, timeout=timeout + 20)
        return done.stdout if done.returncode == 0 and done.stdout else None
    except (OSError, subprocess.SubprocessError):
        return None


def _parse_history(raw):
    """{date: {currency: Decimal}} from the ECB history zip.

    Only the currencies this app uses are kept. The file carries 40-odd, and
    holding the rest would be 40 times the cache for no reader.

    Two shapes in the file to be careful of: it appends a trailing comma, so
    every row has a final empty cell and the header has a phantom column; and
    a currency that did not exist yet is written "N/A" rather than left blank.
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        name = archive.namelist()[0]
        with archive.open(name) as handle:
            rows = csv.reader(io.TextIOWrapper(handle, encoding="utf-8"))
            header = [cell.strip() for cell in next(rows)]
            columns = {cur: header.index(cur) for cur in money.TARGETS
                       if cur in header}
            missing = set(money.TARGETS) - set(columns)
            if missing:
                raise RateError(f"ECB file has no column for {sorted(missing)}")

            out = {}
            for row in rows:
                if not row or not row[0].strip():
                    continue
                try:
                    on = dt.datetime.strptime(row[0].strip(), _DATE).date()
                except ValueError:
                    continue
                day = {}
                for cur, index in columns.items():
                    text = row[index].strip() if index < len(row) else ""
                    if not text or text == "N/A":
                        continue
                    try:
                        day[cur] = Decimal(text)
                    except Exception:
                        continue
                if day:
                    out[on] = day
    return out


def refresh(directory=None, timeout=120):
    """Download the history and write the cache. True on success.

    Returns False rather than raising when the network is unavailable: the app
    is useful offline against whatever was last cached, and a failed refresh
    should not take the page down.
    """
    raw = _download(HISTORY_URL, timeout)
    if not raw:
        return False
    try:
        rates = _parse_history(raw)
    except (zipfile.BadZipFile, RateError, csv.Error):
        return False
    if not rates:
        return False

    path = cache_path(directory)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Written newest-first so a human opening the file sees current rates.
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date"] + list(money.TARGETS))
        for on in sorted(rates, reverse=True):
            writer.writerow([on.isoformat()] +
                            [str(rates[on].get(cur, "")) for cur in money.TARGETS])
    reset()
    return True


def load(directory=None):
    """The cache, read once. None if it has never been built."""
    global _cache, _cache_path
    path = cache_path(directory)
    with _lock:
        if _cache is not None and _cache_path == path:
            return _cache
        if not os.path.exists(path):
            return None
        rates = {}
        try:
            with open(path, encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    try:
                        on = dt.datetime.strptime(row["date"].strip(), _DATE).date()
                    except (ValueError, KeyError, AttributeError):
                        continue
                    day = {}
                    for cur in money.TARGETS:
                        text = (row.get(cur) or "").strip()
                        if text:
                            try:
                                day[cur] = Decimal(text)
                            except Exception:
                                continue
                    if day:
                        rates[on] = day
        except (OSError, csv.Error):
            return None
        if not rates:
            return None
        _cache, _cache_path = rates, path
        return _cache


def reset():
    """Forget the in-memory cache. For tests and after a refresh."""
    global _cache, _cache_path
    with _lock:
        _cache, _cache_path = None, None


def available(directory=None):
    return load(directory) is not None


def rate(on, currency, directory=None):
    """(rate, date_the_rate_is_from) for converting EUR on `on`.

    Walks back to the most recent published business day, up to
    MAX_LOOKBACK_DAYS. The returned date is not decoration -- it is what lets
    the output file say which day's rate a weekend purchase was converted at.
    """
    if currency == money.BASE:
        return Decimal(1), on
    if currency not in money.TARGETS:
        raise RateError(f"not a currency this app converts to: {currency}")

    rates = load(directory)
    if rates is None:
        raise RateError("no rate cache; run a refresh first")

    if isinstance(on, dt.datetime):
        on = on.date()
    newest = max(rates)
    if on > newest:
        # A purchase dated after the last published rate. Refusing is the
        # honest answer: converting tomorrow's spending at today's rate is a
        # guess, and a tracker that guesses is worse than one that says wait.
        raise RateError(f"{on} is later than the newest rate ({newest}); "
                        f"refresh, or check the transaction date")

    for back in range(MAX_LOOKBACK_DAYS + 1):
        day = on - dt.timedelta(days=back)
        found = rates.get(day)
        if found and currency in found:
            return found[currency], day
    raise RateError(f"no {currency} rate within {MAX_LOOKBACK_DAYS} days "
                    f"before {on}")


def convert(cents, on, currency, directory=None):
    """(converted_cents, rate, rate_date). The one call the ledger needs."""
    value, used = rate(on, currency, directory)
    return money.convert(cents, value), value, used


def convert_all(cents, on, directory=None):
    """{currency: {"cents", "rate", "used", "lag_days", "why"}} for every target.

    One conversion attempt per currency, with the failure handling in one
    place. Both ledger and budgets had their own loop over TARGETS doing this,
    and the two had already drifted: one caught (RateError, MoneyError) and the
    other also caught ValueError, so the same bad date failed differently
    depending on which screen you were looking at.

    A currency that could not be converted comes back with cents None and a
    reason, never a zero. Zero in a money column is indistinguishable from a
    free purchase.
    """
    out = {}
    for currency in money.TARGETS:
        try:
            cents_out, value, used = convert(cents, on, currency, directory)
        except (RateError, money.MoneyError, ValueError) as problem:
            out[currency] = {"cents": None, "rate": None, "used": None,
                             "lag_days": None, "why": str(problem)}
            continue
        as_date = on.date() if isinstance(on, dt.datetime) else on
        out[currency] = {"cents": cents_out, "rate": value, "used": used,
                         "lag_days": (as_date - used).days, "why": ""}
    return out


def coverage(directory=None):
    """What the cache holds, for the page footer and for tests."""
    rates = load(directory)
    if rates is None:
        return {"available": False}
    days = sorted(rates)
    return {
        "available": True,
        "days": len(days),
        "first": days[0].isoformat(),
        "last": days[-1].isoformat(),
        "currencies": sorted(money.TARGETS),
    }
