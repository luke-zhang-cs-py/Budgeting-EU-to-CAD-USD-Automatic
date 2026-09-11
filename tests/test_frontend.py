"""The browser half, checked structurally.

Transaction descriptions come out of a bank CSV and go into the page. That is
untrusted input by any reasonable definition, and the face-recognition app in
this family shipped a stored XSS hole by putting registered names straight
into innerHTML. This is the cheap guard against repeating it: not a browser
test, just a check that no field is interpolated raw.

The checks run over every bundle of browser code in the project, not one of
them. That is the lesson this file has already been taught twice: finance.js
shipped a second money formatter and an off-scale font size because every
guard here read app.js by name, and then the published capture page arrived
as a third bundle with its own script and its own stylesheet. A guard that
names a file stops guarding the moment another one appears, so what is named
here is the *list of bundles*, and the last test in the file fails if a page
in the project belongs to no bundle at all.
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _at(*parts):
    return os.path.join(ROOT, *parts)


# The Flask app: two views, served templates, and common.js holding the one
# copy of the helpers both of them use.
APP = {
    "name": "app",
    "pages": [_at("templates", "index.html"), _at("templates", "simple.html")],
    "scripts": [_at("static", "js", name) for name in
                ("common.js", "app.js", "finance.js", "simple.js")],
    "styles": [_at("static", "css", "style.css")],
    "in_page": r"filename='js/([A-Za-z0-9_.-]+)'",
    # Ids the script creates itself rather than finding in the template.
    "created": {"expensesPositive"},
    # Names holding a fragment that was already built and escaped. Composing
    # two of those is safe, and escaping one a second time would double every
    # ampersand in it -- which is why the list exists rather than a blanket
    # "everything must be esc()".
    "fragments": (r"esc\(|\(share|\(Math|width|options|pickers|flag|pace|lag|"
                  r"rate\b|picker|direction|query|month|search|rowsHtml|"
                  r"sparkHtml"),
    # One deliberate exception to the type scale: the small uppercase tag.
    "font_literals": {"10.5"},
}

# The published static page. It cannot load common.js: it is served by GitHub
# Pages with no Flask behind it, so it carries its own small el/esc/say and
# its own stylesheet. That duplication is real, and the answer is to point
# the same guards at it rather than to leave it unchecked.
CAPTURE = {
    "name": "capture",
    "pages": [_at("docs", "capture", "index.html")],
    "scripts": [_at("docs", "capture", name) for name in
                ("rules.js", "app.js")],
    "styles": [_at("docs", "capture", "style.css")],
    "in_page": r'<script src="([A-Za-z0-9_.-]+)"',
    "created": set(),
    "fragments": r"esc\(",
    "font_literals": {"10.5"},
}

BUNDLES = (APP, CAPTURE)


def _read(paths):
    out = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            out.append(handle.read())
    return "\n".join(out)


def _code(source):
    """JavaScript with its block comments removed.

    The same helper test_structure.py has for Python, and here for the same
    reason: a check that greps source keeps failing on prose describing the
    very thing it searches for. This one caught the comment in common.js
    explaining that `toFixed(2)` is forbidden -- which is documentation, not
    a second money formatter.

    Only block comments. This codebase uses them throughout, and stripping
    `//` would also eat the `https://` in a font URL.
    """
    return re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)


def _script(name):
    """One of the app's scripts, by filename -- for the checks that are about
    how the app's own files divide work between them."""
    return _read([_at("static", "js", name)])


@pytest.fixture(params=BUNDLES, ids=[spec["name"] for spec in BUNDLES])
def bundle(request):
    """One bundle of browser code, with its files already read."""
    spec = dict(request.param)
    spec["js"] = _read(spec["scripts"])
    spec["html"] = _read(spec["pages"])
    spec["css"] = _read(spec["styles"])
    return spec


# ------------------------------------------------------------ every bundle


def test_the_pages_load_every_script_these_guards_check(bundle):
    """The guard on the fixture. Concatenating the files is only a check if
    the list matches what the pages actually load -- otherwise a script is
    added, nobody updates the list, and the checks silently cover part of
    the client."""
    loaded = set(re.findall(bundle["in_page"], bundle["html"]))
    named = {os.path.basename(path) for path in bundle["scripts"]}
    assert loaded == named, (
        f"the {bundle['name']} pages load {sorted(loaded)} but the guards "
        f"read {sorted(named)}")


def test_an_escaping_helper_exists(bundle):
    assert "function esc(" in bundle["js"], f"{bundle['name']} has no escaper"
    for char in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert char in bundle["js"], f"esc does not handle {char}"


