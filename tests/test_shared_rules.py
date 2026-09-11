"""The one piece of logic that exists twice, and the fixture that holds it.

`receipts.py` reads a purchase out of OCR text on the server.
`docs/capture/rules.js` does the same in the browser, because the published
static page has no server to ask.

Two implementations of anything drift. So the JS one is not trusted to stay in
step -- it is checked. `docs/capture/cases.json` holds the cases and the
answers, and there are two tests here:

  * the Python side still produces those answers, so the fixture cannot rot
    silently while the reference implementation moves;
  * the JavaScript side produces the same ones, run in a real browser.

If either side changes a rule and not the other, one of these fails. That is
the entire justification for allowing the duplication at all -- without it,
the page and the app would eventually disagree about what an amount is, and
nothing would say so.

The last test here checks the other half of that seam: the static page's only
output is a CSV file, and the wallet's importer is the only thing that reads
it. That test writes the file with the shipped `RULES.csv` in a real browser
and reads it back with the shipped importer, so neither side can rename a
column or flip the sign of an expense without it failing.
"""
import datetime as dt
import html
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db          # noqa: E402
import importers   # noqa: E402
import ledger      # noqa: E402
import ocr         # noqa: E402
import receipts    # noqa: E402

FIXTURE = os.path.join(ROOT, "docs", "capture", "cases.json")
RULES_JS = os.path.join(ROOT, "docs", "capture", "rules.js")

# Where a browser might be. Checked rather than assumed, and the JS tests skip
# when there is none -- CI has no browser, and a skip that says why is better
# than a suite that cannot run there.
BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


def browser():
    for path in BROWSERS:
        if os.path.isfile(path):
            return path
    return shutil.which("google-chrome") or shutil.which("chromium")


@pytest.fixture(scope="module")
def fixture():
    with io.open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def rules():
    with io.open(RULES_JS, encoding="utf-8") as handle:
        return handle.read()


def in_browser(script, marker):
    """Run `script` after rules.js in headless Chrome, return what it printed.

    --dump-dom rather than a JS test runner: there is no node on this machine,
    and the browser is the environment rules.js actually runs in, which makes
    it the right place to check it. The script is expected to put
    `marker + result` into #out.
    """
    with io.open(RULES_JS, encoding="utf-8") as handle:
        source = handle.read()

    page = ('<!doctype html><meta charset="utf-8"><body><pre id="out"></pre>\n'
            '<script>%s</script>\n<script>%s</script></body>'
            % (source, script))

    folder = tempfile.mkdtemp()
    try:
        path = os.path.join(folder, "check.html")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(page)
        done = subprocess.run(
            [browser(), "--headless", "--disable-gpu", "--no-sandbox",
             "--no-first-run", "--no-default-browser-check",
             # Its own profile, not the one the developer is browsing in. With
             # the default profile this returned an empty page whenever a real
             # Chrome window was open, which made the test fail for a reason
             # that had nothing to do with the code under test.
             "--user-data-dir=" + os.path.join(folder, "profile"),
             "--virtual-time-budget=5000", "--dump-dom",
             "file:///" + path.replace("\\", "/")],
            capture_output=True, timeout=180)
        dom = done.stdout.decode("utf-8", "replace")
        noise = done.stderr.decode("utf-8", "replace")[-400:]
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    assert marker in dom, (
        "the page did not run -- a syntax error in rules.js would do this.\n"
        f"  exit {done.returncode}, {len(dom)} bytes of DOM\n  {noise}")
    return html.unescape(dom.split(marker, 1)[1].split("</pre>")[0]).strip()


def only_the_shared_fields(parsed):
    """The fields both implementations are expected to agree on.

    The Python side also reports the bank-disclosed conversion, which the
    static page has no use for and does not implement. Comparing everything
    would fail on a difference that is deliberate.
    """
    amount, date = parsed["amount"], parsed["date"]
    return {
        "amount": None if not amount else {
            "minor": amount["minor"], "plain": amount["plain"],
            "currency": amount["currency"]},
        "amounts": [a["minor"] for a in parsed["amounts"]],
        "date": None if not date else {
            "iso": date["iso"], "ambiguous": date["ambiguous"],
            "alternative": date["alternative"]},
        "merchant": parsed["merchant"],
        "card_mask": parsed["card_mask"],
        "needs": sorted(parsed["needs"]),
    }


def test_the_fixture_is_not_empty(fixture):
    """A guard on the guard. Both tests below loop over this file, and a loop
    over nothing passes -- which is how a structural test comes to cover
    nothing at all."""
    assert len(fixture["cases"]) >= 20
    assert fixture["today"] == "2026-09-09"


def test_the_python_side_still_matches_the_fixture(fixture):
    """So the fixture cannot rot while receipts.py moves on."""
    today = dt.date.fromisoformat(fixture["today"])
    for case in fixture["cases"]:
        boxes = [ocr.Box(**b) for b in case["boxes"]]
        got = only_the_shared_fields(receipts.parse(boxes, today=today))
        assert got == case["expect"], case["why"]


