"""The live rate: reading two sources, refusing nonsense, and caching.

No test here touches the network. Both real response bodies are pinned as
fixtures, because the useful thing to test is what happens when a source
changes its shape or starts returning something implausible -- and a live
fetch would test the internet instead.
"""
import datetime as dt
import json
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetch     # noqa: E402
import fxlive    # noqa: E402

# Real bodies, trimmed. The two sources disagree about where the date lives,
# which is the reason _published reads rather than assumes.
FRANKFURTER = json.dumps({
    "amount": 1.0, "base": "EUR", "date": "2026-09-09",
    "rates": {"CAD": 1.6043, "USD": 1.1652},
}).encode()

ER_API = json.dumps({
    "result": "success", "base_code": "EUR",
    "time_last_update_utc": "Wed, 09 Sep 2026 00:02:31 +0000",
    "rates": {"EUR": 1, "CAD": 1.602409, "USD": 1.162415, "GBP": 0.84},
}).encode()

NOW = dt.datetime(2026, 9, 9, 14, 3, 0)


@pytest.fixture(autouse=True)
def fresh():
    fxlive.reset()
    yield
    fxlive.reset()


def answering(*bodies):
    """A fetch.get that returns each body in turn, then None."""
    queue = list(bodies)

    def get(_url, _timeout=None):
        return queue.pop(0) if queue else None
    return get


# ---------------------------------------------------------------- reading

def test_the_first_source_is_read(monkeypatch):
    monkeypatch.setattr(fetch, "get", answering(FRANKFURTER))
    got = fxlive.quote(now=NOW)
    assert got["rates"]["CAD"] == Decimal("1.6043")
    assert got["published"] == "2026-09-09"
    assert got["source"] == "frankfurter"
    assert got["fetched_at"] == "2026-09-09T14:03:00"


def test_rates_are_decimals_not_floats(monkeypatch):
    """A rate multiplied into an amount decides what somebody is charged.
    money.convert takes Decimal for exactly this reason."""
    monkeypatch.setattr(fetch, "get", answering(FRANKFURTER))
    for value in fxlive.quote(now=NOW)["rates"].values():
        assert isinstance(value, Decimal)


def test_the_second_source_is_used_when_the_first_is_silent(monkeypatch):
    monkeypatch.setattr(fetch, "get", answering(None, ER_API))
    got = fxlive.quote(now=NOW)
    assert got["source"] == "er-api"
    assert got["rates"]["CAD"] == Decimal("1.602409")


def test_the_other_shape_of_date_is_read(monkeypatch):
    """er-api gives an RFC-1123 timestamp where frankfurter gives a date."""
    monkeypatch.setattr(fetch, "get", answering(ER_API))
    assert fxlive.quote(now=NOW)["published"] == "2026-09-09"


def test_currencies_this_app_does_not_use_are_dropped(monkeypatch):
    """er-api returns 160 currencies. Only the two targets are kept, so a
    caller cannot come to depend on a rate the rest of the app has no
    history for."""
    monkeypatch.setattr(fetch, "get", answering(ER_API))
    assert set(fxlive.quote(now=NOW)["rates"]) == {"CAD", "USD"}


def test_no_source_answering_is_none_not_an_exception(monkeypatch):
    """Every caller is drawing a page that works without a live rate. "Not
    right now" is a legitimate answer and the interface shows it as one."""
    monkeypatch.setattr(fetch, "get", answering())
    assert fxlive.quote(now=NOW) is None


# --------------------------------------------------------------- nonsense

@pytest.mark.parametrize("body,why", [
    (b"<html>502 Bad Gateway</html>", "an error page instead of JSON"),
    (b"[1, 2, 3]", "JSON that is not an object"),
    (b"{}", "no rates at all"),
    (json.dumps({"rates": "nope"}).encode(), "rates that are not a mapping"),
    (json.dumps({"rates": {"CAD": None}}).encode(), "a null rate"),
    (json.dumps({"rates": {"CAD": "abc"}}).encode(), "an unreadable rate"),
])
def test_a_source_returning_rubbish_is_skipped(body, why, monkeypatch):
    monkeypatch.setattr(fetch, "get", answering(body))
    assert fxlive.quote(now=NOW) is None, why


@pytest.mark.parametrize("rate,why", [
    (0.62, "inverted -- CAD per EUR reported as EUR per CAD"),
    (0, "a placeholder zero"),
    (-1.6, "negative"),
    (160.43, "the decimal point lost"),
])
def test_an_implausible_rate_is_refused(rate, why, monkeypatch):
    """EUR/CAD has spent its whole existence between about 1.2 and 1.8. A
    figure outside the bounds is a source that has changed what it means, and
    using it would convert every estimate wrongly with total confidence."""
    body = json.dumps({"date": "2026-09-09", "rates": {"CAD": rate}}).encode()
    monkeypatch.setattr(fetch, "get", answering(body))
    assert fxlive.quote(now=NOW) is None, why