def test_there_is_exactly_one_escaper(bundle):
    """Within a bundle. Two escapers means one of them eventually stops being
    the one that gets fixed, and this family has already shipped a stored XSS
    hole once."""
    assert bundle["js"].count("function esc(") == 1


# The fields that carry text somebody else wrote. A bank can and does put
# ampersands and angle brackets in a merchant name.
UNTRUSTED = ("description", "merchant", "category", "keyword", "why",
             "rate_note", "last_seen")


@pytest.mark.parametrize("field", UNTRUSTED)
def test_untrusted_fields_are_never_interpolated_raw(bundle, field):
    """Looks for `+ row.field +` outside an esc(...) call.

    A crude check that would miss a determined workaround, and that is fine:
    it catches the accident, which is what actually happens.
    """
    raw = re.findall(r"\+\s*(?:row|rule|p|body)\.%s\s*\+" % field,
                     bundle["js"])
    assert not raw, f"{field} interpolated without esc(): {raw}"


def test_every_interpolated_value_in_a_template_string_is_escaped(bundle):
    """Every `+ something +` inside an HTML-building expression should be an
    esc() call, a number, or a fragment that was escaped when it was built."""
    allowed = re.compile(r"\+\s*(?:%s)" % bundle["fragments"])
    for line in bundle["js"].splitlines():
        stripped = line.strip()
        if "'<" not in stripped and '"<' not in stripped:
            continue
        for hit in re.findall(r"\+\s*[A-Za-z_$][\w$.]*", stripped):
            if not allowed.match(hit.strip()) and "esc(" not in stripped:
                pytest.fail(f"unescaped interpolation in: {stripped}")


def test_the_page_and_the_script_agree_on_the_ids_used(bundle):
    """A typo in an id is a silent no-op in JavaScript: el('spendEur') just
    returns null and the figure never updates."""
    declared = set(re.findall(r'id="([A-Za-z0-9_-]+)"', bundle["html"]))
    used = set(re.findall(r"el\('([A-Za-z0-9_-]+)'\)", bundle["js"]))
    missing = used - declared - bundle["created"]
    assert not missing, (
        f"the {bundle['name']} script looks for ids the page does not have: "
        f"{missing}")


def test_no_page_carries_an_id_nothing_uses(bundle):
    """Dead markup, caught structurally.

    The reverse of the check above, and the more useful direction. An element
    nobody writes to is either litter or a missing feature: the Tally app in
    this family had one of each, and the stripped-back view here shipped
    three decorative ids plus one named for a job it had stopped doing.

    An id counts as used if it appears quoted anywhere in the scripts, not
    just inside el(...) -- showNotes('needs', ...) passes one as an argument,
    and a check that only matched el() would call it dead.
    """
    declared = set(re.findall(r'id="([A-Za-z][\w-]*)"', bundle["html"]))
    # `for=` and `list=` point at an id from the markup itself, and CSS may
    # select one; neither needs a script.
    styled_or_linked = set(re.findall(
        r'(?:for|list|aria-labelledby)="([A-Za-z][\w-]*)"', bundle["html"]))
    used = set()

    for name in declared:
        if f"'{name}'" in bundle["js"] or f'"{name}"' in bundle["js"]:
            used.add(name)
        elif f"#{name}" in bundle["css"]:
            styled_or_linked.add(name)

    unused = sorted(declared - used - styled_or_linked)
    assert not unused, (
        f"nothing in the {bundle['name']} bundle uses these ids: {unused}")


def test_the_stylesheet_has_one_type_scale(bundle):
    """The transit app grew a second scale by accident -- a 10px label among
    11.5px figures. Every font-size must come from a variable."""
    literals = re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", bundle["css"])
    assert set(literals) <= bundle["font_literals"], (
        f"hardcoded font sizes in {bundle['name']}: {literals}")


def test_the_hidden_attribute_actually_hides(bundle):
    """A page that ships elements `hidden` must enforce it in CSS.

    The browser's own `[hidden] { display: none }` is a user agent rule, and
    any author rule that sets `display` beats it -- so `.progress
    { display: grid }` drew an empty progress track under the drop zone
    before an image was ever chosen, and `.shotImage { display: block }` drew
    an empty image box above the form on both of the app's views. In every
    case the markup said `hidden`, the script set `.hidden = true`, and
    neither did anything.

    Both were found by looking at the rendered page rather than by any test,
    which is the argument for this one.
    """
    shipped = re.findall(r"<[^>]*\bid=\"([A-Za-z][\w-]*)\"[^>]*\bhidden",
                         bundle["html"])
    assert shipped, (
        "no element in this bundle ships hidden, so either the markup "
        "changed shape or this check is no longer reading it")

    enforced = re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important",
                         bundle["css"])
    assert enforced, (
        f"the {bundle['name']} bundle ships {sorted(set(shipped))} hidden, "
        f"but its stylesheet does not enforce [hidden] with !important -- "
        f"any display rule on one of them silently shows it")


