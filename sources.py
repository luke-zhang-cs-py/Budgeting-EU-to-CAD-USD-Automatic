"""
sources.py
----------
Getting purchases in without being asked twice.

Apple Wallet and Google Wallet cannot be read by a desktop app, and Canada has
no live open-banking API yet -- the Consumer-Driven Banking Act got Royal
Assent in March 2026 and CIBC is a mandatory participant, but Phase 1 read
access has no operational date. So the automatic route today is a folder.

You point this at somewhere your phone already syncs -- iCloud Drive,
OneDrive, Dropbox -- and drop a bank export into it. The app notices, reads
it, and adds what is new. Nothing leaves the machine, there are no
credentials, and no third party sees a transaction.

Two properties make that safe to run unattended:

  * **A file is never imported twice.** Recorded by digest of its contents,
    with a UNIQUE constraint, so re-syncing the same export is free.
  * **An updated file is re-read, and only its new rows land.** Re-exporting
    from CIBC with a wider date range produces a different digest, so it is
    processed again -- and the overlapping purchases are caught by the
    transaction fingerprint that was already there. That is the whole reason
    unattended import is safe: the duplicate rule predates it.

Files are never moved or deleted. It is your folder.
"""
import datetime as dt
import hashlib
import os
import threading

import db
import importers
import paths

# Where to watch. Defaults inside the data directory so a fresh clone has
# somewhere to put things, but the point is to aim it at a synced folder.
ENV_VAR = "WALLET_INBOX"
DEFAULT_DIRNAME = "inbox"

# Anything else in a synced folder is not a bank export.
SUFFIXES = (".csv", ".txt", ".tsv")

# A statement larger than this is not a statement.
MAX_BYTES = 8 * 1024 * 1024

# How often the background scan runs. A minute is far more often than a
# statement appears, and a directory listing of a handful of files costs
# nothing; the point is that dropping a file feels immediate.
INTERVAL_SECONDS = 60

_lock = threading.Lock()
# The running background timer, held in a dict so the two nested scopes in
# start_watching can rebind it without a `global` in each.
_running = {"timer": None, "stopping": False}
_last = {"at": None, "files": 0, "added": 0, "problems": []}


def inbox_dir(directory=None):
    """The folder being watched. Resolved per call, never at import."""
    return os.environ.get(ENV_VAR) or os.path.join(
        paths.data_dir(directory), DEFAULT_DIRNAME)


def digest(raw):
    """The identity of a file: its contents, not its name.

    By contents so that renaming or re-downloading the same export is free,
    and so that a file edited in place is correctly seen as new.
    """
    return hashlib.sha256(raw).hexdigest()[:32]


def already_imported(connection, mark):
    row = connection.execute("SELECT id FROM imports WHERE digest = ?",
                             (mark,)).fetchone()
    return row is not None


def record(connection, filename, mark, outcome):
    connection.execute(
        "INSERT INTO imports (filename, digest, added, duplicate, "
        "unreadable, at) VALUES (?, ?, ?, ?, ?, ?)",
        (filename, mark, outcome.get("added", 0), outcome.get("duplicate", 0),
         outcome.get("unreadable", 0),
         dt.datetime.now().isoformat(timespec="seconds")))
    connection.commit()


def candidates(directory=None):
    """The files worth looking at, oldest first.

    Oldest first so that importing several at once processes them in the order
    they arrived, which is the order a reader would expect in the log.
    """
    folder = inbox_dir(directory)
    if not os.path.isdir(folder):
        return []
    found = []
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        if not name.lower().endswith(SUFFIXES):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if stat.st_size == 0 or stat.st_size > MAX_BYTES:
            continue
        found.append((stat.st_mtime, path, name))
    return [(path, name) for _mtime, path, name in sorted(found)]


