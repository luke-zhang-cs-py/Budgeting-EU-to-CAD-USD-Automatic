"""Rewrite the measured figures in docs/index.html.

`tests/test_published_figures.py` checks that the numbers the published page
quotes are the real ones. This is the other half of that: it measures and
writes them, so keeping the page true is one command rather than a hunt
through a 700-line file.

    python tools/refresh_figures.py

It runs the suite under coverage, because `missed` cannot be known without a
real run, then rewrites the module and test blocks. It prints what it
changed; a run that prints only the totals means the page was already right.

Two things it will not do on its own:

  * invent prose. A new test file, or a new module, needs a row saying what
    it covers — only a person knows that. It stops and says which.
  * leave a whitespace diff. It re-pads both blocks to a column computed
    from the longest name and checks its own output for stability before
    writing, because a tool that reformats the file on every run is one
    nobody runs.
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "docs", "index.html")
README = os.path.join(ROOT, "README.md")


def read(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def measure():
    """Run the suite under coverage and read the JSON report.

    The report rather than a static analysis: it is the same set of files the
    terminal report prints, so the omitted build tools stay omitted and the
    network/ package is not missed — both of which a walk of the root got
    wrong.
    """
    handle, path = tempfile.mkstemp(suffix=".json")
    os.close(handle)
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--cov",
         "--cov-report=json:" + path, "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=1800)
    report = json.loads(read(path))
    os.remove(path)

    out = {}
    for name, body in report["files"].items():
        clean = name.replace("\\", "/")
        out[clean] = {
            "lines": len(read(os.path.join(ROOT, name)).splitlines()),
            "stmts": body["summary"]["num_statements"],
            "missed": body["summary"]["missing_lines"],
        }
    return out


def collect():
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=900)
    counts = {}
    for line in done.stdout.splitlines():
        if "::" in line:
            name = os.path.basename(line.split("::")[0])
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        raise SystemExit("collection produced nothing:\n" + done.stdout[-800:])
    return counts


def fix_modules(page, measured, changes):
    block = re.search(r"var MODULES = \[\n(.*?)\n\];", page, re.S)
    rows = re.findall(
        r"\{ name: '([\w./]+)',\s*lines:\s*(\d+),\s*stmts:\s*(\d+),"
        r"\s*missed:\s*(\d+), does: '(.*?)' \}", block.group(1))
    does = {name: text for name, _, _, _, text in rows}
    was = {name: (int(a), int(b), int(c)) for name, a, b, c, _ in rows}

    missing = sorted(set(measured) - set(does))
    if missing:
        raise SystemExit(
            "no description on the page for %s.\nAdd a row for it in the "
            "MODULES block first, saying what it does; this script will fix "
            "the figures." % ", ".join(missing))
    for name in sorted(set(does) - set(measured)):
        changes.append("%s is on the page but coverage no longer reports it "
                       "-- remove it, or check whether it became omitted"
                       % name)

    order = sorted(measured.items(), key=lambda kv: -kv[1]["stmts"])
    column = max(len(name) for name in measured) + 3
    lines = []
    for index, (name, body) in enumerate(order):
        now = (body["lines"], body["stmts"], body["missed"])
        if was.get(name) != now:
            changes.append("%-24s %s -> %s" % (name, was.get(name, "new"), now))
        lines.append(
            "  { name: %s lines: %s, stmts: %s, missed: %s, does: '%s' }%s"
            % (("'%s'," % name).ljust(column), str(body["lines"]).rjust(3),
               str(body["stmts"]).rjust(3), str(body["missed"]).rjust(2),
               does[name], "," if index < len(order) - 1 else ""))
    return page[:block.start(1)] + "\n".join(lines) + page[block.end(1):]


def fix_tests(page, counts, changes):
    block = re.search(r"var TESTS = \[\n(.*?)\n\];", page, re.S)
    rows = re.findall(r"\{ file: '([\w.]+)',\s*n:\s*(\d+), of: '(.*?)' \}",
                      block.group(1))
    of = {name: text for name, _, text in rows}
    was = {name: int(n) for name, n, _ in rows}

    missing = sorted(set(counts) - set(of))
    if missing:
        raise SystemExit(
            "no description on the page for %s.\nAdd a row for it in the "
            "TESTS block first, saying what it covers; this script will fix "
            "the count." % ", ".join(missing))
    for name in sorted(set(of) - set(counts)):
        changes.append("%s is on the page but collects nothing" % name)

    order = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    column = max(len(name) for name in counts) + 3
    lines = []
    for index, (name, count) in enumerate(order):
        if was.get(name) != count:
            changes.append("%-28s %s -> %s" % (name, was.get(name, "new"),
                                               count))
        lines.append("  { file: %s n: %s, of: '%s' }%s"
                     % (("'%s'," % name).ljust(column), str(count).rjust(2),
                        of[name], "," if index < len(order) - 1 else ""))
    return page[:block.start(1)] + "\n".join(lines) + page[block.end(1):]


def fix_measured_with(page, changes):
    """Record the interpreter, because a statement count is a property of a
    file *and* an interpreter -- 3.14 and 3.12 disagree about several of the
    modules in this family."""
    version = "%d.%d" % sys.version_info[:2]
    found = re.search(r"var MEASURED_WITH = 'Python ([\d.]+)';", page)
    if not found:
        raise SystemExit(
            "the page has no MEASURED_WITH line, so nothing records which "
            "Python produced its figures. Add one to the data block.")
    if found.group(1) != version:
        changes.append("MEASURED_WITH Python %s -> %s"
                       % (found.group(1), version))
    return re.sub(r"var MEASURED_WITH = 'Python [\d.]+';",
                  "var MEASURED_WITH = 'Python %s';" % version, page)


def fix_test_lines(page, changes):
    total = 0
    for here, dirs, names in os.walk(os.path.join(ROOT, "tests")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in names:
            if name.endswith(".py"):
                total += len(read(os.path.join(here, name)).splitlines())
    found = re.search(r"var TEST_LINES = (\d+);", page)
    if found and int(found.group(1)) != total:
        changes.append("TEST_LINES %s -> %s" % (found.group(1), total))
    return re.sub(r"var TEST_LINES = \d+;",
                  "var TEST_LINES = %d;" % total, page)


def fix_readme(measured, counts, changes):
    """The one sentence of figures in the README, if it has one."""
    if not os.path.exists(README):
        return
    readme = read(README)
    stmts = sum(body["stmts"] for body in measured.values())
    missed = sum(body["missed"] for body in measured.values())
    percent = round(100 * (stmts - missed) / stmts)
    wanted = "%s tests, %d%% of %s statements" % (
        format(sum(counts.values()), ","), percent, format(stmts, ","))
    found = re.search(r"[\d,]+ tests, \d+% of [\d,]+ statements", readme)
    if not found:
        return                     # this README does not state them that way
    if found.group(0) != wanted:
        changes.append("README  %s -> %s" % (found.group(0), wanted))
        write(README, readme.replace(found.group(0), wanted))


def rewrite(page, measured, counts, changes):
    return fix_measured_with(fix_test_lines(
        fix_tests(fix_modules(page, measured, changes), counts, changes),
        changes), changes)


def main():
    measured, counts, changes = measure(), collect(), []
    page = read(PAGE)
    fixed = rewrite(page, measured, counts, changes)

    again = rewrite(fixed, measured, counts, [])
    if again != fixed:
        raise SystemExit(
            "this script is not idempotent -- a second pass over its own "
            "output changed it again, so it would leave a diff behind on "
            "every run. Fix that before trusting what it wrote.")

    if fixed != page:
        write(PAGE, fixed)
    fix_readme(measured, counts, changes)

    for line in changes:
        print("  " + line)
    stmts = sum(body["stmts"] for body in measured.values())
    missed = sum(body["missed"] for body in measured.values())
    print("  %d tests, %d statements, %d%% covered"
          % (sum(counts.values()), stmts,
             round(100 * (stmts - missed) / stmts)))


if __name__ == "__main__":
    main()
