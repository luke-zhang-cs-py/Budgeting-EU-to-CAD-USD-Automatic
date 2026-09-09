# Code audit

First audit of this repository, run against the checklist the day after it was
written. Writing the code and auditing it are different activities, and the
gap between them is where most of the findings below came from — three of them
are smells introduced *while fixing other smells*.

```bash
pytest -q --cov=. --cov-report=term-missing
python -m flake8 . --select=E9,F63,F7,F82,F401,F402,F811,F841,E722,E741,C901 --max-complexity=12
python -m radon cc . -s -n C --exclude "tests/*"
python -m radon mi . -s --exclude "tests/*"
```

## Coverage

**204 tests, 94%.**

| Module | Cover | | Module | Cover |
|---|---|---|---|---|
| `db.py` | **100%** | | `budgets.py` | 99% |
| `export.py` | **100%** | | `money.py` | 97% |
| `paths.py` | **100%** | | `app.py` | 95% |
| `ledger.py` | 99% | | `importers.py` | 88% |
| | | | `fxrates.py` | 85% |

Maintainability index is A across every module (46.8 – 92.8).

`fxrates.py` is the lowest and knowingly so: the uncovered lines are the
`urllib`-then-`curl` download ladder and the defensive `continue` branches in
the ECB parser for rows the real file has never contained. Exercising the
download for real in the suite would make the tests depend on the network,
which is the opposite of what they are for.

`importers.py` at 88% is the both-columns-filled reconciliation in
`_amount_of` and a few unreachable-in-practice guards. Worth a test if a bank
turns up that writes both `Paid out` and `Paid in` non-zero on one row.

## Findings

### Dispensables — dead code

**`paths.in_data_dir` was never called by anything, including its own
module.** Written twenty minutes earlier while fixing the duplication below —
a helper added "in case", which is speculative generality, and which coverage
caught at 73% on a seven-statement file. Removed.

That is the honest shape of this finding: the newest code in the repository
was the dead code. Nothing else is orphaned — every other private helper has
call sites within its module, checked rather than assumed.

flake8 F401/F402/F811/F841 is clean.

### Dispensables — duplicate code

**Three separate duplications, one of which had already drifted.**

1. **`WALLET_DATA` was read in two modules.** `db.data_dir` and
   `fxrates.cache_path` each resolved the data directory for themselves. The
   duplication is two lines; the *failure* is that nothing forced them to
   agree, so the database and the rate cache could resolve to different
   directories — a ledger read from one place and converted with rates from
   another, neither looking wrong. Now `paths.py`, and a structural test fails
   if any module reads the variable itself.

   This is the third time this family of projects has had it. The face project
   had seven modules capturing their own paths and a `use()` that consequently
   moved none of them.

2. **The conversion loop existed twice, and had drifted.**
   `ledger._converted` and `budgets._converted_total` both looped over
   `money.TARGETS`, converted, built `<prefix>_{key}` and `_text` entries, and
   caught failures. One caught `(RateError, MoneyError)`; the other also caught
   `ValueError`. **The same bad date therefore failed differently depending on
   which screen you were looking at** — the transaction list would 500 where
   the summary degraded quietly. Now `fxrates.convert_all`, with the failure
   handling in one place.

   Divergence in a copy is not hypothetical. It had already happened here,
   within a day of the copy being made.

3. **`export._plain` re-implemented money formatting**, with the minor-unit
   scale written out as a literal `100` — knowledge `money.py` already held as
   `MINOR_UNITS`, in a second place that did not know about the first. Now
   `money.format(symbol=False, grouping=False)`.

### Bloaters — large method

**`create_app` measured cyclomatic complexity 40.** Every route inline in the
factory: the standard Flask shape, and the standard problem, since it gains a
branch per endpoint and there were fifteen. Split into six registration
functions — `_pages`, `_reading`, `_writing`, `_importing`, `_settings`,
`_exporting` — each taking one context object. It is now below the reporting
threshold; the largest is `_settings` at C(13).

Two duplicated fragments came out of the split as named helpers: `_read_upload`
(preview and commit both read, sniffed, mapped and previewed a file, and had
drifted in which errors they reported) and `_signed_amount`.

