"""The browser half, checked structurally.

Transaction descriptions come out of a bank CSV and go into the page. That is
untrusted input by any reasonable definition, and the face-recognition app in
this family shipped a stored XSS hole by putting registered names straight
into innerHTML. This is the cheap guard against repeating it: not a browser
test, just a check that no field is interpolated raw.
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Every script the page loads, not just the first one. finance.js was added
# with the screenshot reader and none of these guards applied to it, because
# they all read app.js by name -- so it shipped a second money formatter and
# a font size outside the type scale, both of which these tests exist to
# catch. A guard that names one file stops guarding the moment a second
# appears.
SCRIPTS = ("app.js", "finance.js")
CSS = os.path.join(ROOT, "static", "css", "style.css")
HTML = os.path.join(ROOT, "templates", "index.html")


def _script(name):
    with open(os.path.join(ROOT, "static", "js", name),
              encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def js():
    """Every script, concatenated, so a check cannot miss one of them."""
    return "\n".join(_script(name) for name in SCRIPTS)


def test_the_page_loads_every_script_these_guards_check():
    """The other half of the fixture above. Concatenating the files is only
    a guard if the list matches what the page actually loads -- otherwise a
    third script is added, nobody updates SCRIPTS, and the checks silently
    cover two thirds of the client."""
    with open(HTML, encoding="utf-8") as handle:
        html = handle.read()
    loaded = set(re.findall(r"filename='js/([A-Za-z0-9_.-]+)'", html))
    assert loaded == set(SCRIPTS), (
        f"the page loads {sorted(loaded)} but the guards read "
        f"{sorted(SCRIPTS)}")


def test_an_escaping_helper_exists(js):
    assert "function esc(" in js
    for char in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert char in js, f"esc does not handle {char}"


# The fields that carry text somebody else wrote. A bank can and does put
# ampersands and angle brackets in a merchant name.
UNTRUSTED = ("description", "merchant", "category", "keyword", "why",
             "rate_note", "last_seen")


@pytest.mark.parametrize("field", UNTRUSTED)
def test_untrusted_fields_are_never_interpolated_raw(js, field):
    """Looks for `+ row.field +` outside an esc(...) call.

    A crude check that would miss a determined workaround, and that is fine:
    it catches the accident, which is what actually happens.
    """
    raw = re.findall(r"\+\s*(?:row|rule|p|body)\.%s\s*\+" % field, js)
    assert not raw, f"{field} interpolated without esc(): {raw}"


def test_every_interpolated_value_in_a_template_string_is_escaped(js):
    """Every `+ something +` inside an HTML-building expression should be an
    esc() call, a number, or a computed style width."""
    # Names holding a fragment that was already built and escaped. Composing
    # two of those is safe, and escaping one a second time would double every
    # ampersand in it -- which is why the list exists rather than a blanket
    # "everything must be esc()".
    allowed = re.compile(
        r"\+\s*(esc\(|\(share|\(Math|width|options|pickers|flag|pace|lag|"
        r"rate\b|picker|direction|query|month|search|rowsHtml|sparkHtml)")
    for line in js.splitlines():
        stripped = line.strip()
        if "'<" not in stripped and '"<' not in stripped:
            continue
        for hit in re.findall(r"\+\s*[A-Za-z_$][\w$.]*", stripped):
            if not allowed.match(hit.strip()) and "esc(" not in stripped:
                pytest.fail(f"unescaped interpolation in: {stripped}")


def test_the_page_and_the_stylesheet_agree_on_the_ids_used(js):
    """A typo in an id is a silent no-op in JavaScript: el('spendEur') just
    returns null and the figure never updates."""
    with open(HTML, encoding="utf-8") as handle:
        html = handle.read()
    declared = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
    used = set(re.findall(r"el\('([A-Za-z0-9_-]+)'\)", js))
    # Ids the script creates itself rather than finding in the template.
    created = {"expensesPositive"}
    missing = used - declared - created
    assert not missing, f"script looks for ids the page does not have: {missing}"


def test_the_stylesheet_has_one_type_scale():
    """The transit app grew a second scale by accident -- a 10px label among
    11.5px figures. Every font-size here must come from a variable."""
    with open(CSS, encoding="utf-8") as handle:
        css = handle.read()
    literals = re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", css)
    # One deliberate exception: the small uppercase tag.
    assert set(literals) <= {"10.5"}, f"hardcoded font sizes: {literals}"


def test_there_is_exactly_one_escaper(js):
    """finance.js reuses app.js's esc rather than declaring its own. Two
    escapers means one of them eventually stops being the one that gets
    fixed, and this family has already shipped a stored XSS hole once."""
    assert js.count("function esc(") == 1


def test_money_is_set_in_a_monospace_face():
    """Money in a proportional font makes columns fail to line up, which is
    the one thing a column of money has to do."""
    with open(CSS, encoding="utf-8") as handle:
        css = handle.read()
    assert "--mono" in css
    assert "tabular-nums" in css
    assert ".figure" in css and "var(--mono)" in css


def test_the_client_does_not_reimplement_money_formatting(js):
    """Amounts arrive already formatted. Formatting them again here would be
    a second implementation of the same rounding, and the two would drift --
    which is exactly how a total comes to disagree with its own rows."""
    assert "toFixed(2)" not in js
    assert "/ 100" not in js
