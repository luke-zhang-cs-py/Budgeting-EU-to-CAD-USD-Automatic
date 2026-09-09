"""
app.py
------
The web front end. Runs on 127.0.0.1:5004.

Loopback and debug-off by default, deliberately. The Almanac app in this
family bound 0.0.0.0 with DEBUG on, which put the Werkzeug debugger -- an
interactive Python console -- on every interface of the machine. This holds
personal financial history, so the default has to be the safe one and going
wider has to be a choice somebody makes on purpose.

The routes are registered in groups rather than written out inline. As one
function, create_app measured cyclomatic complexity 40 -- a Large Method by
any reading, and the kind that grows another branch every time an endpoint is
added. Each group now takes the same small context object, so adding a route
means touching one function of a dozen lines.
"""
import datetime as dt
import os

from flask import Flask, Response, jsonify, render_template, request

import budgets
import db
import export
import fxrates
import importers
import ledger
import money

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5004"))
DEBUG = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")

# An upload larger than this is not a bank statement.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


class Context:
    """What every route group needs: where the data is, and how to reach it.

    One object rather than three parameters threaded through five registration
    functions. Those parameters were all strings and callables describing one
    thing -- the request's view of the data directory -- which is the shape
    primitive obsession takes in a web app.
    """

    def __init__(self, directory=None):
        self.directory = directory

    def connect(self):
        """One connection per request, closed when the block ends.

        sqlite3 objects are not shareable across threads and Flask serves each
        request on its own, so a connection per request is right. This used to
        hand back db.connect() directly, and `with` on a sqlite3 connection
        scopes a transaction rather than closing it -- so every request leaked
        one. db.session closes.
        """
        return db.session(self.directory)

    def month(self):
        return request.args.get("month") or dt.date.today().strftime("%Y-%m")


