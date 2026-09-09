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

## Tests

```bash
pytest -q --cov=. --cov-report=term-missing
python -m flake8 . --select=E9,F63,F7,F82,F401,F402,F811,F841,E722,E741
```

Both clean before a push. The cases most worth adding to are the awkward
ones: a bank format that imports wrong, a holiday the rate lookback handles
badly, an amount that parses to the wrong magnitude.
