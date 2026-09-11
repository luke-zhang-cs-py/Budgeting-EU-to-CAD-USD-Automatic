"""Rewrite the measured figures in docs/index.html and README.md.

`tests/test_published_figures.py` checks that the numbers those two files
quote are the real ones. This is the other half of that: it measures and
writes them, so keeping them true is one command rather than a hunt through
a 900-line page.

    python tools/refresh_figures.py

Run it after adding or removing tests, or after a module changes size. It
prints what it changed and nothing else, so a run that prints nothing means
the published figures were already right.

The numbers come from the same places the guard reads them: `coverage`'s own
analysis with this project's .coveragerc for the statement counts, `wc -l`
for file length, and a `--collect-only` run for the test counts. Nothing here
estimates anything.
"""
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "docs", "index.html")
README = os.path.join(ROOT, "README.md")

# A description for a test file that is not in the page's table yet. Without
# one the script stops rather than inventing prose for it, because the column
# says what the file covers and only a person knows that.
UNKNOWN = None


def read(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def measure_modules():
    import coverage
    config = coverage.Coverage(config_file=os.path.join(ROOT, ".coveragerc"))
    config.load()
    out = {}
    for name in sorted(os.listdir(ROOT)):
        if not name.endswith(".py"):
            continue
        _, statements, _, _, _ = config.analysis2(os.path.join(ROOT, name))
        out[name] = (len(read(os.path.join(ROOT, name)).splitlines()),
                     len(statements))
    return out


def collect_tests():
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=600)
    counts = {}
    for line in done.stdout.splitlines():
        if "::" in line:
            name = os.path.basename(line.split("::")[0])
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        raise SystemExit("collection produced nothing:\n" + done.stdout[-800:])
    return counts


def fix_modules(page, measured, changes):
    def one(match):
        name = match.group("name")
        if name not in measured:
            changes.append("%s is on the page but no longer in the project"
                           % name)
            return match.group(0)
        lines, stmts = measured[name]
        was = (int(match.group("lines")), int(match.group("stmts")))
        if was != (lines, stmts):
            changes.append("%-14s %s -> %s" % (name, was, (lines, stmts)))
        return ("{ name: '%s',%s lines: %s, stmts: %s, missed: 0,"
                % (name, match.group("pad"), str(lines).rjust(3),
                   str(stmts).rjust(3)))

    page = re.sub(
        r"\{ name: '(?P<name>[\w.]+)',(?P<pad>\s*)lines:\s*(?P<lines>\d+),"
        r"\s*stmts:\s*(?P<stmts>\d+), missed: 0,", one, page)

    listed = set(re.findall(r"\{ name: '([\w.]+)',", page))
    for name in sorted(set(measured) - listed):
        changes.append("%s is a module the page does not list -- add a row "
                       "for it by hand, with what it does" % name)
    return page


def fix_tests(page, counts, changes):
    block = re.search(r"var TESTS = \[\n(.*?)\n\];", page, re.S)
    # `n:\s*` rather than `n: ` -- the counts are right-justified, so a
    # single-digit one carries an extra space and a stricter pattern silently
    # read those rows as missing.
    prose = dict(re.findall(r"\{ file: '([\w.]+)',\s*n:\s*\d+, of: '(.*?)' \}",
                            block.group(1)))
    was = dict((name, int(n)) for name, n in re.findall(
        r"\{ file: '([\w.]+)',\s*n:\s*(\d+),", block.group(1)))

    missing = sorted(set(counts) - set(prose))
    if missing:
        raise SystemExit(
            "no description on the page for %s.\nAdd a row for it in the "
            "TESTS block first, saying what it covers; this script will fix "
            "the count." % ", ".join(missing))

    for name in sorted(set(prose) - set(counts)):
        changes.append("%s is on the page but collects nothing -- dropping it"
                       % name)
    for name, count in sorted(counts.items()):
        if was.get(name) != count:
            changes.append("%-26s %s -> %s" % (name, was.get(name, "new"),
                                               count))

    rows = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    width = max(len(name) for name in counts) + 3
    lines = ["  { file: %s n: %s, of: '%s' }%s"
             % (("'%s'," % name).ljust(width), str(n).rjust(2), prose[name],
                "," if index < len(rows) - 1 else "")
             for index, (name, n) in enumerate(rows)]
    return page[:block.start(1)] + "\n".join(lines) + page[block.end(1):]


def fix_route_count(page, changes):
    source = read(os.path.join(ROOT, "app.py"))
    routes = source.count("@app.route")
    groups = len(re.findall(r"^def _\w+\(app, ctx\):", source, re.MULTILINE))
    for name, value in (("ROUTE_COUNT", routes), ("ROUTE_GROUPS", groups)):
        found = re.search(r"var %s = (\d+);" % name, page)
        if int(found.group(1)) != value:
            changes.append("%s %s -> %s" % (name, found.group(1), value))
        page = re.sub(r"var %s = \d+;" % name, "var %s = %d;" % (name, value),
                      page)
    return page


def fix_readme(counts, measured, changes):
    readme = read(README)
    tests = sum(counts.values())
    statements = sum(stmts for _, stmts in measured.values())
    wanted = "%s tests, 100%% of %s statements" % (f"{tests:,}",
                                                   f"{statements:,}")
    found = re.search(r"[\d,]+ tests, 100% of [\d,]+ statements", readme)
    if not found:
        raise SystemExit("the README no longer states its counts in the form "
                         "this script writes")
    if found.group(0) != wanted:
        changes.append("README  %s -> %s" % (found.group(0), wanted))
        write(README, readme.replace(found.group(0), wanted))


def main():
    measured, counts, changes = measure_modules(), collect_tests(), []
    page = read(PAGE)
    fixed = fix_route_count(fix_tests(fix_modules(page, measured, changes),
                                      counts, changes), changes)
    if fixed != page:
        write(PAGE, fixed)
    fix_readme(counts, measured, changes)

    for line in changes:
        print("  " + line)
    print("  %d tests, %d statements"
          % (sum(counts.values()),
             sum(stmts for _, stmts in measured.values())))


if __name__ == "__main__":
    main()
