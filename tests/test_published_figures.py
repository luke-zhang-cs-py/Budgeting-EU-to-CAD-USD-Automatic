"""The published page says its figures were measured. This checks that.

`docs/index.html` is served to the public from GitHub Pages, and every number
on it -- the tiles, the charts, the module table -- is read from one DATA
block near the bottom of the file. The block carries a comment saying the
figures came from `pytest --cov` and `wc -l` and were "not estimated".

They had already gone stale once. An earlier version had them typed into the
markup and was wrong by 411 tests and 1,361 statements by the time anybody
looked, which is what the DATA block was introduced to fix. Moving them to
one place made them easier to correct but no harder to forget, so this file
measures the same things again and compares.

Everything here is measured statically -- `coverage`'s own analysis with this
project's `.coveragerc`, which reproduces the Stmts column exactly, and a
`--collect-only` run for the test counts. Nothing needs the suite to have
been run under coverage first, so the check works the same in CI.
"""
import io
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PAGE = os.path.join(ROOT, "docs", "index.html")


@pytest.fixture(scope="module")
def page():
    with io.open(PAGE, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def published_modules(page):
    """The module table as the page states it: name -> (lines, statements)."""
    found = re.findall(
        r"\{ name: '([\w.]+)',\s*lines:\s*(\d+),\s*stmts:\s*(\d+),"
        r"\s*missed: (\d+),", page)
    assert found, "the MODULES block could not be read at all"
    return {name: (int(lines), int(stmts), int(missed))
            for name, lines, stmts, missed in found}


@pytest.fixture(scope="module")
def measured_modules():
    """The same figures, measured the way the coverage report measures them."""
    import coverage
    config = coverage.Coverage(config_file=True)
    config.load()
    out = {}
    for name in sorted(os.listdir(ROOT)):
        if not name.endswith(".py"):
            continue
        _, statements, _, _, _ = config.analysis2(os.path.join(ROOT, name))
        with io.open(os.path.join(ROOT, name), encoding="utf-8") as handle:
            lines = len(handle.read().splitlines())
        out[name] = (lines, len(statements))
    return out


def test_the_page_lists_every_module(published_modules, measured_modules):
    """A module the table omits is worse than a wrong number: the chart of
    module sizes silently stops including it, and the totals under it are
    short by however much it holds."""
    assert set(published_modules) == set(measured_modules), (
        f"listed but gone: {sorted(set(published_modules) - set(measured_modules))}; "
        f"present but unlisted: "
        f"{sorted(set(measured_modules) - set(published_modules))}")


def test_every_module_figure_on_the_page_is_the_measured_one(
        published_modules, measured_modules):
    wrong = []
    for name, (lines, stmts, missed) in sorted(published_modules.items()):
        if (lines, stmts) != measured_modules[name]:
            wrong.append(f"{name}: page says {(lines, stmts)}, "
                         f"measured {measured_modules[name]}")
        if missed != 0:
            wrong.append(f"{name}: the page claims {missed} missed lines, "
                         f"and the table's caption says none are")
    assert not wrong, "the published figures are stale:\n  " + "\n  ".join(wrong)


def test_the_route_count_on_the_page_is_the_route_count(page):
    """ROUTE_COUNT feeds a headline tile. /simple was added and it kept
    saying 37."""
    with io.open(os.path.join(ROOT, "app.py"), encoding="utf-8") as handle:
        source = handle.read()
    claimed = int(re.search(r"var ROUTE_COUNT = (\d+);", page).group(1))
    assert claimed == source.count("@app.route")

    groups = int(re.search(r"var ROUTE_GROUPS = (\d+);", page).group(1))
    assert groups == len(re.findall(r"^def _\w+\(app, ctx\):", source,
                                    re.MULTILINE))


def test_every_test_file_and_its_count_are_the_real_ones(page):
    """The other half of the page: what the suite contains.

    A `--collect-only` run rather than counting `def test_` by hand, because
    a parameterised test is one definition and many tests -- and the busiest
    file here is parameterised, so counting definitions would understate it
    by twenty.
    """
    listed = dict((name, int(count)) for name, count in re.findall(
        r"\{ file: '([\w.]+)',\s*n:\s*(\d+),", page))
    assert listed, "the TESTS block could not be read at all"

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=600)
    real = {}
    for line in done.stdout.splitlines():
        if "::" in line:
            name = os.path.basename(line.split("::")[0])
            real[name] = real.get(name, 0) + 1

    assert real, ("collection produced nothing, so this test would pass "
                  "whatever the page said:\n" + done.stdout[-500:])
    assert listed == real, (
        f"the page's test table does not match the suite.\n"
        f"  page:     {sorted(listed.items())}\n"
        f"  collected {sorted(real.items())}")


def test_the_readme_quotes_the_real_counts(measured_modules):
    """The README's one sentence of figures, checked the same way.

    It is the first thing anybody reads and the easiest thing to leave
    behind: it said 759 tests and 2,346 statements while the suite had moved
    on from both.
    """
    with io.open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
        readme = handle.read()

    claim = re.search(r"([\d,]+) tests, 100% of ([\d,]+) statements", readme)
    assert claim, ("the README no longer states a test count in the form "
                   "this check reads, so it is no longer being checked")

    statements = sum(stmts for _, stmts in measured_modules.values())
    assert int(claim.group(2).replace(",", "")) == statements, (
        f"the README says {claim.group(2)} statements, measured "
        f"{statements:,}")

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=600)
    collected = sum(1 for line in done.stdout.splitlines() if "::" in line)
    assert collected, "collection produced nothing to compare against"
    assert int(claim.group(1).replace(",", "")) == collected, (
        f"the README says {claim.group(1)} tests, collected {collected}")


# The page's own history, told in three places: what the figures said before
# they were moved into the data block. These are the only counts on the page
# allowed to be wrong, because being wrong is what they describe.
HISTORY = (
    "411 tests and 1,361 statements",
    "288 tests and 834 statements",
    "wrong by 411 tests",
)


def test_no_count_is_typed_into_the_page(page):
    """The tiles and the captions add the arrays up in the page itself, so
    there is no hand-written total to check -- and there must not be one.

    A number typed into a sentence beside a number computed from the data is
    how the two come to disagree, which is the failure this whole file exists
    to prevent. So rather than checking that a written total is right, this
    checks there is none: a count may appear inside the DATA block, and
    everywhere else must derive it. The `og:description` tag said 759 tests
    long after the suite had passed it twice, precisely because nothing on
    the page read that tag.
    """
    body = re.sub(r"var MODULES = \[.*?\n\];", "", page, flags=re.DOTALL)
    body = re.sub(r"var TESTS = \[.*?\n\];", "", body, flags=re.DOTALL)
    for sentence in HISTORY:
        assert sentence in page, (
            f"the history this check exempts is no longer on the page: "
            f"{sentence!r} -- remove it from HISTORY too")
        body = body.replace(sentence, "")

    typed = re.findall(r"[\d,]{3,}\s*(?:executable\s+)?(?:statements|tests)",
                       body)
    assert not typed, (
        f"these counts are typed into the page rather than computed from "
        f"the data block: {typed}")
