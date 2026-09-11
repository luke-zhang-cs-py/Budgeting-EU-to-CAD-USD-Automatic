"""The watched folder, and what makes it safe to run unattended.

Automatic import is only safe because the duplicate rule came first. These
tests are mostly about that: the same file arriving twice, a wider re-export
arriving later, and a malformed one that must not be retried every minute for
the rest of the year.
"""
import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db        # noqa: E402
import fxrates   # noqa: E402
import ledger    # noqa: E402
import sources   # noqa: E402

EUROS = ("Date,Description,Amount,Currency\n"
         "2026-09-02,REWE SAGT DANKE,-52.30,EUR\n"
         "2026-09-02,CAFE NERO,-3.50,EUR\n")

WIDER = EUROS + "2026-09-04,DB VERTRIEB,-39.90,EUR\n"


@pytest.fixture
def wallet(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    monkeypatch.delenv("WALLET_INBOX", raising=False)
    fxrates.reset()
    sources.reset()
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("date,CAD,USD\n2026-09-04,1.6040,1.1620\n"
                     "2026-09-02,1.6033,1.1614\n")
    inbox = sources.inbox_dir(str(tmp_path))
    os.makedirs(inbox, exist_ok=True)
    connection = db.connect(str(tmp_path))
    yield connection, str(tmp_path), inbox
    connection.close()
    sources.stop_watching()
    sources.reset()
    fxrates.reset()


def drop(inbox, name, text):
    path = os.path.join(inbox, name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


# ------------------------------------------------------------ what it reads

def test_a_dropped_file_is_imported(wallet):
    conn, directory, inbox = wallet
    drop(inbox, "cibc.csv", EUROS)
    results = sources.scan(conn, directory)
    assert len(results) == 1
    assert results[0]["status"] == "imported"
    assert results[0]["added"] == 2
    assert len(ledger.transactions(conn, directory=directory)) == 2


def test_the_same_file_is_not_imported_twice(wallet):
    """The folder is swept every minute. Without this it would re-read the
    same export sixty times an hour."""
    conn, directory, inbox = wallet
    drop(inbox, "cibc.csv", EUROS)
    assert sources.scan(conn, directory)[0]["added"] == 2
    assert sources.scan(conn, directory) == []
    assert len(ledger.transactions(conn, directory=directory)) == 2


def test_renaming_a_file_does_not_make_it_new(wallet):
    """Identity is the contents, not the name -- a synced folder renames
    things, and "cibc (1).csv" is not a second statement."""
    conn, directory, inbox = wallet
    drop(inbox, "cibc.csv", EUROS)
    sources.scan(conn, directory)
    drop(inbox, "cibc (1).csv", EUROS)
    assert sources.scan(conn, directory) == []


def test_a_wider_re_export_adds_only_its_new_rows(wallet):
    """The behaviour the whole design rests on. You re-download from CIBC
    with a longer date range; the overlap must not double."""
    conn, directory, inbox = wallet
    drop(inbox, "cibc.csv", EUROS)
    sources.scan(conn, directory)

    drop(inbox, "cibc.csv", WIDER)
    again = sources.scan(conn, directory)
    assert again[0]["added"] == 1
    assert again[0]["duplicate"] == 2
    assert len(ledger.transactions(conn, directory=directory)) == 3


def test_only_statement_shaped_files_are_read(wallet):
    """A synced folder has other things in it."""
    conn, directory, inbox = wallet
    drop(inbox, "holiday.jpg", "not a statement")
    drop(inbox, "notes.md", "not a statement")
    drop(inbox, "cibc.csv", EUROS)
    results = sources.scan(conn, directory)
    assert [r["file"] for r in results] == ["cibc.csv"]


def test_an_empty_file_is_skipped(wallet):
    """A sync in progress leaves a zero-byte placeholder. Reading it would
    record a digest and then never look again once the real file lands."""
    conn, directory, inbox = wallet
    drop(inbox, "syncing.csv", "")
    assert sources.scan(conn, directory) == []


def test_a_missing_folder_is_not_an_error(wallet):
    """The point of the folder is that it lives in a synced directory, which
    may not be mounted yet."""
    conn, directory, _inbox = wallet
    assert sources.candidates(str(os.path.join(directory, "absent"))) == []


def test_files_are_read_oldest_first(wallet):
    conn, directory, inbox = wallet
    first = drop(inbox, "a-august.csv",
                 "Date,Description,Amount\n2026-08-02,AUGUST,-10.00\n")
    second = drop(inbox, "b-september.csv", EUROS)
    old = dt.datetime(2026, 8, 1).timestamp()
    os.utime(first, (old, old))
    order = [name for _path, name in sources.candidates(directory)]
    assert order == ["a-august.csv", "b-september.csv"]
    assert os.path.exists(second)


# --------------------------------------------------------- what it refuses

def test_a_file_that_cannot_be_parsed_is_recorded_and_not_retried(wallet):
    """Otherwise a malformed export is re-read every minute forever, and
    re-reading it will not make it parse."""
    conn, directory, inbox = wallet
    drop(inbox, "garbage.csv", "this is not a statement at all\n")
    first = sources.scan(conn, directory)
    assert first[0]["status"] == "rejected"
    assert first[0]["why"]
    assert sources.scan(conn, directory) == [], "it was retried"


def test_a_rejected_file_appears_in_the_history(wallet):
    """Silently ignoring it would leave somebody waiting for an import that
    is never going to happen."""
    conn, directory, inbox = wallet
    drop(inbox, "garbage.csv", "this is not a statement at all\n")
    sources.scan(conn, directory)
    entry = sources.history(conn)[0]
    assert entry["filename"] == "garbage.csv"
    assert entry["unreadable"] == 1
    assert entry["added"] == 0


def test_the_file_is_never_moved_or_deleted(wallet):
    """It is the user's folder, and a synced one. Deleting an import would
    delete it from their phone too."""
    conn, directory, inbox = wallet
    path = drop(inbox, "cibc.csv", EUROS)
    sources.scan(conn, directory)
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as handle:
        assert handle.read() == EUROS


# ------------------------------------------------------------- the history

def test_the_history_reads_newest_first(wallet):
    conn, directory, inbox = wallet
    drop(inbox, "one.csv", EUROS)
    sources.scan(conn, directory)
    drop(inbox, "two.csv",
         "Date,Description,Amount\n2026-09-05,LIDL,-22.10\n")
    sources.scan(conn, directory)
    assert [h["filename"] for h in sources.history(conn)] == ["two.csv",
                                                              "one.csv"]


def test_the_history_can_be_limited(wallet):
    conn, directory, inbox = wallet
    for n in range(4):
        drop(inbox, f"f{n}.csv",
             f"Date,Description,Amount\n2026-09-0{n + 1},SHOP {n},-1.00\n")
        sources.scan(conn, directory)
    assert len(sources.history(conn, limit=2)) == 2


# -------------------------------------------------------------- the status

def test_the_status_says_where_it_is_watching(wallet):
    conn, directory, inbox = wallet
    report = sources.status(directory)
    assert report["folder"] == inbox
    assert report["folderExists"] is True
    assert report["watching"] is False
    assert report["files"] == 0
    assert report["unread"] is None, (
        "without a connection it cannot know, and says so")


def test_the_status_counts_the_files_in_the_folder(wallet):
    conn, directory, inbox = wallet
    drop(inbox, "cibc.csv", EUROS)
    assert sources.status(directory)["files"] == 1


def test_an_imported_file_stops_counting_as_unread(wallet):
    """The bug this split exists for. Files are never moved or deleted, so a
    count of what is in the folder said "1 waiting" forever after a
    successful import -- and the Scan now button makes people look at it.
    """
    conn, directory, inbox = wallet
    drop(inbox, "cibc.csv", EUROS)

    before = sources.status(directory, conn)
    assert before["files"] == 1
    assert before["unread"] == 1

    sources.scan(conn, directory)

    after = sources.status(directory, conn)
    assert after["files"] == 1, "the file is still there, as promised"
    assert after["unread"] == 0, "but it is no longer waiting to be read"


def test_an_edited_file_counts_as_unread_again(wallet):
    """Identity is the contents, so re-exporting with a wider date range is a
    different file and is read again -- its overlapping rows caught by the
    transaction fingerprint."""
    conn, directory, inbox = wallet
    drop(inbox, "cibc.csv", EUROS)
    sources.scan(conn, directory)
    assert sources.status(directory, conn)["unread"] == 0

    drop(inbox, "cibc.csv", EUROS + "2026-02-09,ALDI,-12.00\n")
    assert sources.status(directory, conn)["unread"] == 1


def test_the_inbox_can_be_pointed_somewhere_else(tmp_path, monkeypatch):
    """The whole point is aiming it at iCloud Drive or OneDrive."""
    monkeypatch.setenv("WALLET_INBOX", str(tmp_path / "somewhere-synced"))
    assert sources.inbox_dir() == str(tmp_path / "somewhere-synced")


# -------------------------------------------------------------- the sweeper

def test_a_sweep_imports_and_records_what_it_did(wallet):
    conn, directory, inbox = wallet
    drop(inbox, "cibc.csv", EUROS)
    results = sources.sweep(directory)
    assert results[0]["added"] == 2
    last = sources.status(directory)["last"]
    assert last["added"] == 2
    assert last["files"] == 1
    assert last["at"]


def test_a_sweep_that_raises_does_not_stop_the_watching(wallet, monkeypatch):
    """A background sweep must not take the process down, and must not kill
    the timer -- watching that has silently stopped is the worst outcome,
    because the folder looks like it is being read and is not."""
    conn, directory, _inbox = wallet

    def explode(*_a, **_k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(sources, "sweep", explode)
    assert sources.start_watching(directory, interval=0.01) is True
    import time
    time.sleep(0.15)
    assert sources.status(directory)["watching"] is True
    assert sources.stop_watching() is True


def test_watching_starts_once_and_stops(wallet):
    conn, directory, _inbox = wallet
    assert sources.start_watching(directory, interval=30) is True
    assert sources.start_watching(directory, interval=30) is False, "started twice"
    assert sources.status(directory)["watching"] is True
    assert sources.stop_watching() is True
    assert sources.status(directory)["watching"] is False
    assert sources.stop_watching() is False


def test_the_timer_actually_imports(wallet):
    """Not just that a thread starts -- that a file dropped after it started
    lands in the ledger without anybody asking."""
    conn, directory, inbox = wallet
    drop(inbox, "cibc.csv", EUROS)
    sources.start_watching(directory, interval=0.05)
    import time
    for _ in range(40):
        time.sleep(0.05)
        if sources.status(directory)["last"]["files"]:
            break
    sources.stop_watching()
    assert sources.status(directory)["last"]["added"] == 2
    with db.session(directory) as fresh:
        assert len(ledger.transactions(fresh, directory=directory)) == 2


# --------------------------------------------------- the remaining branches

def test_a_directory_in_the_inbox_is_skipped(wallet):
    """iCloud and OneDrive both put folders in there."""
    conn, directory, inbox = wallet
    os.makedirs(os.path.join(inbox, "a-subfolder.csv"), exist_ok=True)
    assert sources.candidates(directory) == []


def test_a_file_that_vanishes_mid_scan_is_skipped(wallet, monkeypatch):
    """A synced folder is written to by another process. Between listing the
    directory and stat-ing a name, the file can be gone."""
    conn, directory, inbox = wallet
    drop(inbox, "cibc.csv", EUROS)

    def vanish(*_a, **_k):
        raise OSError("gone")

    monkeypatch.setattr(sources.os, "stat", vanish)
    assert sources.candidates(directory) == []


def test_a_file_too_large_to_be_a_statement_is_skipped(wallet, monkeypatch):
    conn, directory, inbox = wallet
    drop(inbox, "huge.csv", EUROS)
    monkeypatch.setattr(sources, "MAX_BYTES", 10)
    assert sources.candidates(directory) == []


def test_a_file_that_cannot_be_opened_is_reported_not_fatal(wallet,
                                                            monkeypatch):
    """Locked by the syncing client, most likely. Reported so it is visible,
    and not recorded, so the next sweep tries again -- unlike a parse failure,
    this one may well succeed in a minute."""
    conn, directory, inbox = wallet
    drop(inbox, "cibc.csv", EUROS)
    real = open

    def refuse(*args, **kwargs):
        if args and str(args[0]).endswith("cibc.csv"):
            raise OSError("locked by another process")
        return real(*args, **kwargs)

    monkeypatch.setattr("builtins.open", refuse)
    results = sources.scan(conn, directory)
    assert results[0]["status"] == "unreadable"
    assert "locked" in results[0]["why"]
    assert sources.history(conn) == [], "a lock should not be recorded"


def test_stopping_before_the_first_tick_does_not_arm_a_timer(wallet):
    """stop_watching can land between arm() being called and the timer being
    stored. It must not leave one running."""
    conn, directory, _inbox = wallet
    sources.start_watching(directory, interval=30)
    sources.stop_watching()
    assert sources.status(directory)["watching"] is False
    assert sources._running["timer"] is None


def test_a_stop_during_a_sweep_does_not_arm_another_timer(wallet,
                                                          monkeypatch):
    """The race: stop_watching lands while a sweep is running, so the sweep
    finishes and would otherwise reschedule itself into a watcher nobody
    asked for."""
    conn, directory, _inbox = wallet
    stopped = []

    def sweep_then_stop(*_a, **_k):
        sources.stop_watching()
        stopped.append(True)

    monkeypatch.setattr(sources, "sweep", sweep_then_stop)
    sources.start_watching(directory, interval=0.01)
    import time
    for _ in range(50):
        time.sleep(0.02)
        if stopped:
            break
    time.sleep(0.05)
    assert stopped, "the sweep never ran"
    assert sources._running["timer"] is None, "it rearmed after being stopped"
    assert sources.status(directory)["watching"] is False
