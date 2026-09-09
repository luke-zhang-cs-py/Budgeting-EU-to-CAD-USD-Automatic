"""The defensive branches: malformed input, a failed download, a corrupt cache.

These are the paths that decide whether a bad day degrades or crashes, and
they are exactly the ones no happy-path test reaches. Every one is written
against a real failure mode -- a proxy that breaks TLS, an ECB file with an
"N/A" in it, a cache half-written by an interrupted run.
"""
import csv
import datetime as dt
import io
import os
import subprocess
import sys
import urllib.error
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fxrates   # noqa: E402
import ledger    # noqa: E402
import money     # noqa: E402


@pytest.fixture(autouse=True)
def fresh():
    fxrates.reset()
    yield
    fxrates.reset()


def zipped(csv_text, name="eurofxref-hist.csv"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, csv_text)
    return buffer.getvalue()


# ==========================================================================
#  _download -- urllib, then curl, then give up
# ==========================================================================

def test_urllib_is_tried_first_and_its_bytes_are_used(monkeypatch):
    class Response:
        def read(self):
            return b"payload"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(fxrates.urllib.request, "urlopen",
                        lambda *a, **k: Response())
    called = []
    monkeypatch.setattr(fxrates.subprocess, "run",
                        lambda *a, **k: called.append(1))
    assert fxrates._download("https://example.invalid/x.zip") == b"payload"
    assert not called, "curl was run even though urllib worked"