def create_app(directory=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.config["WALLET_DIR"] = directory

    context = Context(directory)
    for register in (_pages, _reading, _writing, _importing, _settings,
                     _exporting):
        register(app, context)
    return app


# ----------------------------------------------------------------- routes

def _pages(app, ctx):
    @app.route("/")
    def index():
        return render_template("index.html")


def _reading(app, ctx):
    @app.route("/api/overview")
    def overview():
        month = ctx.month()
        with ctx.connect() as conn:
            return jsonify({
                "month": month,
                "months": ledger.months(conn),
                "categories": ledger.categories(conn),
                "summary": budgets.summary(conn, month,
                                           directory=ctx.directory),
                "budgets": budgets.status(conn, month),
                "trend": [{"month": m, "spent_eur": -t,
                           "spent_text": money.format(-t, "EUR")}
                          for m, t in ledger.monthly_totals(conn)],
                "recurring": ledger.recurring(conn),
                "rates": fxrates.coverage(ctx.directory),
            })

    @app.route("/api/transactions")
    def transactions():
        with ctx.connect() as conn:
            return jsonify({
                "transactions": ledger.transactions(
                    conn,
                    month=request.args.get("month") or None,
                    category=request.args.get("category") or None,
                    search=request.args.get("q") or None,
                    directory=ctx.directory,
                    limit=request.args.get("limit", 500)),
            })

    @app.route("/api/reconciles")
    def reconciles():
        """Do the exported rows add up to the ledger? Shown in the footer."""
        with ctx.connect() as conn:
            agrees, from_file, from_ledger = export.reconciles(
                conn, month=ctx.month(), directory=ctx.directory)
        return jsonify({"agrees": agrees, "file_eur": from_file,
                        "ledger_eur": from_ledger,
                        "file_text": money.format(from_file, "EUR")})


def _writing(app, ctx):
    @app.route("/api/transaction", methods=["POST"])
    def add_transaction():
        """Manual entry -- the route in for cash, and for anything a wallet
        app will not export."""
        body = request.get_json(silent=True) or request.form
        try:
            amount = _signed_amount(body)
        except money.MoneyError as bad:
            return jsonify({"error": str(bad)}), 400

        with ctx.connect() as conn:
            try:
                tid, how = ledger.add(
                    conn, body.get("date") or dt.date.today().isoformat(),
                    body.get("description"), amount,
                    category=body.get("category") or None,
                    source="manual", force=_flag(body.get("force")))
            except (ValueError, money.MoneyError) as bad:
                return jsonify({"error": str(bad)}), 400
        return jsonify({"id": tid, "result": how}), (
            201 if how == "added" else 200)

    @app.route("/api/transaction/<int:tid>", methods=["DELETE"])
    def delete_transaction(tid):
        with ctx.connect() as conn:
            ledger.remove(conn, tid)
        return jsonify({"deleted": tid})

    @app.route("/api/transaction/<int:tid>/category", methods=["POST"])
    def set_category(tid):
        body = request.get_json(silent=True) or request.form
        category = (body.get("category") or "").strip()
        if not category:
            return jsonify({"error": "no category given"}), 400
        with ctx.connect() as conn:
            ledger.recategorise(conn, tid, category)
        return jsonify({"id": tid, "category": category})


def _importing(app, ctx):
    @app.route("/api/import/preview", methods=["POST"])
    def import_preview():
        """What the file would do, before it does it."""
        previewed, sniffed, failure = _read_upload(request)
        if failure:
            return failure
        return jsonify({
            "headers": sniffed["headers"],
            "delimiter": sniffed["delimiter"],
            "mapping": previewed["mapping"],
            "readable": previewed["readable"],
            "unreadable": previewed["unreadable"],
            "problems": previewed["problems"][:20],
            "shown": previewed["shown"],
            "spending_total": previewed["spending_total"],
            "spending_total_text": money.format(previewed["spending_total"],
                                                "EUR"),
        })

    @app.route("/api/import/commit", methods=["POST"])
    def import_commit():
        previewed, _sniffed, failure = _read_upload(request)
        if failure:
            return failure
        name = getattr(request.files.get("file"), "filename", "") or "paste"
        with ctx.connect() as conn:
            out = importers.load(
                conn, previewed, source=f"import:{name}",
                use_file_categories=_flag(
                    request.form.get("use_file_categories")))
            out["recategorised"] = ledger.apply_rules(conn)
        return jsonify(out)


def _settings(app, ctx):
    @app.route("/api/budget", methods=["POST"])
    def set_budget():
        body = request.get_json(silent=True) or request.form
        category = (body.get("category") or "").strip()
        if not category:
            return jsonify({"error": "no category given"}), 400
        try:
            cap = money.parse(body.get("cap") or "0")
        except money.MoneyError as bad:
            return jsonify({"error": str(bad)}), 400
        with ctx.connect() as conn:
            try:
                budgets.set_cap(conn, category, cap)
            except ValueError as bad:
                return jsonify({"error": str(bad)}), 400
            return jsonify({"budgets": budgets.status(conn, ctx.month())})

    @app.route("/api/rules", methods=["GET", "POST"])
    def manage_rules():
        with ctx.connect() as conn:
            if request.method == "GET":
                return jsonify({"rules": ledger.rules(conn)})
            body = request.get_json(silent=True) or request.form
            try:
                ledger.add_rule(conn, body.get("keyword"),
                                (body.get("category") or "").strip()
                                or db.UNCATEGORISED)
            except ValueError as bad:
                return jsonify({"error": str(bad)}), 400
            return jsonify({"rules": ledger.rules(conn),
                            "recategorised": ledger.apply_rules(conn)}), 201

    @app.route("/api/rules/<int:rule_id>", methods=["DELETE"])
    def delete_rule(rule_id):
        with ctx.connect() as conn:
            ledger.remove_rule(conn, rule_id)
            return jsonify({"rules": ledger.rules(conn)})

    @app.route("/api/rates/refresh", methods=["POST"])
    def refresh_rates():
        """Returns 200 with ok=false on a network failure rather than 5xx.
        A failed refresh is a normal outcome offline, not a server error."""
        ok = fxrates.refresh(ctx.directory)
        return jsonify({"ok": ok, "rates": fxrates.coverage(ctx.directory)})


def _exporting(app, ctx):
    @app.route("/export/transactions.csv")
    def export_transactions():
        month = request.args.get("month") or None
        with ctx.connect() as conn:
            text = export.transactions_csv(
                conn, month=month,
                category=request.args.get("category") or None,
                search=request.args.get("q") or None,
                directory=ctx.directory)
        return _csv(text, export.filename("transactions", month))

    @app.route("/export/summary.csv")
    def export_summary():
        month = ctx.month()
        with ctx.connect() as conn:
            text = export.summary_csv(conn, month)
        return _csv(text, export.filename("summary", month))


# ----------------------------------------------------------------- helpers

def _flag(value):
    """A checkbox or JSON boolean as a bool.

    Three spellings arrive at these endpoints -- "on" from an HTML checkbox,
    true from JSON, "1" from a query string -- and each was being tested for
    inline at its own call site with slightly different wording.
    """
    return str(value).lower() in ("1", "true", "yes", "on")


def _signed_amount(body):
    """The amount, signed by what kind of entry this is.

    Typed as a positive number and taken as spending unless marked as income.
    Requiring a minus sign for every coffee is the friction that stops people
    using a tracker at all.
    """
    amount = money.parse(body.get("amount"))
    if amount == 0:
        raise money.MoneyError("an amount of zero is not a purchase")
    return abs(amount) if body.get("kind") == "income" else -abs(amount)


def _uploaded(req):
    """(text, problem). Accepts a file or pasted text."""
    handle = req.files.get("file")
    if handle and handle.filename:
        raw = handle.read()
        if not raw:
            return None, "the file is empty"
        return raw.decode("utf-8-sig", errors="replace"), None
    pasted = (req.form.get("text") or "").strip()
    if pasted:
        return pasted, None
    return None, "no file or text given"


def _read_upload(req):
    """(previewed, sniffed, failure_response).

    Preview and commit both need the file read, sniffed, mapped and previewed,
    and both need the same three failure paths. Written out twice, the two
    drifted apart in which errors they reported.
    """
    text, problem = _uploaded(req)
    if problem:
        return None, None, (jsonify({"error": problem}), 400)
    try:
        sniffed = importers.sniff(text)
        previewed = importers.preview(sniffed, _mapping_from(req, sniffed))
    except importers.ImportProblem as bad:
        return None, None, (jsonify({"error": str(bad)}), 400)
    return previewed, sniffed, None


def _mapping_from(req, sniffed):
    """The mapping the browser chose, falling back to the guess per field."""
    guessed = sniffed["mapping"]
    chosen = {}
    for field in ("date", "description", "amount", "amount_out", "amount_in",
                  "currency", "category"):
        sent = req.form.get(field)
        chosen[field] = (sent or None) if sent is not None \
            else guessed.get(field)
    chosen["expenses_positive"] = (
        _flag(req.form.get("expenses_positive"))
        if "expenses_positive" in req.form
        else guessed.get("expenses_positive", False))
    return chosen


def _csv(text, name):
    return Response(text, mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{name}"'})


app = create_app()


if __name__ == "__main__":
    # Populate the rate cache on first run so the page is useful immediately.
    if not fxrates.available():
        print("fetching ECB rate history (one time, ~640 KB) ...")
        print("  ok" if fxrates.refresh() else
              "  failed -- the app still runs; use Refresh rates later")
    print(f"Wallet FX & Budget on http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=DEBUG)
