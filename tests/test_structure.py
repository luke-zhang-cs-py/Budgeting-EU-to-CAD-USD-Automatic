"""The extracted shared code, and structural guards against it coming back.

Every test here corresponds to a smell that was really in this repository.
They are cheap, and they fail on the accident rather than on a determined
workaround -- which is the failure mode that actually happens.
"""
import datetime as dt
import inspect
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import budgets    # noqa: E402
import db         # noqa: E402
import export     # noqa: E402
import fxrates    # noqa: E402
import importers  # noqa: E402
import ledger     # noqa: E402
import money      # noqa: E402
import paths      # noqa: E402

MODULES = (money, paths, db, fxrates, ledger, importers, budgets, export)


def _code_of(module):
    """A module's source with docstrings and comments removed.

    Structural tests that grep raw source keep failing on prose that describes
    the very thing being searched for -- three separate times while auditing
    these projects. Stripping first is the fix, and it belongs in one helper
    rather than in each test that needs it.
    """
    source = inspect.getsource(module)
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    source = re.sub(r"'''(?:.|\n)*?'''", "", source)
    return re.sub(r"#[^\n]*", "", source)


@pytest.fixture(autouse=True)
def fresh():
    fxrates.reset()
    yield
    fxrates.reset()


# ------------------------------------------------------- one data directory

def test_the_database_and_the_rate_cache_agree_on_where_they_live(tmp_path):
    """They each resolved this independently and each read WALLET_DATA for
    itself, so nothing forced them to agree. A ledger read from one directory
    and converted with rates from another would look entirely normal."""
    assert os.path.dirname(db.db_path(str(tmp_path))) == \
        os.path.dirname(fxrates.cache_path(str(tmp_path)))


def test_only_one_module_reads_the_data_directory_variable():
    """The structural half. Any module resolving WALLET_DATA for itself is
    that divergence starting again."""
    culprits = []
    for module in MODULES:
        if module is paths:
            continue
        source = inspect.getsource(module)
        # Ignore prose: only flag it being read.
        if re.search(r'environ(\.get)?\(?\[?["\']WALLET_DATA', source):
            culprits.append(module.__name__)
    assert not culprits, f"these resolve WALLET_DATA themselves: {culprits}"


def test_an_explicit_directory_beats_the_environment(monkeypatch, tmp_path):
    """A caller that knows where it wants to work must not be overridden by a
    variable it did not set -- which is the whole mechanism the tests use."""
    monkeypatch.setenv("WALLET_DATA", str(tmp_path / "from-env"))
    assert paths.data_dir(str(tmp_path / "explicit")) == \
        str(tmp_path / "explicit")


def test_with_nothing_set_it_lands_beside_the_code(monkeypatch):
    monkeypatch.delenv("WALLET_DATA", raising=False)
    assert paths.data_dir().endswith(paths.DEFAULT_DIRNAME)


# ------------------------------------------------------- one conversion loop

def test_convert_all_answers_for_every_target_currency(tmp_path):
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("date,CAD,USD\n2026-02-02,1.6033,1.1614\n")

    out = fxrates.convert_all(5000, dt.date(2026, 2, 2), str(tmp_path))
    assert set(out) == set(money.TARGETS)
    assert out["CAD"]["cents"] == 8017
    assert out["USD"]["cents"] == 5807
    assert out["CAD"]["lag_days"] == 0
    assert out["CAD"]["why"] == ""


def test_convert_all_reports_a_failure_per_currency_rather_than_raising(
        tmp_path):
    """None and a reason, never a zero. Zero in a money column is
    indistinguishable from a free purchase."""
    out = fxrates.convert_all(5000, dt.date(2026, 2, 2), str(tmp_path))
    for currency in money.TARGETS:
        assert out[currency]["cents"] is None
        assert out[currency]["why"]


def test_convert_all_carries_the_business_day_lag(tmp_path):
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        # The real Easter 2026 shape: 2 April, then nothing until the 7th.
        # The 7th has to be in the cache, or the 6th is later than the newest
        # rate and is refused as a guess rather than looked back for -- which
        # is correct, and is what the first version of this test tripped on.
        handle.write("date,CAD,USD\n2026-04-07,1.6100,1.1700\n"
                     "2026-04-02,1.6033,1.1614\n")
    out = fxrates.convert_all(5000, dt.date(2026, 4, 6), str(tmp_path))
    assert out["CAD"]["used"] == dt.date(2026, 4, 2)
    assert out["CAD"]["lag_days"] == 4