@pytest.mark.skipif(not browser(), reason="no browser to run the JS in")
def test_the_javascript_side_matches_the_fixture(fixture):
    """The JS port, run in a real browser against the same cases."""
    script = """
var FIXTURE = %s;
var failures = [];
FIXTURE.cases.forEach(function (item) {
  var when = new Date(FIXTURE.today + 'T12:00:00Z');
  var got = RULES.parse(item.boxes, when);
  var mine = {
    amount: got.amount ? { minor: got.amount.minor, plain: got.amount.plain,
                           currency: got.amount.currency } : null,
    amounts: got.amounts.map(function (a) { return a.minor; }),
    date: got.date ? { iso: got.date.iso, ambiguous: got.date.ambiguous,
                       alternative: got.date.alternative } : null,
    merchant: got.merchant,
    card_mask: got.card_mask,
    needs: got.needs.slice().sort()
  };
  if (JSON.stringify(mine) !== JSON.stringify(item.expect)) {
    failures.push(item.why + ' js: ' + JSON.stringify(mine) +
                  ' py: ' + JSON.stringify(item.expect));
  }
});
document.getElementById('out').textContent =
  'RESULT ' + (failures.length ? failures.join(' | ') : 'ALL MATCH');
""" % json.dumps(fixture)

    verdict = in_browser(script, "RESULT ")
    assert verdict == "ALL MATCH", (
        "the browser and the server disagree:\n  " + verdict)


@pytest.mark.skipif(not browser(), reason="no browser to run the JS in")
def test_that_check_would_notice_a_disagreement(fixture):
    """The negative control. A cross-implementation check is worth nothing
    until it has been seen to fail, so this hands the browser a fixture with
    one wrong answer in it and asserts the comparison rejects it."""
    broken = json.loads(json.dumps(fixture))
    for case in broken["cases"]:
        if case["expect"]["amount"]:
            case["expect"]["amount"]["minor"] += 1
            break
    else:                                        # pragma: no cover
        pytest.fail("no case in the fixture has an amount to break")

    script = """
var FIXTURE = %s;
var bad = 0;
FIXTURE.cases.forEach(function (item) {
  var got = RULES.parse(item.boxes, new Date(FIXTURE.today + 'T12:00:00Z'));
  var mine = got.amount ? got.amount.minor : null;
  var want = item.expect.amount ? item.expect.amount.minor : null;
  if (mine !== want) { bad += 1; }
});
document.getElementById('out').textContent = 'MISMATCHES ' + bad;
""" % json.dumps(broken)

    count = in_browser(script, "MISMATCHES ")
    assert count == "1", (
        f"a planted wrong answer produced {count} mismatches, not one -- "
        f"the comparison is not doing what it claims")


# What the page exports, and what the wallet does with it. Two purchases: a
# plain one, and one whose description carries a comma and a quote mark, which
# is where a hand-rolled CSV writer goes wrong.
EXPORTED = [
    {"date": "2026-09-08", "description": "REWE SAGT DANKE",
     "category": "", "minor": 5230},
    {"date": "2026-09-09", "description": 'Cafe "Nord", Berlin',
     "category": "Coffee", "minor": 380},
]


@pytest.mark.skipif(not browser(), reason="no browser to run the JS in")
def test_the_file_this_page_writes_imports_into_the_wallet(tmp_path):
    """The hand-off, end to end and in both real implementations.

    The static page cannot talk to the wallet -- it has no server and says so.
    The CSV is the whole of the connection between them, which makes the
    column names and the sign of an expense a contract. So the file is written
    by the shipped RULES.csv in a browser and read by the shipped importer
    here: rename a column on either side, or export a spend as positive, and
    this fails.
    """
    script = ("document.getElementById('out').textContent = 'CSV ' + "
              "JSON.stringify(RULES.csv(%s));" % json.dumps(EXPORTED))
    text = json.loads(in_browser(script, "CSV "))

    assert text.startswith("Date,Description,Amount,Currency\n")

    shape = importers.preview(importers.sniff(text))
    assert shape["problems"] == []
    assert shape["unreadable"] == 0, (
        "the wallet could not read a row the page wrote")

    # Every column recognised on its own, with no mapping supplied by hand:
    # that is the property that lets the user import the file without being
    # asked which column is which.
    mapping = shape["mapping"]
    assert [mapping[name] for name in
            ("date", "description", "amount", "currency")] == \
        ["Date", "Description", "Amount", "Currency"]
    assert mapping["expenses_positive"] is False, (
        "the page writes expenses as negative -- if the importer reads them "
        "as positive, every captured purchase lands as income")

    connection = db.connect(str(tmp_path / "wallet.db"))
    added = importers.load(connection, shape, source="capture.csv")
    assert added["added"] == len(EXPORTED)
    assert added["failed"] == []
    assert added["duplicate"] == 0

    stored = {row["description"]: row["amount_eur"]
              for row in ledger.transactions(connection, limit=10)}
    assert stored == {
        "REWE SAGT DANKE": -5230,
        'Cafe "Nord", Berlin - Coffee': -380,
    }, "money out, at the amount read, with the quoting survived"