def test_curl_is_the_fallback_when_urllib_cannot_verify_the_chain(monkeypatch):
    """The real reason this ladder exists: on a machine behind an inspecting
    proxy, urllib rejects the certificate chain and curl does not."""
    def refuse(*_a, **_k):
        raise urllib.error.URLError("certificate verify failed")

    class Done:
        returncode = 0
        stdout = b"from curl"

    monkeypatch.setattr(fxrates.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(fxrates.subprocess, "run", lambda *a, **k: Done())
    assert fxrates._download("https://example.invalid/x.zip") == b"from curl"


@pytest.mark.parametrize("returncode,stdout", [(1, b""), (0, b""), (7, b"x")])
def test_a_curl_that_fails_or_returns_nothing_is_not_a_download(monkeypatch,
                                                                returncode,
                                                                stdout):
    def refuse(*_a, **_k):
        raise OSError("no network")

    class Done:
        pass

    Done.returncode = returncode
    Done.stdout = stdout
    monkeypatch.setattr(fxrates.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(fxrates.subprocess, "run", lambda *a, **k: Done())
    assert fxrates._download("https://example.invalid/x.zip") is None


def test_curl_missing_entirely_is_not_a_crash(monkeypatch):
    """A machine with no curl on PATH. Returning None lets the caller say the
    refresh failed; an OSError here would take the page down."""
    def refuse(*_a, **_k):
        raise OSError("nope")

    monkeypatch.setattr(fxrates.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(fxrates.subprocess, "run", refuse)
    assert fxrates._download("https://example.invalid/x.zip") is None


def test_a_curl_timeout_is_not_a_crash(monkeypatch):
    def refuse(*_a, **_k):
        raise urllib.error.URLError("x")

    def expire(*_a, **_k):
        raise subprocess.TimeoutExpired("curl", 1)

    monkeypatch.setattr(fxrates.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(fxrates.subprocess, "run", expire)
    assert fxrates._download("https://example.invalid/x.zip") is None


# ==========================================================================
#  _parse_history -- the ECB file's real quirks
# ==========================================================================

def test_a_file_missing_a_currency_column_is_refused(monkeypatch, tmp_path):
    """If the ECB ever drops CAD, silently producing a USD-only cache would
    leave half the app blank with no explanation."""
    monkeypatch.setattr(fxrates, "_download",
                        lambda *a, **k: zipped("Date, USD,\n2026-09-08, 1.16,\n"))
    assert fxrates.refresh(str(tmp_path)) is False


def test_rows_with_no_date_are_skipped(monkeypatch, tmp_path):
    """The file ends with a trailing blank line, and has a trailing comma on
    every row, so an empty first cell is normal rather than corrupt."""
    monkeypatch.setattr(fxrates, "_download", lambda *a, **k: zipped(
        "Date, USD, CAD,\n"
        "2026-09-08, 1.1614, 1.6033,\n"
        ", , ,\n"
        "\n"))
    assert fxrates.refresh(str(tmp_path)) is True
    assert fxrates.coverage(str(tmp_path))["days"] == 1


def test_a_row_with_an_unreadable_date_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(fxrates, "_download", lambda *a, **k: zipped(
        "Date, USD, CAD,\n"
        "not-a-date, 1.16, 1.60,\n"
        "2026-09-08, 1.1614, 1.6033,\n"))
    assert fxrates.refresh(str(tmp_path)) is True
    report = fxrates.coverage(str(tmp_path))
    assert report["days"] == 1
    assert report["last"] == "2026-09-08"


def test_na_and_blank_values_are_skipped_not_stored_as_zero(monkeypatch,
                                                            tmp_path):
    """The file writes "N/A" for a currency that did not exist yet. Stored as
    zero it would convert every purchase to nothing."""
    monkeypatch.setattr(fxrates, "_download", lambda *a, **k: zipped(
        "Date, USD, CAD,\n"
        "2026-09-08, 1.1614, N/A,\n"
        "2026-09-07, , 1.6033,\n"
        "2026-09-04, 1.1600, 1.6000,\n"))
    assert fxrates.refresh(str(tmp_path)) is True

    # The N/A is skipped rather than stored, so CAD on the 8th falls back to
    # the 7th -- which is the lookback doing its job, not a stored zero.
    value, used = fxrates.rate(dt.date(2026, 9, 8), "CAD", str(tmp_path))
    assert used == dt.date(2026, 9, 7)
    assert str(value) == "1.6033"

    # The blank USD on the 7th is skipped the same way; the 8th has its own.
    value, used = fxrates.rate(dt.date(2026, 9, 8), "USD", str(tmp_path))
    assert used == dt.date(2026, 9, 8)
    assert str(value) == "1.1614"
    value, used = fxrates.rate(dt.date(2026, 9, 7), "USD", str(tmp_path))
    assert used == dt.date(2026, 9, 4), "the blank USD row should be skipped"


def test_a_value_that_is_not_a_number_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(fxrates, "_download", lambda *a, **k: zipped(
        "Date, USD, CAD,\n"
        "2026-09-08, 1.1614, not-a-rate,\n"
        "2026-09-07, 1.1600, 1.6000,\n"))
    assert fxrates.refresh(str(tmp_path)) is True
    _value, used = fxrates.rate(dt.date(2026, 9, 8), "CAD", str(tmp_path))
    assert used == dt.date(2026, 9, 7), "the bad row should not have been kept"


def test_a_short_row_does_not_raise_an_index_error(monkeypatch, tmp_path):
    """A truncated line in the middle of the file."""
    monkeypatch.setattr(fxrates, "_download", lambda *a, **k: zipped(
        "Date, USD, CAD,\n"
        "2026-09-08, 1.1614\n"
        "2026-09-07, 1.1600, 1.6000,\n"))
    assert fxrates.refresh(str(tmp_path)) is True
    assert fxrates.coverage(str(tmp_path))["days"] >= 1


# ==========================================================================
#  refresh -- what a bad download does
# ==========================================================================

def test_something_that_is_not_a_zip_is_reported_not_raised(monkeypatch,
                                                            tmp_path):
    """An error page served with a 200, which is what a captive portal does."""
    monkeypatch.setattr(fxrates, "_download",
                        lambda *a, **k: b"<html>not a zip</html>")
    assert fxrates.refresh(str(tmp_path)) is False
    assert fxrates.available(str(tmp_path)) is False


def test_a_zip_with_no_usable_rows_does_not_replace_the_cache(monkeypatch,
                                                              tmp_path):
    """Otherwise a bad refresh wipes a good cache and the app goes blank."""
    monkeypatch.setattr(fxrates, "_download", lambda *a, **k: zipped(
        "Date, USD, CAD,\n2026-09-08, 1.1614, 1.6033,\n"))
    assert fxrates.refresh(str(tmp_path)) is True
    good = fxrates.coverage(str(tmp_path))["days"]

    monkeypatch.setattr(fxrates, "_download", lambda *a, **k: zipped(
        "Date, USD, CAD,\nnot-a-date, , ,\n"))
    assert fxrates.refresh(str(tmp_path)) is False
    assert fxrates.coverage(str(tmp_path))["days"] == good


# ==========================================================================
#  load -- a cache written by an interrupted run
# ==========================================================================

def test_a_cache_row_with_a_bad_rate_is_skipped(tmp_path):
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("date,CAD,USD\n"
                     "2026-09-08,not-a-rate,1.1614\n"
                     "2026-09-07,1.6000,1.1600\n")
    _value, used = fxrates.rate(dt.date(2026, 9, 8), "CAD", str(tmp_path))
    assert used == dt.date(2026, 9, 7)


def test_a_cache_that_cannot_be_read_is_absent_not_fatal(tmp_path,
                                                         monkeypatch):
    """A permissions problem or a file being rewritten underneath us. The app
    is useful without rates; it is not useful having crashed."""
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("date,CAD,USD\n2026-09-08,1.6033,1.1614\n")

    real = open

    def refuse(*args, **kwargs):
        if args and str(args[0]).endswith(fxrates.CACHE_NAME):
            raise OSError("permission denied")
        return real(*args, **kwargs)

    monkeypatch.setattr("builtins.open", refuse)
    assert fxrates.load(str(tmp_path)) is None
    assert fxrates.newest(str(tmp_path)) is None


def test_a_csv_error_while_reading_is_absent_not_fatal(tmp_path, monkeypatch):
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("date,CAD,USD\n2026-09-08,1.6033,1.1614\n")

    def explode(*_a, **_k):
        raise csv.Error("field larger than field limit")

    monkeypatch.setattr(fxrates.csv, "DictReader", explode)
    assert fxrates.load(str(tmp_path)) is None


# ==========================================================================
#  ledger.as_date -- the types an importer actually hands over
# ==========================================================================

def test_a_datetime_is_reduced_to_its_date():
    """Some bank exports carry a timestamp. The ledger stores days, and a
    time component would make two purchases on one day look like different
    dates to the duplicate fingerprint."""
    assert ledger.as_date(dt.datetime(2026, 9, 8, 14, 35, 2)) == \
        dt.date(2026, 9, 8)


def test_a_date_is_passed_through_untouched():
    given = dt.date(2026, 9, 8)
    assert ledger.as_date(given) is given


# ==========================================================================
#  money.parse -- the malformed halves
# ==========================================================================

@pytest.mark.parametrize("text", ["1-2", "1-2.5", "1.2.3-"])
def test_a_stray_sign_inside_the_whole_part_is_refused(text):
    """A minus in the middle is not an amount. It has to raise rather than
    parse to something plausible -- a silently wrong figure in a ledger is
    worse than a refused import row."""
    with pytest.raises(money.MoneyError):
        money.parse(text)


@pytest.mark.parametrize("text", ["1.2-", "1,2-"])
def test_a_stray_sign_inside_the_decimal_part_is_refused(text):
    with pytest.raises(money.MoneyError):
        money.parse(text)


@pytest.mark.parametrize("text", ["-", "+", "+-", "..", ",,"])
def test_something_with_no_digits_at_all_is_refused(text):
    with pytest.raises(money.MoneyError):
        money.parse(text)


# ==========================================================================
#  importers -- the malformed-file branches
# ==========================================================================

def test_a_file_of_nothing_but_delimiters_has_no_rows():
    """Distinct from an empty file: there is text, so the delimiter sniffs
    fine, but every cell is blank once stripped."""
    import importers
    with pytest.raises(importers.ImportProblem) as caught:
        importers.sniff(",,,\n,,,\n")
    assert "no rows" in str(caught.value)


def test_a_column_is_found_by_substring_when_no_header_matches_exactly():
    """Banks decorate their headers. "Booking Date (CET)" is not any known
    name exactly, but it contains one."""
    import importers
    mapping = importers.sniff(
        "Booking Date (CET),Narrative Text,Gross Amount\n"
        "2026-09-08,TESCO,-45.50\n")["mapping"]
    assert mapping["date"] == "Booking Date (CET)"
    assert mapping["description"] == "Narrative Text"
    assert mapping["amount"] == "Gross Amount"


def test_a_zero_in_the_paid_out_column_is_treated_as_empty():
    """Some statements fill both columns and put 0.00 in the unused one, so
    taking "out" on sight would make every deposit a withdrawal."""
    import importers
    text = ("Date,Description,Paid out,Paid in\n"
            "2026-09-08,SALARY,0.00,2500.00\n"
            "2026-09-09,TESCO,45.50,0.00\n")
    rows = importers.preview(importers.sniff(text))["rows"]
    amounts = {row["description"]: row["amount_eur"] for row in rows}
    assert amounts["SALARY"] == 250000
    assert amounts["TESCO"] == -4550


def test_an_unreadable_figure_beside_a_good_one_does_not_stop_the_row():
    """One cell of junk in the unused column. The row still has an amount, so
    reporting it as unreadable would lose a real purchase."""
    import importers
    text = ("Date,Description,Paid out,Paid in\n"
            "2026-09-08,TESCO,45.50,n/a\n")
    out = importers.preview(importers.sniff(text))
    assert out["readable"] == 1
    assert out["rows"][0]["amount_eur"] == -4550


def test_a_row_with_both_amount_columns_empty_is_unreadable():
    import importers
    text = ("Date,Description,Paid out,Paid in\n"
            "2026-09-08,TESCO,,\n"
            "2026-09-09,LIDL,22.10,\n")
    out = importers.preview(importers.sniff(text))
    assert out["readable"] == 1
    assert out["unreadable"] == 1
    assert "either column" in out["problems"][0]["why"]


def test_no_amount_column_at_all_is_refused():
    """Every row fails the same way, which is the signal that the mapping is
    wrong rather than the data.

    Six rows, not one: a per-row failure is reported per row, and it only
    becomes "your mapping is wrong" once there are enough rows for the ratio
    to mean something. One unreadable row out of one says nothing.
    """
    import importers
    text = "when,what\n" + "".join(
        f"2026-09-0{n},SHOP {n}\n" for n in range(1, 7))
    sniffed = importers.sniff(text)
    mapping = dict(sniffed["mapping"], date="when", description="what",
                   amount=None, amount_out=None, amount_in=None)
    with pytest.raises(importers.ImportProblem) as caught:
        importers.preview(sniffed, mapping)
    assert "mapping" in str(caught.value)


def test_one_row_with_no_amount_column_is_a_row_problem_not_a_file_problem():
    """The other side of the same threshold, stated so the behaviour is not
    mistaken for the mapping check failing to fire."""
    import importers
    sniffed = importers.sniff("when,what\n2026-09-08,TESCO\n")
    mapping = dict(sniffed["mapping"], date="when", description="what",
                   amount=None, amount_out=None, amount_in=None)
    out = importers.preview(sniffed, mapping)
    assert out["readable"] == 0
    assert out["unreadable"] == 1
    assert "no amount column" in out["problems"][0]["why"]


def test_a_row_with_an_empty_date_cell_is_unreadable():
    """Different from an unreadable date: the cell is simply blank, which is
    what a subtotal or carried-balance line looks like."""
    import importers
    text = ("Date,Description,Amount\n"
            ",BALANCE CARRIED FORWARD,1200.00\n"
            "2026-09-08,TESCO,-45.50\n")
    out = importers.preview(importers.sniff(text))
    assert out["readable"] == 1
    assert out["problems"][0]["why"] == "no date"


def test_a_row_the_ledger_refuses_is_reported_rather_than_lost(tmp_path,
                                                               monkeypatch):
    """preview and add validate separately, so add can still refuse a row
    preview accepted. That must be counted, not swallowed."""
    import db
    import importers
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    conn = db.connect(str(tmp_path))
    try:
        previewed = importers.preview(importers.sniff(
            "Date,Description,Amount\n2026-09-08,TESCO,-45.50\n"))

        def refuse(*_a, **_k):
            raise ValueError("the ledger said no")

        monkeypatch.setattr(ledger, "add", refuse)
        out = importers.load(conn, previewed)
        assert out["added"] == 0
        assert out["failed"] == [{"row": 2, "why": "the ledger said no"}]
    finally:
        conn.close()
