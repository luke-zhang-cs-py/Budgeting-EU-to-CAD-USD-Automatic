# Wallet — euro spending in CAD and USD

[![CI](https://github.com/luke-zhang-cs-py/Budgeting-EU-to-CAD-USD-Automatic/actions/workflows/python-package.yml/badge.svg)](https://github.com/luke-zhang-cs-py/Budgeting-EU-to-CAD-USD-Automatic/actions/workflows/python-package.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.12-blue.svg)](https://www.python.org/)

**[Read the overview →](https://luke-zhang-cs-py.github.io/Budgeting-EU-to-CAD-USD-Automatic/)**
— how the screenshot reader decides what an amount is, why "real time" is the
wrong promise for a card rate, and every bug this thing has had.

A local Flask app that takes euro spending, converts each purchase to Canadian
and US dollars **at the rate that applied on the day it was spent**, writes the
result to a CSV, and tracks it against a monthly budget per category.

Everything stays on your machine by default: no account, no API key, no bank connection. It can be [hosted](#running-it-somewhere-other-than-your-own-machine) if you want it from your phone, and refuses to start reachable without a password.

```bash
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5004
```

The first run downloads the European Central Bank rate history — one 640 KB
file covering every business day back to 1999 — and caches it. After that the
app works offline.

## Getting data in

**Apple Wallet and Google Wallet cannot be read by any desktop app.** They are
sandboxed on the device and have no export API. That is a real constraint, not
a gap in this project, and anything claiming otherwise is scraping screenshots.

So there are four honest routes in.

**1. A watched folder — the automatic one.** Point `WALLET_INBOX` at a folder
your phone already syncs (iCloud Drive, OneDrive, Dropbox), drop a bank export
into it, and it imports itself within the minute. Nothing leaves your machine,
there are no credentials, and no third party sees a transaction. Files are
never moved or deleted.

Two properties make that safe to leave running:

- **A file is never imported twice.** Identity is a digest of the contents, so
  renaming or re-syncing the same export is free.
- **A wider re-export adds only its new rows.** Re-download from your bank
  with a longer date range and the overlap is caught by the duplicate rule
  that predates all of this. That is the whole reason unattended import is
  safe.

**2. Prefer the `.qfx`, not the CSV.** CIBC exports both, and the OFX/QFX
file carries three things its CSV throws away:

| Tag | What it gives |
|---|---|
| `<FITID>` | the bank's own id for the transaction |
| `<CURSYM>` | the currency the purchase was actually made in |
| `<CURRATE>` | the exact rate the bank converted at |

So the euro amount is the bank's own arithmetic — 85.94 CAD at a disclosed
1.64321 is €52.30, exactly — rather than a regex over a description. And
"have I seen this before" becomes an exact question instead of a comparison of
dates and descriptions. Both dialects read: SGML (version 1, unclosed tags)
and XML (version 2).

Drop a `.qfx` in the watched folder and it is preferred automatically; the
format is recognised by content, not by extension.

**3. Import a CSV by hand.** The importer assumes no format. It sniffs the
delimiter, works out which column is which, and shows a preview to correct
before anything is saved. Revolut, Wise, N26, a UK `Paid out`/`Paid in`
statement, a German semicolon file with decimal commas, and **headerless
exports like CIBC's** all import as they are — for a file with no header row
the columns are inferred from the values, because CIBC alone produces at
least three layouts and a per-bank profile would have to guess which one you
downloaded.

**4. Type it in.** One line for cash, or anything the export missed.

`sample-statement.csv` is included so a fresh clone has something to try.

### Why there is no CIBC sync

Three separate things were checked before accepting that a file is the
interface:

- **No open-banking API.** The Consumer-Driven Banking Act received Royal
  Assent in March 2026 and CIBC is a mandatory participant, but Phase 1 read
  access has no operational date.
- **No OFX Direct Connect.** CIBC supports Web Connect only — a manual
  download. Direct Connect is supported by very few Canadian banks, and not
  by this one.
- **No aggregator worth using.** The ones that reach Canadian banks either
  need a commercial agreement or log into your online banking on your behalf,
  which breaches CIBC's own agreement and can void your fraud-liability
  protection.

When the official API lands it becomes another source. Until then, exporting
the `.qfx` into a synced folder is as close to automatic as CIBC allows, and
the only step it costs you is the download.

## What the conversion cost you

A Canadian card used in Europe does not bill euros. CIBC converts at the Visa
network rate and adds 2.5%, so its export shows CAD — already converted, at a
rate you did not choose and were not told.

Because the ECB rate is stored per transaction date, the comparison is free:

```
2026-09-02  REWE SAGT DANKE     €52.30
            billed by the card  CA$85.94
            at the ECB rate     CA$83.85
            the conversion cost CA$ 2.09   (2.49%)
```

That works when the original euro figure is recoverable from the description,
which card statements carry when they carry it at all. When it is not, the row
is refused rather than guessed at — the rule that has always applied, because
a 200 SEK lunch booked as EUR 200 is a twenty-fold error that looks entirely
plausible.

## The part that is easy to get wrong

The ECB publishes rates **on business days only.** There are no weekend rows
at all, and the longest gap in the series is five days — Easter 2026 runs
2 April straight to 7 April, and Christmas 2025 runs the 24th to the 29th.

So a purchase made on a Sunday has no rate of its own. This app walks back to
the last published business day and **records which day it used**:

| Spent on | Rate from | Lag |
|---|---|---|
| Tue 8 Sep 2026 | 8 Sep | 0 |
| Sun 6 Sep 2026 | 4 Sep | 2 |
| Easter Sun 5 Apr 2026 | 2 Apr | 3 |
| Boxing Day 2025 | 24 Dec | 2 |

Those `rate_date` and `rate_lag_days` columns are in the exported file, so the
arithmetic can always be checked. Reaching for "yesterday's rate" instead is
wrong several times a year, and wrong quietly.

It also never reaches *forward* for a nearer rate. 5 April is one day from the
7th and three from the 2nd, but the 7th had not happened when the money was
spent — using it would make a closed month's total change every time the file
was refreshed.

## Snapping a purchase

Upload a screenshot — a card app's transaction detail, a payment
confirmation, a row of online banking — and the amount, date, merchant and
card are read off it. The engine is a local ONNX model, so **no picture of
what you bought is sent anywhere**. It is an optional install:

```bash
pip install -r requirements-ocr.txt      # ~60 MB, local, works offline
```

Without it the upload still works: the screenshot is stored and attached to
the purchase you then type by hand.

**Nothing is ever recorded from a screenshot without you seeing it first.**
The reader fills a form; the form is what saves. That is not caution for its
own sake — an OCR misread that books €5,230 instead of €52.30 surfaces a
month later in a total nobody can explain, which is far worse than a
screenshot that could not be read at all.

So it says what it could not settle, and why:

| What it does | Why |
|---|---|
| Takes the **tallest** number as the amount | Whoever designed that screen made the figure large because it is the one you opened it to see. Position is a much weaker clue. Every other candidate is offered. |
| Refuses `2026`, `14:32`, `4417`, `1.64321` | A run of digits is money only if it carries a currency marker or ends in exactly two decimals. Without that rule the year is a very large purchase. |
| Leaves `$` unresolved | On a CIBC statement it is Canadian and on a US receipt it is not, and nothing in a screenshot settles it. |
| Asks about `07/09/2026` | The 7th of September, or the 9th of July. It offers both rather than picking one. |
| Prefers what the bank disclosed | `Foreign currency 52.30 EUR @ 1.64321` is the bank stating what it actually did — and 52.30 × 1.64321 is exactly the CA$85.94 it billed. |
| Matches `••4417` to a card | So the right foreign-transaction fee applies. Two cards sharing a mask match neither. |

## What will this cost?

The latest published euro rate, fetched when you ask, with your card's fee on
top:

```
€52.30 at 1.6043            CA$83.90
CIBC Dividend Visa, 2.50%    +CA$2.10
you would be charged        CA$86.00      an all-in rate of 1.6444
```

A word on "real time", because the phrase promises more than the situation
can use. Both free sources republish the ECB's **once-a-day** fixing, and
there is no free intraday EUR/CAD tick. Chasing one would be false precision
anyway: **a card is not settled at the rate at the moment you tap.** Visa
converts on the day the transaction reaches the network, and the fee follows.
So the figure carries the day it was published and the minute it was fetched,
and the page says plainly that it is a daily reference rate.

The model checks out against a real statement: the ECB rate plus 2.5%
predicts CA$86.00 where CIBC actually billed CA$85.94.

## Cards, and a fee that is measured rather than assumed

The foreign-transaction fee is a property of the **card**, not of the
purchase — the same euro coffee costs 2.5% more on a CIBC Visa than on a euro
account — so it is stored per card, in basis points, and each purchase is
attributed to whichever card paid for it.

The published 2.5% is a starting point. Once statements are in, `measure fee`
reports what the card actually charged over its own rows, which came out at
2.39–2.49%, because Visa converts at its own rate rather than the ECB's. It
declines to answer below three priced purchases rather than dressing the
published figure up as an observation.

A card's balance says how it was arrived at: purchases imported from a
statement are exact, euro purchases converted at the day's rate are an
estimate, and the two are counted separately rather than blended into one
figure that looks exact.

## What it does

- **Converts** every purchase to CAD and USD at its own transaction-date rate
- **Reads a screenshot** of a purchase, locally, and asks before recording it
- **Estimates** what a euro amount will bill on a given card, at today's rate
- **Tracks cards**, each with its own currency, fee and balance
- **Exports** a CSV: euros, CAD, USD, both rates, the rate date and the lag
- **Budgets** a monthly cap per category, with spent / remaining / over-pace
- **Detects duplicates**, so re-importing an overlapping statement is safe
- **Auto-categorises** from keyword rules, applied to rows already imported
- **Flags recurring charges** — and says when one has quietly got dearer,
  with what the rise costs over a year
- **Compares each category with its own average**, not with a budget, which
  is the comparison that catches something creeping up over a quarter
- **Tracks goals** against what your own caps actually left over
- **Search, monthly trend, per-category totals**

### Money

Amounts are integers, in cents, everywhere. Floats are the wrong type for
money and it shows quickly: `0.1 + 0.2` is `0.30000000000000004`, so a total
disagrees with the rows above it, and once a reader sees that they stop
trusting every other figure on the page.

Conversion rounds once per row, half-up, using `Decimal`. Not academic — at
the real rate of 1.6033, €50.00 is exactly 8016.5 cents; `Decimal` gives
CA$80.17 and naive float arithmetic gives CA$80.16.

The importer reads both decimal conventions, because euro data is genuinely
ambiguous: `1.234,56` and `1,234.56` both mean the same amount, and `1,234`
means a thousand. Getting that wrong is a factor-of-a-thousand error that
looks like an ordinary small purchase.

### Endpoints

| Route | Does |
|---|---|
| `GET /` | the page |
| `GET /api/overview` | totals, budgets, trend, recurring, rate coverage |
| `GET /api/transactions` | rows, filtered by month / category / search |
| `POST /api/transaction` | add one manually |
| `POST /api/import/preview` | what a file would do, saving nothing |
| `POST /api/import/commit` | actually import it |
| `POST /api/budget` | set a monthly cap (0 removes it) |
| `GET POST /api/rules` | keyword rules |
| `POST /api/rates/refresh` | re-fetch from the ECB |
| `GET /export/transactions.csv` | the file |
| `GET /export/summary.csv` | budget vs actual |
| `GET /api/reconciles` | does the file add up to the ledger |
| `GET /api/sources` | where the watched folder is, and what it has imported |
| `POST /api/sources/scan` | sweep the folder now rather than waiting |
| `POST /api/receipt/read` | read a screenshot; records nothing |
| `POST /api/receipt/save` | record the purchase you confirmed |
| `GET /receipt/<name>` | a stored screenshot, by its content digest |
| `GET /api/rate/live` | the latest published rate, fetched now |
| `POST /api/rate/estimate` | what an amount would bill on a card |
| `GET POST /api/cards` | cards and their balances |
| `POST DELETE /api/cards/<id>` | correct or remove one |
| `GET /api/cards/<id>/measured` | the fee its own rows actually show |
| `POST /api/transaction/<id>/card` | say which card paid |
| `GET /api/trends` | month by month, and this month's movers |
| `GET /api/trends/<category>` | the purchases behind one figure |
| `GET /api/subscriptions` | what is due, what landed, what got dearer |
| `GET POST /api/goals` | goals and what the month contributed |
| `DELETE /api/goals/<id>` | remove one |

## Running it somewhere other than your own machine

The app binds `127.0.0.1` with no password because it holds your spending
history. Reaching it from anywhere else means turning that off, so it will not
let you do it by accident:

```
loopback, no password   ->  runs, no login.        What it has always done.
loopback, password set  ->  runs, asks for it.
anything else, no hash  ->  refuses to start.
```

That last line is a raise, not a warning. `auth.guard` raises `Unsafe` and the
process exits, telling you what to set. A warning printed into a log nobody
reads is how a financial ledger ends up on the open internet with no password
on it.

### Read this before your first deploy

**Point `WALLET_DATA` at a disk that survives a restart.** This is the single
most likely way to lose the data. Most platforms give each deploy a fresh
filesystem, so `wallet.db`, the rate cache and every uploaded screenshot are
destroyed the next time you push. The `Dockerfile` declares
`VOLUME ["/data"]` and sets `WALLET_DATA=/data` for exactly this reason —
attach something to it. On Fly.io that is `fly volumes create`; on Render, a
disk; on a VPS, a bind mount.

Back it up too. It is one SQLite file and a folder of images.

### Setting it up

```bash
python -m auth                    # asks twice, prints WALLET_PASSWORD_HASH=...
python -c "import secrets; print(secrets.token_hex(32))"   # SECRET_KEY
```

The password is typed, never passed as an argument — a command line ends up in
shell history and in the process list. Only the hash is stored, so there is
nothing anywhere that can recover the password; keep it in a password manager.

Then, in the environment where it runs:

| Variable | |
|---|---|
| `WALLET_PASSWORD_HASH` | the scrypt hash. Required once reachable. |
| `SECRET_KEY` | 32+ characters. Signs the session cookie; a guessable one lets anyone mint a logged-in session. Required once reachable. |
| `WALLET_DATA` | the persistent volume. See above. |
| `HOST` | `0.0.0.0` on a platform that proxies to you. |
| `PORT` | usually set for you. |

```bash
docker build -t wallet .
docker run -p 8000:8000 -v wallet-data:/data \
  -e WALLET_PASSWORD_HASH='scrypt:...' \
  -e SECRET_KEY='...' \
  wallet
```

Or without Docker: `gunicorn wsgi:application --workers 2 --timeout 120`.
`wsgi.py` exists rather than reusing `app.py`'s `__main__` because a
deployment differs in ways that matter — the dev server is single-threaded, and
the folder watcher must not run once per worker, since the right number of
threads writing to one SQLite file is one.

### HTTPS is the host's job, and the app assumes you did it

Once `HOST` is not loopback the session cookie is marked `Secure`, so **it is
not sent over plain HTTP at all**. If you deploy and the login page accepts
your password and then bounces you straight back to it, that is this — you are
on `http://`. Every platform worth using terminates TLS for you; put it behind
one rather than turning the flag off.

`Strict-Transport-Security` is sent only when public, because promising HTTPS
on a loopback run that has none makes the app unreachable in a browser that
believes it.

### What the protection is, and what it is not

| Threat | What is done about it |
|---|---|
| Guessing the password | scrypt, and three free attempts then a refusal window that doubles to 15 minutes. Refused, not slept — holding the request open would let an attacker exhaust the workers for free. |
| A forged request from another site | A session token that must come back in a header, on all 19 state-changing routes. `SameSite=Lax` too, but that is a second lock rather than the lock: three of those routes take multipart uploads, which a plain cross-origin form can send. |
| A stolen cookie | `HttpOnly` so script cannot read it, `Secure`, and a 14-day lifetime. |
| Someone reading the page | Nothing is reachable without a session except `/login` and `/health`, and that list is checked by a test that walks the real routing table — so a route added later is closed because it was not opted out, rather than exposed because somebody forgot to opt it in. |
| XSS | A CSP with `script-src 'self'`, which is only possible because the page has no inline script at all. |

And what it does not address, because it cannot: **anyone who can read the
disk can read the database.** There is no encryption at rest here. Hosting
this means trusting the host with the file — which is a real decision, and the
reason the app defaults to your own machine.

There is also one account, deliberately. No registration, no password reset,
no email. All of that is attack surface, and a single-user ledger has no use
for any of it.

## Your data

`data/` — holding `wallet.db` with every purchase, the rate cache, and
`data/receipts/` with every screenshot you have uploaded — is gitignored,
along with `*.db` and `*.csv`. Financial history does not belong in a
repository, and a folder of pictures of what you bought is the most personal
thing this app holds. Set `WALLET_DATA` to move it elsewhere, and `WALLET_INBOX`
to point the watched folder at your synced directory.

This repository is public, so that ignore file is what stands between a real
ledger and the internet. A feature that writes anything derived from a
transaction needs a line in it, in the same commit.

The server binds `127.0.0.1` with debug off. Going wider is deliberate:
`HOST=0.0.0.0`. The sibling app in this family bound every interface with the
Werkzeug debugger on, which is an interactive Python console for anyone on the
network.

## Tests

```bash
pytest -q --cov=. --cov-report=term-missing
python -m flake8 . --select=E9,F63,F7,F82,F401,F402,F811,F841,E722,E741
```

759 tests, 100% of 2,346 statements. See [CONTRIBUTING.md](CONTRIBUTING.md)
for the conventions and the one thing that will confuse you.

The screenshot parser is tested without the OCR engine at all: `ocr.py` turns
an image into text boxes, `receipts.py` turns boxes into a purchase, and the
tests build boxes by hand. Every mangled string in them came out of the real
engine — `8September2026at14:32` with the spaces eaten, `18,90` with the euro
sign dropped — because inventing tidier input would test a parser for a
problem this one does not have. One integration test runs the real engine and
skips when it is not installed.

## License

[MIT](LICENSE)
