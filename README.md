# Wallet — euro spending in CAD and USD

[![CI](https://github.com/luke-zhang-cs-py/Budgeting-EU-to-CAD-USD-Automatic/actions/workflows/python-package.yml/badge.svg)](https://github.com/luke-zhang-cs-py/Budgeting-EU-to-CAD-USD-Automatic/actions/workflows/python-package.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.12-blue.svg)](https://www.python.org/)

A local Flask app that takes euro spending, converts each purchase to Canadian
and US dollars **at the rate that applied on the day it was spent**, writes the
result to a CSV, and tracks it against a monthly budget per category.

Everything stays on your machine. No account, no API key, no bank connection.

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

So there are two honest routes in:

1. **Import a CSV** exported from the bank or card sitting behind the wallet.
   The importer does not assume a format — it sniffs the delimiter, guesses
   which column is which from the header names, and shows you a preview to
   correct before anything is saved. Revolut, Wise, N26, a UK
   `Paid out`/`Paid in` statement and a German semicolon file with decimal
   commas all import as-is.
2. **Type it in.** One line for cash, or anything the export missed.

`sample-statement.csv` is included so a fresh clone has something to try.

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

## What it does

- **Converts** every purchase to CAD and USD at its own transaction-date rate
- **Exports** a CSV: euros, CAD, USD, both rates, the rate date and the lag
- **Budgets** a monthly cap per category, with spent / remaining / over-pace
- **Detects duplicates**, so re-importing an overlapping statement is safe
- **Auto-categorises** from keyword rules, applied to rows already imported
- **Flags recurring charges** — same merchant, similar amount, 3+ months
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

## Your data

`data/` — holding `wallet.db` with every purchase, and the rate cache — is
gitignored, along with `*.db` and `*.csv`. Financial history does not belong
in a repository. Set `WALLET_DATA` to move it elsewhere.

The server binds `127.0.0.1` with debug off. Going wider is deliberate:
`HOST=0.0.0.0`. The sibling app in this family bound every interface with the
Werkzeug debugger on, which is an interactive Python console for anyone on the
network.

## Tests

```bash
pytest -q --cov=. --cov-report=term-missing
python -m flake8 . --select=E9,F63,F7,F82,F401,F402,F811,F841,E722,E741
```

204 tests. See [CONTRIBUTING.md](CONTRIBUTING.md) for the conventions and the
one thing that will confuse you.

## License

[MIT](LICENSE)