def test_neither_caller_keeps_its_own_conversion_loop():
    """ledger and budgets each had one, and the two had already drifted: one
    caught (RateError, MoneyError) and the other also caught ValueError, so
    the same bad date failed differently depending on the screen."""
    for module in (ledger, budgets):
        source = inspect.getsource(module)
        assert "for currency in money.TARGETS" not in source, \
            f"{module.__name__} still loops over the currencies itself"
        assert "convert_all" in source


# ------------------------------------------------------- one money formatter

def test_the_csv_form_comes_from_money_not_a_second_implementation():
    """export.py did this arithmetic itself with the scale written out as a
    literal 100 -- knowledge money.py already held as MINOR_UNITS."""
    source = inspect.getsource(export)
    assert "divmod" not in source
    # Code only. export.py's docstring explains the literal it used to
    # contain, and grepping prose for the thing the prose describes is a test
    # that fails on its own documentation.
    code = re.sub(r'"""(?:.|\n)*?"""', "", source)
    code = re.sub(r"#[^\n]*", "", code)
    assert not re.search(r"[/%*,]\s*100\b", code), "the minor-unit scale is back"


def test_grouping_can_be_turned_off_for_the_file():
    assert money.format(123456, "EUR", symbol=False, grouping=True) == "1,234.56"
    assert money.format(123456, "EUR", symbol=False, grouping=False) == "1234.56"
    assert money.format(-123456, "EUR", symbol=False, grouping=False) == "-1234.56"
    assert money.format(None, "EUR", grouping=False) == ""


def test_the_scale_is_named_in_exactly_one_place():
    assert money.MINOR_UNITS == 2
    assert money._SCALE == 100
    for module in MODULES:
        if module is money:
            continue
        assert "MINOR_UNITS =" not in inspect.getsource(module)


# --------------------------------------------------------------- boundaries

def test_no_module_reaches_into_another_private_name():
    """importers called ledger._as_date. The underscore is a contract saying
    "this may change without notice", so depending on it across a module
    boundary is inappropriate intimacy."""
    names = [m.__name__ for m in MODULES]
    for module in MODULES:
        # Code only. A docstring that names another module's private function
        # in order to explain a decision is documentation, not a dependency --
        # and this test failed on exactly that: fxrates.newest says "see
        # budgets._converted_total", which is the cross-reference a reader
        # wants. Its sibling test already strips prose for the same reason.
        code = _code_of(module)
        for other in names:
            if other == module.__name__:
                continue
            found = re.findall(rf"\b{other}\._[a-z]", code)
            assert not found, f"{module.__name__} reaches into {other}: {found}"


def test_nothing_imports_inside_a_function():
    """Two functions in importers did `import ledger` locally, which looks
    like a circular-import workaround and was not one -- there is no cycle.
    A local import hides the dependency from anyone reading the imports."""
    for module in MODULES:
        for line in inspect.getsource(module).splitlines():
            if re.match(r"\s+(import|from)\s+[a-z_]", line):
                assert "typing" in line, \
                    f"{module.__name__} imports inside a function: {line.strip()}"


def test_the_module_layers_do_not_cycle():
    """money < paths < db/fxrates < ledger < importers/budgets < export.
    Asserting it here means a new import that inverts the order fails a test
    rather than an application startup."""
    layer = {money: 0, paths: 0, db: 1, fxrates: 1, ledger: 2,
             importers: 3, budgets: 3, export: 4}
    for module, rank in layer.items():
        source = inspect.getsource(module)
        for other, other_rank in layer.items():
            if other is module:
                continue
            if re.search(rf"^import {other.__name__}$", source, re.MULTILINE):
                assert other_rank < rank or (other_rank == rank
                                             and other in (money, paths)), \
                    f"{module.__name__} imports {other.__name__} upward"


# ---------------------------------------------------------- the route split

def test_the_app_factory_is_not_one_long_function():
    """create_app measured cyclomatic complexity 40 with every route inline --
    a Large Method that grew a branch per endpoint."""
    import app as web
    source = inspect.getsource(web.create_app)
    assert len(source.splitlines()) < 25
    groups = [name for name, _obj in inspect.getmembers(web, inspect.isfunction)
              if name.startswith("_") and "app" in
              inspect.signature(_obj_of(web, name)).parameters]
    assert len(groups) >= 4, f"routes are not grouped: {groups}"


def _obj_of(module, name):
    return getattr(module, name)


def test_the_checkbox_flag_is_read_one_way():
    """"on", true and "1" all arrive at these endpoints, and each call site
    was testing for them inline with slightly different wording."""
    import app as web
    assert web._flag("on") is True
    assert web._flag(True) is True
    assert web._flag("1") is True
    assert web._flag(None) is False
    assert web._flag("") is False
    assert web._flag("off") is False
