# Contributing

## Setup

```bash
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5004
```

The first run fetches the ECB rate history and caches it under `data/`. After
that everything works offline.

## The one thing that will confuse you

`data/` is gitignored — the database *and* the rate cache. So a fresh clone has
no rates, and `fxrates.rate()` raises `RateError` until something refreshes.

That is the state CI runs in, and it is deliberate. Every test that needs rates
writes its own small cache into `tmp_path` rather than relying on a real one:

```python
monkeypatch.setenv("WALLET_DATA", str(tmp_path))
```

Do the same in any new test. A test that quietly depends on the real cache
passes on your machine and fails in CI — that exact bug cost six red runs in
the transit project in this family, and two more were found in the face
project. Check with:

```bash
git clone . /tmp/fresh && cd /tmp/fresh && pytest -q
```

## Rules worth keeping

**Money is integer cents.** Never a float, anywhere. `money.parse` on the way
in, `money.format` on the way out, `money.convert` for the arithmetic. If you
find yourself writing `/ 100` or `toFixed(2)`, the value is in the wrong type
— and a test asserts the browser half does not do it.

**Store euros, derive the rest.** Converted figures are computed at read time
from the transaction date's rate. Storing them would freeze whatever rate was
cached on import day, and there would be no way to tell later whether a number
was wrong or just old. A test checks no `amount_cad` column exists.

**Rates never reach forward.** A purchase is converted at a rate published on
or before the day it was spent. Using a later one is hindsight and makes closed
months drift.

**Nothing untrusted reaches the DOM raw.** Bank descriptions go through
`esc()`. `tests/test_frontend.py` checks it structurally, because the face
project shipped a stored XSS hole doing exactly this.

**A failure is never a silent zero.** An unreadable amount raises, a missing
rate leaves the column blank with a note. Zero in a money column is
indistinguishable from a free purchase, so the ledger would be wrong and
nothing would flag it.

**Nothing read off a screenshot reaches the ledger unseen.** `receipts.parse`
returns what it could not settle in `needs`, and the interface puts it in
front of a person. An OCR misread that books 5230 instead of 52.30 is found a
month later in a total nobody can explain; a screenshot that could not be read
is found immediately.

**The OCR engine stays optional and stays local.** It is imported inside a
function -- the one deferred import in the project, allowed by name in
`tests/test_structure.py` because it costs a measured 1.0s against 0.65s for
the whole rest of the wallet. `ocr.available()` is the only way to ask. No
screenshot is ever sent to a service.

**A new route is closed unless you say otherwise.** Everything requires a
session except the four endpoints named in `app.OPEN_ENDPOINTS`, and
`test_every_route_is_closed_unless_it_is_named_open` walks the real routing
table to check it. So adding a route protects it by default; opening one is a
visible edit to a frozenset.

**Every state-changing request carries the CSRF token.** `send()` in app.js
attaches it, so no call site has to remember -- forgetting it at one of
nineteen would be a 403 somebody debugs for an hour. If you add a bare
`fetch()` that POSTs, route it through `send()`.

**Never make the app reachable without a password.** `auth.guard` raises
`Unsafe` for any host that is not loopback unless `WALLET_PASSWORD_HASH` and
a 32-character `SECRET_KEY` are both set. Do not add a flag that bypasses it.
It is the one mistake in this project that cannot be walked back, and a
warning in a log is not a substitute.

**Guards name the thing they guard, not one file.** Every check in
`test_frontend.py` read `app.js` by name, so a second script escaped all of
them and shipped a duplicate money formatter. `test_structure.py` has the same
shape with its `MODULES` tuple. Adding a module or a script means adding it
there, and both files now have a companion test that fails if the list drifts
from reality.

## Tests

```bash
pytest -q --cov=. --cov-report=term-missing
python -m flake8 . --select=E9,F63,F7,F82,F401,F402,F811,F841,E722,E741
```

Both clean before a push. The cases most worth adding to are the awkward
ones: a bank format that imports wrong, a holiday the rate lookback handles
badly, an amount that parses to the wrong magnitude, a screenshot the parser
reads as a year.

**Adding a test moves two published figures**, because the README and the
overview page both quote the suite's size and
`tests/test_published_figures.py` checks that they quote it correctly. Do not
go hunting for them:

```bash
python tools/refresh_figures.py
```

It measures, rewrites both, and prints what it changed. A new test *file* is
the one thing it will not finish on its own: add a row for it in the page's
`TESTS` block saying what the file covers, and the script fills in the count.

The optional OCR install is not needed to run them. `ocr.py` turns an image
into text boxes and `receipts.py` turns boxes into a purchase, so the parser
tests build boxes by hand and finish in milliseconds. Use real engine output
when you add one -- every mangled string in there came off an actual
screenshot, and tidier input would test a parser for a problem this one does
not have. One integration test runs the engine and skips without it.

**Watch a new guard fail before trusting it.** Break the code it protects,
confirm it goes red, put the code back. Four structural tests written for
these projects passed unconditionally -- one compared a value to itself, one
grepped a whole file and found its string in a comment.
