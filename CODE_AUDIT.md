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

---

# Second pass

Run over the code added for automatic import -- `sources.py`, `fxcost.py`,
`layout.py`, the migration and the UI -- roughly 400 statements written in one
sitting and unaudited. Same checklist.

**375 tests, 100% of all 1,117 statements**, flake8 clean, maintainability A
across twelve modules.

| Module | Cover | | Module | Cover |
|---|---|---|---|---|
| `app.py` | **100%** | | `layout.py` | **100%** |
| `budgets.py` | **100%** | | `ledger.py` | **100%** |
| `db.py` | **100%** | | `money.py` | **100%** |
| `export.py` | **100%** | | `paths.py` | **100%** |
| `fxcost.py` | **100%** | | `sources.py` | **100%** |
| `fxrates.py` | **100%** | | `importers.py` | **100%** |

## Bloaters — the largest method in the project

`infer_mapping` measured **D(22)**, the only D the project has had. It did four
jobs: classify every column, choose the date, choose the description, and
decide which of three amount shapes the file uses. Split into `_classify` and
`_amount_shape`, and it is now below the reporting threshold with `_classify`
at C(11).

## Bloaters — divergent change in importers.py

`importers.py` had grown to **291 statements over thirteen functions** with the
lowest maintainability index in the project (41.42), and it held two unrelated
jobs. Nine functions answered *what shape is this file*; four answered *turn
these rows into transactions*. Those change for different reasons: a new bank
format touches the first, a change to how a transaction is built touches the
second.

Extracted as `layout.py`. `importers.py` is down to 127 statements and its
index back to 53.89; `layout.py` is 60.10.

## Couplers — a message chain

`sources.py` called **`importers.ledger.apply_rules(connection)`** -- reaching
through one module to get at another. Both names are public, so this is not the
private-access smell; the fault is the route. It would have broken silently the
day `importers` stopped needing `ledger`, with nothing in `sources` to explain
why. It imports `ledger` directly now.

## Magic Number — a length written out three times

The digest truncation `[:32]` appeared in `ledger.fingerprint` twice and
`sources.digest` once. Both feed UNIQUE identity columns declared in the same
schema, so it is one decision in two modules. Named `db.DIGEST_CHARS`.

Also named, in the extracted module: `SAMPLE_ROWS`, `TYPE_AGREEMENT` and
`BALANCE_DENSITY`, which were the bare `40`, `0.8` and `0.9` deciding how a
column is classified.

## Dispensables — a comment that had become untrue

`db.py` opened "Three tables and no ORM" while declaring four; the `imports`
table was added without the sentence being updated. The spam classifier in this
family had the identical fault -- a comment describing a fix as finished while
half of it was outstanding -- and it is worse than no comment, because it
answers a reader's question wrongly. A test now derives the count from the
schema.

## Abusers — a route group that became a grab-bag

`_settings` measured C(15) after the source routes were added, holding budgets,
rules, rates and the folder. Split into `_budgets`, `_rules_and_rates` and
`_watching`; eight groups now, none above C(11).

## Unit-level bug — a test that could not fail

The worst finding of this pass, and mine.

`test_nothing_reaches_through_one_module_to_another` was written through a
shell heredoc, which turned the `` in its regex into a literal **backspace
byte (0x08)**. The pattern could never match, so the test passed
unconditionally -- a guard against message chains that would not have noticed
one.

It survived a negative control because I checked a regex I had retyped by hand
rather than the one in the file. The real check is to run the actual test
function against a planted fault, and it now does: the planted chain is caught,
and the code as it stands passes.

This is the second one this month. The spam classifier had
`web.clf.TEST_SIZE == allinone.TEST_SIZE`, which compares a value to itself
because `web.clf` *is* that module. Same category, different mechanism, and the
lesson is the same: **a structural test is worthless until it has been seen to
fail.** Every guard added in either pass has now been run against the code it
was written to reject.

## Maintenance classification

**Corrective** — the vacuous guard, and the `db.py` docstring.

**Adaptive** — the whole reason this code exists. Canada has no live
open-banking API: the Consumer-Driven Banking Act received Royal Assent in
March 2026 and CIBC is a mandatory participant, but Phase 1 read access has no
operational date. Adapting meant a watched folder rather than an API, and
handling CIBC's headerless export by inferring layout from values because that
one bank produces at least three shapes.

**Perfective** — `layout.py`, the two function splits, the route regrouping,
`DIGEST_CHARS`, the three named thresholds. No behaviour changed: all 369 tests
passed before each step and after it.