def test_money_is_set_in_a_monospace_face(bundle):
    """Money in a proportional font makes columns fail to line up, which is
    the one thing a column of money has to do."""
    assert "--mono" in bundle["css"]
    assert "tabular-nums" in bundle["css"]
    assert ".figure" in bundle["css"] and "var(--mono)" in bundle["css"]


# ------------------------------------------------------------ the app only


def test_both_views_share_the_helpers_rather_than_copying_them():
    """common.js exists so there is one escaper, one request wrapper and one
    place the CSRF token is read. If either view declares its own, they have
    started to diverge.

    This is about the app's two served views. The capture page is left out on
    purpose: it has no server to fetch a shared file from, which is why the
    checks above run over it as its own bundle rather than expecting it to
    load common.js.
    """
    shared = _script("common.js")
    for name in ("esc", "el", "say", "send", "api", "postJson"):
        assert f"function {name}(" in shared, f"{name} is not in common.js"
    for page_script in ("app.js", "simple.js", "finance.js"):
        body = _script(page_script)
        for name in ("esc", "send", "api"):
            assert f"function {name}(" not in body, (
                f"{page_script} declares its own {name}")


def test_the_client_does_not_reimplement_money_formatting():
    """Amounts arrive already formatted. Formatting them again here would be
    a second implementation of the same rounding, and the two would drift --
    which is exactly how a total comes to disagree with its own rows.

    The app only. The capture page has no server to format anything for it,
    so it necessarily does its own -- and that arithmetic is checked against
    money.py by the shared fixture in test_shared_rules.py, which is a
    stronger guarantee than this grep rather than a weaker one.
    """
    code = _code(_read(APP["scripts"]))
    assert "toFixed(2)" not in code
    assert "/ 100" not in code


# ------------------------------------------------------------- the controls


def test_that_check_reads_code_and_not_prose():
    """The negative control. Stripping comments is only right if the check
    still fails on a real reimplementation, so this holds one and asserts it
    is caught -- and holds the comment that tripped it and asserts it is
    not."""
    real = "function show(c) { return (c / 100).toFixed(2); }"
    assert "toFixed(2)" in _code(real), "a real one must still be caught"

    prose = "/* There is a test forbidding toFixed(2) and / 100 here. */"
    assert "toFixed(2)" not in _code(prose), "a comment must not count"


def test_that_check_would_notice_a_dead_id():
    """The negative control, kept rather than run once by hand. A guard is
    worth nothing until it has been seen to fail."""
    js = "el('real');"
    declared = {"real", "litter"}
    used = {name for name in declared if f"'{name}'" in js}
    assert used == {"real"}
    assert sorted(declared - used) == ["litter"], (
        "the check would not have spotted the dead one")


def test_every_page_in_the_project_belongs_to_a_bundle():
    """The guard on the list of bundles.

    Every check above runs over BUNDLES, so a page with its own script that
    nobody added to the list would be covered by none of them -- which is
    precisely how finance.js escaped this file once already. This walks the
    project for HTML that loads a script and asserts each such page is
    claimed.
    """
    claimed = {os.path.abspath(page)
               for spec in BUNDLES for page in spec["pages"]}
    found = []
    for folder in ("templates", "docs", "static"):
        for here, _, names in os.walk(_at(folder)):
            found.extend(os.path.join(here, name) for name in names
                         if name.endswith(".html"))

    assert found, "nothing was searched -- the walk found no pages at all"
    loose = []
    for path in found:
        with open(path, encoding="utf-8") as handle:
            page = handle.read()
        if "<script" not in page:
            continue                   # a page with no JS needs no bundle
        # A page that loads no file and asks nothing for data is exempt, and
        # the exemption is this property rather than a filename: every figure
        # in docs/index.html is written in the file itself, so there is no
        # untrusted input for an escaper to handle and no shared logic to
        # drift from. Give it a script file or a fetch and it stops being
        # exempt, which is the point.
        if ("<script src=" not in page and "fetch(" not in page
                and "XMLHttpRequest" not in page):
            continue
        if os.path.abspath(path) not in claimed:
            loose.append(os.path.relpath(path, ROOT))

    assert not loose, (
        f"these pages load scripts but no bundle claims them, so none of the "
        f"checks in this file apply to them: {sorted(loose)}")
