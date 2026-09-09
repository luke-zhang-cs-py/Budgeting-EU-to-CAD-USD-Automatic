"""ECB rates: caching, and the business-day rule.

The gap handling is the reason this module exists rather than a one-line
lookup. The ECB publishes no weekend rows at all, and its longest gaps are
four calendar days of silence -- Easter and Christmas. Reaching for
"yesterday's rate" is wrong several times a year and wrong quietly, because
the lookup simply misses and something downstream fills in a zero.
"""
import csv
import datetime as dt
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetch      # noqa: E402
import fxrates  # noqa: E402
import money    # noqa: E402


@pytest.fixture(autouse=True)
def fresh():
    fxrates.reset()
    yield
    fxrates.reset()


def write_cache(directory, rows):
    """A cache file holding exactly `rows` -- {date_string: (cad, usd)}."""
    path = fxrates.cache_path(str(directory))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date"] + list(money.TARGETS))
        for day in sorted(rows, reverse=True):
            cad, usd = rows[day]
            writer.writerow([day, cad, usd])
    return path


# A slice of the real series around Easter 2026, which is the longest gap in
# the whole file: 2 April, then nothing until 7 April.
EASTER_2026 = {
    "2026-04-07": ("1.6100", "1.1700"),
    "2026-04-02": ("1.6033", "1.1614"),
    "2026-04-01": ("1.6000", "1.1600"),
}


# ------------------------------------------------------------------ absence

def test_no_cache_is_reported_not_guessed(tmp_path):
    """A clone has no cache until something refreshes. That must be a clear
    message, not a conversion at some default rate."""
    assert fxrates.load(str(tmp_path)) is None
    assert fxrates.available(str(tmp_path)) is False
    assert fxrates.coverage(str(tmp_path)) == {"available": False}
    with pytest.raises(fxrates.RateError):
        fxrates.rate(dt.date(2026, 4, 7), "CAD", str(tmp_path))


def test_a_corrupt_cache_is_absent_not_fatal(tmp_path):
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("date,CAD,USD\nnot-a-date,,\n")
    assert fxrates.load(str(tmp_path)) is None


def test_a_failed_refresh_returns_false_rather_than_raising(tmp_path,
                                                            monkeypatch):
    """The app stays useful offline against the last cache, so a network blip
    must not take the page down."""
    monkeypatch.setattr(fetch, "get", lambda *a, **k: None)
    assert fxrates.refresh(str(tmp_path)) is False


# -------------------------------------------------------- the business days

def test_a_weekday_uses_its_own_rate(tmp_path):
    write_cache(tmp_path, EASTER_2026)
    value, used = fxrates.rate(dt.date(2026, 4, 2), "CAD", str(tmp_path))
    assert value == Decimal("1.6033")
    assert used == dt.date(2026, 4, 2)


def test_a_weekend_purchase_uses_the_preceding_business_day(tmp_path):
    """Saturday 4 April 2026 has no rate. Thursday 2 April is the last one
    published before it, because Good Friday was a holiday too."""
    write_cache(tmp_path, EASTER_2026)
    value, used = fxrates.rate(dt.date(2026, 4, 4), "CAD", str(tmp_path))
    assert used == dt.date(2026, 4, 2)
    assert value == Decimal("1.6033")


def test_the_longest_real_gap_is_crossed(tmp_path):
    """Easter Sunday 2026, four days after the last published rate. This is
    the case that breaks any implementation reaching for yesterday."""
    write_cache(tmp_path, EASTER_2026)
    value, used = fxrates.rate(dt.date(2026, 4, 6), "CAD", str(tmp_path))
    assert used == dt.date(2026, 4, 2)
    assert (dt.date(2026, 4, 6) - used).days == 4


def test_it_never_reaches_forward_for_a_nearer_rate(tmp_path):
    """5 April is one day from the 7th and four from the 2nd, but the 7th had
    not happened yet when the money was spent. Using it would be hindsight,
    and it would change a past total every time the file is refreshed."""
    write_cache(tmp_path, EASTER_2026)
    _value, used = fxrates.rate(dt.date(2026, 4, 5), "CAD", str(tmp_path))
    assert used == dt.date(2026, 4, 2)
    assert used < dt.date(2026, 4, 5)