**Preventive** — five new structural guards, each run against a planted fault
rather than assumed:
`test_nothing_reaches_through_one_module_to_another`,
`test_the_digest_length_is_named_in_one_place`,
`test_working_out_a_file_shape_is_not_in_the_importer`,
`test_the_route_groups_stay_small`,
`test_the_schema_docstring_counts_its_own_tables`.


# Third pass — screenshots, live rates, cards and planning

Measured 9 September 2026 after the work: **699 tests, 100% of 2,195
statements**, and no flake8 finding in any new module, including
`--max-complexity=10`.

Eight modules were added (`ocr`, `receipts`, `fetch`, `fxlive`, `cards`,
`trends`, `upcoming`, `goals`) and two tables (`cards`, `goals`). What follows
is what the checklist found while doing it — most of it caught by the
structural guards the earlier passes left behind, which is the first time
those have paid for themselves on code that did not exist when they were
written.

## Change preventers — a private reached across a module boundary

`fxlive` needed to fetch a URL. `fxrates` already had the urllib-then-curl
ladder, as `_download`, so the two honest options were to reach into that
private name or to write the fallback twice — the exact pair of smells
`test_no_module_reaches_into_another_private_name` and the duplication rules
exist to prevent. Extracted to `fetch.py` instead, and the eighteen tests that
had been patching `fxrates._download` now patch `fetch.get`.

## Bloaters — three route groups and one validator

flake8 measured `_receipts` at 19, `_cards` at 18 and `_planning` at 12,
against a threshold of 10. Split into `_receipts` / `_receipt_files`,
`_cards` / `_card_settings` / `_card_insight`, and
`_planning` / `_goals_and_subscriptions`, with `_record_confirmed` and
`_reading_with_context` extracted to module level. Sixteen route groups now,
none over 10.

`cards.update` measured 11, and the cause was duplication rather than size:
`add` and `update` each validated the same four fields, and had **already
diverged** — update's fee message had lost half its explanation. One
`CLEANERS` table now serves both, so a rule enforced on creation cannot go
missing on correction.

## Dispensables — two unreachable branches

`receipts._one_amount` had a second guard for "a run of digits with no
separator", which could never fire: anything reaching it either had a currency
marker or had the two-decimal tail the line above insists on — and a tail is a
separator.

`cards.measured_fee` had `if not reference: return None`. `fxcost.compare`
returns None rather than a row with a zero reference, and every reference it
does return is `money.convert(abs(...))`, which is positive. Both removed. An
unreachable branch reads as a case somebody has thought about, which is worse
than no branch at all.

## The sign bug that mattered most

A card's balance summed two figures with different sign conventions.
`amount_eur` is signed — negative for an expense — while `charged_minor` is
stored by the importers as `abs(amount)`, a magnitude carrying no direction.
Taken at face value an exact row contributed **+8594** and an estimated one
**-572**, so the balance added a purchase and a purchase as though one were a
refund.

Fixed by taking the direction from the euro amount in both branches and
reporting spending positively, the way `budgets.status` already did. A refund
now nets off, which is the property the test pins.

## Unit-level bugs found by running the thing

Three, all found by pointing the parser at output from the real engine rather
than at input invented for it:

- **The date was never found.** There is no `\b` after the year in
  `8September2026at14:32` — "6" and "a" are both word characters. The engine
  eats spaces, so this is the common case, not an edge one. `(?!\d)` instead.
- **The card was never found.** The engine renders two bullets as a single
  `*`, and the mask pattern required two mask characters.
- **A euro purchase reported a conversion of itself.** The only amount on the
  screen is `-52,30 EUR`, `fxcost.foreign_amount` finds it, and reporting that
  as a foreign original invites comparing an amount against itself — which
  yields a confident zero-cost conversion that never happened.

And one in `ledger.recurring`, found by reading its output: requiring every
month to sit within tolerance of one mean threw away **exactly the case worth
flagging**, a 9.99 that became 14.99. It now accepts one price followed by
another, with `MIN_AT_EACH_LEVEL = 2` sightings at each level — without that
floor a transport spend of 60, 62, 59 then 20 was reported as a subscription
whose price had fallen to 20, and its 20 went into the projected monthly cost
of things that are not subscriptions at all.

## Abusers — a guard that had quietly stopped guarding

Every check in `test_frontend.py` read `static/js/app.js` **by name**. A
second script was added and none of them applied to it, so `finance.js`
shipped a second money formatter and a font size outside the type scale —
both things those tests exist to catch. The fixture now concatenates every
script the page loads, and a companion test asserts that list matches the
`script` tags, so a third file cannot silently escape.

Extending it found four real faults: the client-side cents formatter (removed;
`money.format` now sends the plain form), `toFixed(2)` on percentages
(removed; formatted in Python), an unescaped interpolation in the panel that
quotes the screenshot back (now text nodes, which cannot be markup), and five
unescaped values that were safe but exception-worthy.