def test_one_bad_rate_does_not_discard_the_good_one(monkeypatch):
    """CAD is usable, USD is not. Dropping the whole quote would lose a rate
    that was fine."""
    body = json.dumps({"date": "2026-09-09",
                       "rates": {"CAD": 1.6043, "USD": 99}}).encode()
    monkeypatch.setattr(fetch, "get", answering(body))
    got = fxlive.quote(now=NOW)
    assert set(got["rates"]) == {"CAD"}


def test_a_missing_date_is_none_rather_than_today(monkeypatch):
    """Substituting today would claim a publication date the source never
    gave, and the whole value of the field is saying how old the figure is."""
    body = json.dumps({"rates": {"CAD": 1.6043}}).encode()
    monkeypatch.setattr(fetch, "get", answering(body))
    assert fxlive.quote(now=NOW)["published"] is None


# ------------------------------------------------------------------ cache

def test_a_second_ask_does_not_hit_the_network(monkeypatch):
    calls = []

    def counting(_url, _timeout=None):
        calls.append(1)
        return FRANKFURTER

    monkeypatch.setattr(fetch, "get", counting)
    first = fxlive.quote(now=NOW)
    second = fxlive.quote(now=NOW)
    assert first is second
    assert len(calls) == 1, "the underlying figure changes once a day"


def test_the_cache_expires(monkeypatch):
    calls = []

    def counting(_url, _timeout=None):
        calls.append(1)
        return FRANKFURTER

    monkeypatch.setattr(fetch, "get", counting)
    fxlive.quote(now=NOW)

    clock = [0.0]
    monkeypatch.setattr(fxlive.time, "monotonic", lambda: clock[0])
    fxlive.reset()
    fxlive.quote(now=NOW)
    clock[0] = fxlive.CACHE_SECONDS + 1
    fxlive.quote(now=NOW)
    assert len(calls) == 3


def test_a_failure_is_not_cached(monkeypatch):
    """Caching "no rate" would leave the page saying so for a quarter of an
    hour after the network came back."""
    monkeypatch.setattr(fetch, "get", answering(None))
    assert fxlive.quote(now=NOW) is None
    monkeypatch.setattr(fetch, "get", answering(FRANKFURTER))
    assert fxlive.quote(now=NOW) is not None


def test_reset_forgets_the_quote(monkeypatch):
    """Asserted by counting fetches, not by watching the source label
    change: the label names the URL that was tried, so a stub handing back
    bodies in sequence still reports "frankfurter" for the second one. The
    first version of this test asserted the label and was testing the
    stub."""
    calls = []

    def counting(_url, _timeout=None):
        calls.append(1)
        return FRANKFURTER

    monkeypatch.setattr(fetch, "get", counting)
    fxlive.quote(now=NOW)
    fxlive.quote(now=NOW)
    assert len(calls) == 1, "the second ask should have been cached"

    fxlive.reset()
    fxlive.quote(now=NOW)
    assert len(calls) == 2, "reset should have forced a fresh fetch"


# ------------------------------------------------------------- the framing

def test_the_quote_says_it_is_a_daily_reference(monkeypatch):
    """The honesty flag. Both sources republish the ECB's once-a-day fixing,
    there is no free intraday EUR/CAD tick, and a card would not settle at
    one anyway -- Visa converts on the day it processes the purchase. An
    interface calling this "live, to the second" would be misleading, so the
    quote says what it is and the page reads the flag.
    """
    monkeypatch.setattr(fetch, "get", answering(FRANKFURTER))
    assert fxlive.quote(now=NOW)["daily_reference"] is True


def test_the_timeout_is_short_enough_for_a_page():
    """The ECB history download gets two minutes. This is fetched while
    somebody waits, so it must give up quickly and let the page say so."""
    assert fxlive.TIMEOUT <= 10
    assert fxlive.TIMEOUT < fetch.DEFAULT_TIMEOUT


def test_a_timestamp_in_no_recognised_shape_is_no_date(monkeypatch):
    """Both formats are tried and both fail. The rates are still usable --
    only the "as of" label is unknown, and saying nothing beats inventing a
    publication date the source never gave."""
    body = json.dumps({"time_last_update_utc": "sometime last Tuesday",
                       "rates": {"CAD": 1.6043}}).encode()
    monkeypatch.setattr(fetch, "get", answering(body))
    got = fxlive.quote(now=NOW)
    assert got["published"] is None
    assert got["rates"]["CAD"] == Decimal("1.6043")


def test_a_timestamp_without_a_timezone_is_still_read(monkeypatch):
    """The second of the two formats, which exists because not every source
    sends an offset."""
    body = json.dumps({"time_last_update_utc": "Wed, 09 Sep 2026 00:02:31",
                       "rates": {"CAD": 1.6043}}).encode()
    monkeypatch.setattr(fetch, "get", answering(body))
    assert fxlive.quote(now=NOW)["published"] == "2026-09-09"