def scan(connection, directory=None, use_file_categories=False):
    """Import anything new in the folder. Returns one result per file.

    A file that cannot be read is recorded as a problem and left alone rather
    than retried every minute -- a malformed export would otherwise fill the
    log forever, and re-reading it will not make it parse.
    """
    results = []
    for path, name in candidates(directory):
        try:
            with open(path, "rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
        except OSError as bad:
            results.append({"file": name, "status": "unreadable",
                            "why": str(bad)})
            continue

        mark = digest(raw)
        if already_imported(connection, mark):
            continue

        text = raw.decode("utf-8-sig", errors="replace")
        try:
            sniffed = importers.sniff(text)
            previewed = importers.preview(sniffed)
        except importers.ImportProblem as bad:
            # Recorded so it is not retried, and reported so it is visible.
            record(connection, name, mark, {"unreadable": 1})
            results.append({"file": name, "status": "rejected",
                            "why": str(bad)})
            continue

        outcome = importers.load(connection, previewed,
                                 source=f"inbox:{name}",
                                 use_file_categories=use_file_categories)
        outcome["recategorised"] = importers.ledger.apply_rules(connection)
        record(connection, name, mark, outcome)
        results.append({
            "file": name, "status": "imported",
            "added": outcome["added"], "duplicate": outcome["duplicate"],
            "unreadable": outcome["unreadable"],
            "recategorised": outcome["recategorised"],
            "headerless": sniffed["headerless"],
        })
    return results


def history(connection, limit=20):
    """What has been imported, newest first, for the page."""
    return [dict(row) for row in connection.execute(
        "SELECT filename, added, duplicate, unreadable, at FROM imports "
        "ORDER BY id DESC LIMIT ?", (int(limit),))]


def status(directory=None):
    """Whether watching is on, where, and what the last sweep did."""
    folder = inbox_dir(directory)
    with _lock:
        last = dict(_last)
    return {
        "watching": _running["timer"] is not None,
        "folder": folder,
        "folderExists": os.path.isdir(folder),
        "intervalSeconds": INTERVAL_SECONDS,
        "waiting": len(candidates(directory)),
        "last": last,
    }


# ------------------------------------------------------------- the poller

def sweep(directory=None):
    """One scan on its own connection. Safe to call from a thread."""
    with db.session(directory) as connection:
        results = scan(connection, directory)
    with _lock:
        _last.update(
            at=dt.datetime.now().isoformat(timespec="seconds"),
            files=len(results),
            added=sum(r.get("added", 0) for r in results),
            problems=[r for r in results if r["status"] != "imported"])
    return results


def start_watching(directory=None, interval=None):
    """Begin scanning in the background. Idempotent.

    A daemon timer that reschedules itself, so an interpreter exit does not
    wait on it. Started from the app rather than from import: a module that
    spawns a thread and writes to a database when you import it cannot be
    tested.

    The running timer is held in a dict rather than a module global, because
    the rebinding has to happen from two nested scopes and `global` in each of
    them was both noisy and, in one case, declared without assigning.
    """
    every = interval or INTERVAL_SECONDS
    with _lock:
        if _running["timer"] is not None:
            return False
        _running["stopping"] = False

    def tick():
        try:
            sweep(directory)
        except Exception:
            # A background sweep must not take the process down. The problem
            # is visible in status(); raising here would kill the timer and
            # stop the watching silently, which is worse than a failed sweep.
            pass
        arm()

    def arm():
        """Schedule the next sweep, unless we are stopping.

        One check, here, rather than one here and another in tick(): every
        tick arrives through this function, so the second was unreachable
        belt-and-braces. The race this closes is a stop landing between a
        sweep finishing and the next timer being stored, and closing it in one
        place is what makes it testable.
        """
        timer = threading.Timer(every, tick)
        timer.daemon = True
        with _lock:
            if _running["stopping"]:
                return
            _running["timer"] = timer
        timer.start()

    arm()
    return True


def stop_watching():
    with _lock:
        timer = _running["timer"]
        _running["timer"] = None
        _running["stopping"] = True
    if timer is not None:
        timer.cancel()
    return timer is not None


def reset():
    """Forget the last-sweep record. For tests."""
    with _lock:
        _last.update(at=None, files=0, added=0, problems=[])