The same omission applied to `tests/test_structure.py`, whose `MODULES` tuple
listed nine modules. Adding the eight new ones was one line, and immediately
found the private reach above.

## Speculative generality — deliberately not built

A "real time" rate was asked for. Both free sources republish the ECB's
once-a-day fixing, there is no free intraday EUR/CAD tick, and **a card is not
settled at the rate at the moment you tap** — Visa converts on the day it
processes the purchase. A live ticker would have been precision the situation
cannot use. What is built instead is the latest published rate, carrying its
publication date and the minute it was fetched, with a `daily_reference` flag
the page reads rather than a claim the page makes.

The model self-validates: ECB 1.6043 plus 2.5% predicts CA$86.00 against the
CA$85.94 CIBC actually billed.

## Bug classes

| Class | Instance | State |
|---|---|---|
| Sign | A balance summed a signed and an unsigned figure | Direction taken from `amount_eur` in both branches |
| Type | `spent_on` is text and `fxrates` compares dates, so it raised TypeError rather than RateError and escaped the handler | Coerced in `_day_of` |
| Regex | `\b` after a year the engine ran into the next word | `(?!\d)`, with the reason recorded beside it |
| Off-by-one | A single odd month read as a new price | `MIN_AT_EACH_LEVEL` |
| Injection | Path traversal through an uploaded filename | Names are a content digest; `stored_path` refuses anything else |
| Injection | Untrusted text into `innerHTML` | Text nodes in the one panel that quotes the screenshot |
| Validation | An image type taken from the upload's own filename | Sniffed from magic bytes |
| Resource | A 25 MB "screenshot" tying up a request | `MAX_BYTES`, checked before the engine is built |
| Environment | A 60 MB optional dependency in the critical path | Deferred import, `available()`, its own requirements file |
| Arithmetic | A percentage fee as a float beside an amount | Basis points, integer, one `CLEANERS` entry |

## Maintenance classification

**Corrective** — the sign bug, the three parser bugs, the recurring-detection
gap, and the two faults extending `test_frontend` exposed.

**Adaptive** — a schema that took two more tables and three more columns
without rewriting a row, through the same additive `LATER_COLUMNS` path.
`LATER_INDEXES` was added because an index on a migrated column cannot be
created before the migration runs, which broke opening every pre-cards
database until it was.

**Perfective** — `fetch.py`; the route-group splits; the `CLEANERS` table; two
unreachable branches removed; money formatting moved out of the browser.

**Preventive** — `test_the_page_loads_every_script_these_guards_check`,
`test_there_is_exactly_one_escaper`,
`test_the_deferred_import_allowance_is_not_a_blanket_one`, and the eight new
modules added to `MODULES`. Each was run against a planted fault: the layer
guard, the private-reach guard and the deferred-import guard were all
confirmed to go red on a deliberately broken copy before being trusted.


# Fourth pass — putting it on the web

Measured 9 September 2026: **759 tests, 100% of 2,346 statements**, no flake8
finding in any new code including `--max-complexity=10`.

The app was loopback-only with no authentication, deliberately, because it
holds a spending history. Hosting it is not a deployment task with a security
appendix; it is a change of threat model, and everything below follows from
that.

## The one decision the rest hangs off

**The app refuses to start reachable without a password.** `auth.guard` raises
`Unsafe` and the process exits. Not a warning, not a default that can be left
in place — a raise, at import, so `gunicorn wsgi:application` fails to boot
rather than serving an unprotected ledger.

    loopback, no password   -> runs, no login. What it always did.
    loopback, password set  -> runs, asks for it.
    anything else, no hash  -> refuses, and says what to set.

Every other measure here is a mitigation that can be argued about. This one is
the difference between a private ledger and a public one, and it is the only
mistake in this change that cannot be walked back. It is the first test in
`test_auth.py` and the first thing verified by mutation.

## Closed by default, not open by default

`OPEN_ENDPOINTS` is a frozenset of four names. Everything else needs a
session, and `test_every_route_is_closed_unless_it_is_named_open` walks the
real `url_map` rather than a list — so a route added next year is protected
because nobody opted it out, rather than exposed because somebody forgot to
opt it in. It asserts it actually checked more than fifteen routes, because a
loop that silently iterates nothing is the classic vacuous guard.

Endpoints rather than a path prefix: `/login` as a prefix would also open
`/login-anything`.

## CSRF, and why it is not optional here

19 of the 34 routes change state, and three take multipart uploads — which a
plain cross-origin HTML form can send, with no preflight to stop it. Once
there is a cookie carrying authority, a page on another site can spend it.