### Bloaters — long parameter list / primitive obsession

The route groups needed the data directory, a connection factory and the
requested month. Three parameters threaded through six functions, all
describing one thing — the request's view of the data. That is the shape
primitive obsession takes in a web app. One `Context` object instead.

`directory=None` still travels through most function signatures as a bare
string. Left deliberately: it is the seam the tests use to redirect the whole
app at a temporary directory, and an object wrapping one optional path would be
ceremony.

### Abusers — conditional complexity

Three functions remain at C(16)–C(18), all flat rather than nested:

- `money.parse` C(18) — the decimal-convention rules. Every branch is a real
  format a bank exports, and the alternative is a flag the caller has to get
  right. Guessing wrong is a factor-of-a-thousand error, so the branches are
  the feature.
- `budgets.summary` C(18) — a dict literal of conditional expressions. radon
  counts each ternary; there is no nesting.
- `importers._amount_of` C(16) — the three shapes a bank writes an amount in.

Each is a lookup table expressed as code rather than tangled logic. Left as
is, with the reasoning above each.

### Couplers — inappropriate intimacy

**`importers` called `ledger._as_date`.** The underscore is a contract saying
"this may change without notice", and a sibling module was depending on it.
Renamed to `ledger.as_date` and documented as public, since parsing the eight
date formats banks export is genuinely a public concern. A structural test now
fails on any `module._private` reach across a boundary.

### Couplers — hidden dependencies

**Two functions in `importers` did `import ledger` inside the function body.**
That reads as a circular-import workaround. It was not one — proved by adding
the top-level import and importing the module, which works. So it was a
defensive habit that hid a real dependency from anyone reading the import
block. Moved to the top, and a test asserts nothing imports inside a function.

The layering is now explicit and asserted:
`money`/`paths` → `db`/`fxrates` → `ledger` → `importers`/`budgets` →
`export` → `app`. A new import that inverts it fails a test rather than an
application start.

### Change Preventers

The `WALLET_DATA` and conversion-loop duplications *were* the change
preventers, and both had already cost something. Adding a currency would have
meant editing two loops; changing where data lives meant editing two modules
that could disagree.

One remains, deliberately: `static/js/app.js` formats nothing itself and
receives money pre-formatted from the server. That is the opposite trade —
duplicated *rounding* would be far worse than a round trip — and a test
asserts the browser has no `toFixed(2)` or `/ 100`.

### Global Data

`fxrates` holds a module-level rate cache and `db` a module-level lock. Both
are lock-guarded, both have `reset()`, and `fxrates` keys its cache by the
resolved path so pointing at a new directory invalidates it rather than
silently serving the old rates. That last part is what makes the test suite
able to run 204 tests against 204 temporary directories.

### Magic Number

Named, with the reasoning above each: `MAX_LOOKBACK_DAYS = 10` (measured
worst case is 5), `MINOR_UNITS`, `MAX_BAD_SHARE`,
`MIN_ROWS_FOR_MAPPING_CHECK`, `WARN_AT = 0.80`,
`MIN_ELAPSED_FOR_PACE = 0.15`, `MAX_UPLOAD_BYTES`, `PREVIEW_ROWS`.

The literal `100` in `export.py` was the one unnamed scale and is gone.

### Inconsistent Naming / Uncommunicative Name

One type scale and one palette, both declared as CSS custom properties, with a
test asserting no hardcoded `font-size` outside them. That test exists because
the transit app in this family grew a second scale by accident — a 10px label
among 11.5px figures — and a legend gradient three shades off the map it
explained.

No type-suffixed names (`quantity_int`). Amounts carry their unit instead:
`amount_eur`, `cap_eur`, `spent_cad`, `tolerance_cents`. In a money
application the unit is the thing a reader needs and the type is not.

**Accepted deviation:** `money.format` shadows the builtin `format`. Callers
always write `money.format(...)`, so at the call site it is unambiguous and
reads better than any alternative; the risk is confined to `money.py` itself,
which uses f-strings rather than the builtin. Renaming 40 call sites to remove
a shadow nobody can trip over is churn, so it stays, recorded here.