def test_a_date_after_the_newest_rate_is_refused(tmp_path):
    """Converting tomorrow's spending at today's rate is a guess. Saying so
    is more useful than a number that looks real."""
    write_cache(tmp_path, EASTER_2026)
    with pytest.raises(fxrates.RateError) as caught:
        fxrates.rate(dt.date(2026, 4, 8), "CAD", str(tmp_path))
    assert "newer" in str(caught.value) or "later" in str(caught.value)


def test_a_gap_longer_than_the_lookback_is_refused(tmp_path):
    """Better to say there is no rate than to convert January at a rate from
    the previous November."""
    write_cache(tmp_path, {"2026-04-07": ("1.61", "1.17")})
    with pytest.raises(fxrates.RateError):
        fxrates.rate(dt.date(2026, 4, 7) +
                     dt.timedelta(days=fxrates.MAX_LOOKBACK_DAYS + 1),
                     "CAD", str(tmp_path))


def test_lookback_covers_the_measured_worst_case():
    """Five days is the longest gap in the ECB series. The limit has to be
    comfortably past it, or holidays become gaps in the ledger."""
    assert fxrates.MAX_LOOKBACK_DAYS >= 5


# ------------------------------------------------------------- conversion

def test_euro_converts_to_itself_at_one(tmp_path):
    value, used = fxrates.rate(dt.date(2026, 4, 4), "EUR", str(tmp_path))
    assert value == 1
    assert used == dt.date(2026, 4, 4)


def test_convert_reports_the_rate_and_the_date_it_used(tmp_path):
    """The three things the output file needs from one call."""
    write_cache(tmp_path, EASTER_2026)
    cents, value, used = fxrates.convert(5000, dt.date(2026, 4, 5), "CAD",
                                         str(tmp_path))
    assert cents == 8017          # EUR 50.00 -> CA$80.17
    assert value == Decimal("1.6033")
    assert used == dt.date(2026, 4, 2)


def test_an_unsupported_currency_is_refused(tmp_path):
    write_cache(tmp_path, EASTER_2026)
    with pytest.raises(fxrates.RateError):
        fxrates.rate(dt.date(2026, 4, 2), "GBP", str(tmp_path))


def test_a_datetime_is_accepted_as_well_as_a_date(tmp_path):
    """Importers hand over whatever the CSV had."""
    write_cache(tmp_path, EASTER_2026)
    _v, used = fxrates.rate(dt.datetime(2026, 4, 4, 15, 30), "USD",
                            str(tmp_path))
    assert used == dt.date(2026, 4, 2)


# ------------------------------------------------------------------ caching

def test_the_cache_is_read_once(tmp_path, monkeypatch):
    write_cache(tmp_path, EASTER_2026)
    reads = []
    real = open

    def counting(*args, **kwargs):
        if args and str(args[0]).endswith(fxrates.CACHE_NAME):
            reads.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr("builtins.open", counting)
    for _ in range(5):
        fxrates.rate(dt.date(2026, 4, 2), "CAD", str(tmp_path))
    assert len(reads) == 1


def test_coverage_describes_the_cache(tmp_path):
    write_cache(tmp_path, EASTER_2026)
    report = fxrates.coverage(str(tmp_path))
    assert report["available"] is True
    assert report["days"] == 3
    assert report["first"] == "2026-04-01"
    assert report["last"] == "2026-04-07"
    assert report["currencies"] == ["CAD", "USD"]


def test_a_refresh_writes_a_readable_cache(tmp_path, monkeypatch):
    """The whole round trip, against a miniature of the real zip."""
    import io as _io
    import zipfile

    payload = _io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        # Trailing comma and an N/A, both of which the real file contains.
        archive.writestr("eurofxref-hist.csv",
                         "Date, USD, CAD, CYP,\n"
                         "2026-04-07, 1.1700, 1.6100, N/A,\n"
                         "2026-04-02, 1.1614, 1.6033, N/A,\n")
    monkeypatch.setattr(fetch, "get", lambda *a, **k: payload.getvalue())

    assert fxrates.refresh(str(tmp_path)) is True
    value, used = fxrates.rate(dt.date(2026, 4, 5), "CAD", str(tmp_path))
    assert value == Decimal("1.6033")
    assert used == dt.date(2026, 4, 2)
    assert fxrates.coverage(str(tmp_path))["days"] == 2