`SameSite=Lax` is set and helps, but it is a second lock. The lock is a token
in the session that must come back in a header, checked with
`hmac.compare_digest`, on every unsafe method.

It is enforced **always**, not only when a password is configured. On loopback
without auth there is still a real attack: a website you visit can make your
browser POST to `127.0.0.1:5004` and delete a transaction. That has been true
the whole time; it is closed now.

The cost was one `send()` wrapper in the browser and a `CsrfClient` in the
tests, which attaches the token so the 61 existing call sites read exactly as
they did. The tests that check the protection have to pass `no_csrf=True` —
opting *out* deliberately, so they cannot pass by accident.

## Guessing

Three free attempts, then a refusal window doubling from five seconds to a
capped fifteen minutes. Two details worth the words:

**Refused, not slept.** `time.sleep` in a login handler lets an attacker
exhaust the worker pool for free, which converts a guessing defence into a
denial of service. There is a test asserting the string does not appear.

**Capped.** Uncapped doubling eventually locks the owner out for years.

The reply is byte-identical however the guess was wrong — a test collects the
bodies for four kinds of wrong password into a set and asserts it has one
element, because anything that differs is a way to learn about the password.

## Smaller things the checklist caught

**An open redirect.** `?next=` after login. `//evil.example` is the case that
catches people: it looks like a path and is protocol-relative. `_safe_next`
refuses anything not starting with a single `/`.

**A 401 that a fetch can act on.** An API call without a session returns JSON
with `login: true` rather than a redirect to HTML — following the redirect
would fail JSON parsing and surface as a parse error instead of "signed out".
The browser reloads on it, landing on the login page.

**HSTS only when public.** Promising HTTPS on a loopback run that has none
makes the app unreachable in a browser that believes it.

**`Secure` only when public**, for the mirror-image reason: on plain HTTP the
cookie would never be sent, so login would appear to succeed and then bounce
straight back. It is called out in the README because it is the first thing
that will go wrong on a real deploy.

**`import getpass` inside `__main__`.** Caught by the existing
no-function-imports guard the moment `auth` was added to `MODULES` — hoisted
rather than exempted, since getpass is stdlib and free.

## Two branches that were reachable after all

Chasing the last 1% found the opposite of the previous pass. `password_matches`
has a `try/except ValueError` that looked defensive; `scrypt:1:2:3$salt$bad`
raises it, because the parameters parse as a hash and then do not work as one.
Without the except, a mangled environment variable is a 500 on every login —
which tells an attacker its configuration is broken. Kept, and now tested with
inputs that actually raise.

## What this does not do

There is no encryption at rest. Anyone who can read the disk can read the
database, and hosting means trusting the host with the file. That is stated in
the README rather than left for someone to assume otherwise, because it is a
real decision and it is the reason the app still defaults to your own machine.

## Bug classes

| Class | Instance | State |
|---|---|---|
| Authentication | A financial ledger reachable with no password | Refuses to start; first test, and mutation-checked |
| CSRF | 19 state-changing routes, three accepting multipart | Session token in a header, enforced on every unsafe method |
| Open redirect | `?next=//evil.example` after login | `_safe_next`, with the protocol-relative case tested |
| Information leak | A login reply that differs by how the guess was wrong | One body, asserted by set membership |
| Denial of service | Sleeping to slow down guessing | Refusal window; a test forbids `time.sleep` |
| Denial of service | An uncapped backoff locking the owner out | `MAX_BACKOFF_SECONDS` |
| Transport | `Secure` or HSTS on a loopback run | Both conditional on the host being public |
| Data loss | An ephemeral filesystem destroying the ledger on redeploy | `VOLUME` in the Dockerfile, and the first thing the hosting section says |
| Concurrency | The folder watcher running once per gunicorn worker | `wsgi.py` does not start it, and a test reads the file to confirm |

## Maintenance classification

**Corrective** — nothing; this is new behaviour rather than a fix.

**Adaptive** — the app now has two shapes, and `create_app(host=...)` makes
which one it is an argument rather than a global, so a test can hold both at
once.

**Perfective** — `send()` in the browser, so no call site carries the token
itself; `CsrfClient`, so no test does either.

**Preventive** — `test_every_route_is_closed_unless_it_is_named_open`,
`test_the_open_list_is_short_and_deliberate`, and the autouse
`clean_environment` fixture, which unsets every configuration variable for
every test. That last one is the transit project's bug pre-empted: a test that
asserts a default while reading the environment passes locally and fails for
anyone who has `HOST` exported.

All five protections were run against a deliberately broken copy — the
refusal, the CSRF check, the gate, the redirect guard and the backoff — and
each was confirmed to go red before being trusted.