## Bug classes

| Class | Found |
|---|---|
| Syntax | none — flake8 E9/F63/F7/F82 clean |
| Runtime | **fixed:** the two conversion loops caught different exceptions, so one screen would 500 where the other degraded |
| Functional | none new; the suite covers the conversion and budget arithmetic |
| Logical | **fixed (during construction):** the mapping-confidence guard applied its ratio to files too small for the ratio to mean anything |
| Workflow | reviewed — preview writes nothing, commit is a separate call, duplicates report rather than error |
| Unit-level | **fixed:** two of my own structural tests were wrong (below) |
| Integration | reviewed — verified end to end against the real ECB file and a re-import |
| Out of bounds | reviewed — the rate lookback is bounded and refuses rather than walking arbitrarily far back |
| Security | reviewed below |

**My own test bugs, both found by running them.** One built a rate cache whose
newest row was *before* the date it then asked about, so `convert_all`
correctly refused it as a guess and the test read that as a failure. The other
grepped `export.py` for the literal `100` and matched the docstring explaining
the literal it used to contain — a test failing on its own documentation. The
same mistake, grepping prose for what the prose describes, appeared earlier in
this session against the transit repo. It is worth naming as a habit rather
than an incident.

**Security.** No authentication, no secrets, no user input reaching a shell or
a query — every statement is parameterised. Uploads are capped at 8 MB. The
server binds loopback with debug off, and a test clears `HOST` before asserting
the default, because the sibling app in this family bound every interface with
the Werkzeug debugger enabled — an interactive Python console on the network.

Bank descriptions reach the DOM and go through `esc()`;
`tests/test_frontend.py` checks it structurally, because the face project
shipped a stored XSS hole doing exactly this. `data/` is gitignored along with
`*.db` and `*.csv`, and the commit was checked to contain neither.

## Maintenance classification

**Corrective** — the drifted exception tuples in the duplicated conversion
loop, the mapping guard misfiring on small files, and my two broken tests.

**Adaptive** — measuring the ECB feed before designing against it. It
publishes business days only, with no weekend rows and a five-day maximum gap,
so a Sunday purchase needs to reach back up to four days. That is a property
of somebody else's publication schedule, and adapting to it is why every row
records `rate_date` and `rate_lag_days`. The other adaptation was accepting
that Apple and Google Wallet have no export API, which made a
column-mapping file importer the design rather than a fallback.

**Perfective** — `paths.py`, `fxrates.convert_all`, the `create_app` split,
`money.format(grouping=)`, removing dead code. No behaviour changed: all 188
pre-existing tests passed before each change and after it, which is the only
reason the refactoring was safe to do at this pace.

**Preventive** — the sixteen tests in `tests/test_structure.py`.

Checked rather than claimed: run against the initial commit (with only
`paths.py` added so the module imports), **11 of the 16 fail** —

```
test_only_one_module_reads_the_data_directory_variable
test_neither_caller_keeps_its_own_conversion_loop
test_convert_all_answers_for_every_target_currency
test_convert_all_reports_a_failure_per_currency_rather_than_raising
test_convert_all_carries_the_business_day_lag
test_the_csv_form_comes_from_money_not_a_second_implementation
test_grouping_can_be_turned_off_for_the_file
test_no_module_reaches_into_another_private_name
test_nothing_imports_inside_a_function
test_the_app_factory_is_not_one_long_function
test_the_checkbox_flag_is_read_one_way
```

The other five pass against the old code, and it is worth being accurate about
which: `test_the_module_layers_do_not_cycle` and
`test_the_scale_is_named_in_exactly_one_place` were already true, and the three
`paths` tests only exercise the new module. So five of these guard properties
the code already had, and eleven guard properties it did not.

Structural tests are the only kind that hold a refactoring in place. A comment
saying "keep this in one module" is advice; a failing test is a decision.

Structural tests are the only kind that hold a refactoring in place. A comment
saying "keep this in one module" is advice; a failing test is a decision.
