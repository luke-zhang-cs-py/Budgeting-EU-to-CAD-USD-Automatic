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
import cards
import db
import export
import fxcost
import fxlive
import fxrates
import goals
import importers
import ledger
import money
import ocr
import receipts
import sources
import trends
import upcoming

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
    for register in (_pages, _reading, _writing, _importing,
                     _budgets, _rules_and_rates, _watching, _exporting,
                     _receipts, _receipt_files, _live, _cards,
                     _card_settings, _card_insight, _planning,
                     _goals_and_subscriptions):
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
                # What the card's own conversion cost, over the rows that
                # were billed in something other than euros.
                "fx": fxcost.summarise(ledger.transactions(
                    conn, month=month, directory=ctx.directory)),
                "inbox": sources.status(ctx.directory),
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


def _budgets(app, ctx):
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


def _rules_and_rates(app, ctx):
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


def _watching(app, ctx):
    """The watched folder. Its own group rather than another branch of
    _settings, which had become a grab-bag of budgets, rules, rates and
    sources and measured cyclomatic complexity 15 for it."""
    @app.route("/api/sources", methods=["GET"])
    def sources_status():
        """Where the watched folder is, and what the last sweep did."""
        with ctx.connect() as conn:
            return jsonify({"inbox": sources.status(ctx.directory),
                            "history": sources.history(conn)})

    @app.route("/api/sources/scan", methods=["POST"])
    def sources_scan():
        """Sweep the folder now, rather than waiting for the timer."""
        with ctx.connect() as conn:
            results = sources.scan(
                conn, ctx.directory,
                use_file_categories=_flag(
                    request.form.get("use_file_categories")))
            return jsonify({"results": results,
                            "inbox": sources.status(ctx.directory),
                            "history": sources.history(conn)})


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


def _receipts(app, ctx):
    @app.route("/api/receipt/read", methods=["POST"])
    def read_receipt():
        """Read a screenshot and say what it thinks it found.

        Reads and answers; it does not save. The confirm step is a separate
        route on purpose -- OCR gets amounts wrong, and a tracker that
        silently records EUR 5230 because the decimal point did not survive
        is worse than one that cannot read screenshots at all. Everything
        uncertain comes back in `needs` for a person to settle.
        """
        payload = _uploaded_image(request)
        if isinstance(payload, tuple):
            return payload

        if not ocr.available():
            # The upload is still kept, so the screenshot is attached to the
            # purchase you are about to type by hand.
            return jsonify({
                "ocr": False,
                "stored": receipts.store(payload, ctx.directory),
                "reading": None,
                "why": "no OCR engine installed; "
                       "pip install -r requirements-ocr.txt",
            })

        try:
            boxes = ocr.read(payload)
        except ocr.OcrError as bad:
            return jsonify({"error": str(bad)}), 400

        with ctx.connect() as conn:
            return jsonify({
                "ocr": True,
                "stored": receipts.store(payload, ctx.directory),
                "reading": _reading_with_context(conn, boxes),
            })

    @app.route("/api/receipt/save", methods=["POST"])
    def save_receipt():
        """Record the purchase a person has confirmed off a screenshot.

        Takes the settled figures, never the raw reading, so nothing the OCR
        was unsure about can reach the ledger without having been looked at.
        """
        body = request.get_json(silent=True) or request.form
        try:
            amount = _signed_amount(body)
        except money.MoneyError as bad:
            return jsonify({"error": str(bad)}), 400

        stored = body.get("stored") or None
        if stored and not receipts.stored_path(stored, ctx.directory):
            return jsonify({"error": "unknown receipt"}), 400

        with ctx.connect() as conn:
            return _record_confirmed(conn, body, amount, stored)


def _receipt_files(app, ctx):
    @app.route("/receipt/<name>")
    def show_receipt(name):
        """Serve a stored screenshot.

        The path comes from receipts.stored_path, which only answers for names
        this app itself wrote -- a hex digest and an extension. Anything else
        is a 404 rather than something sanitised into a nearby file.
        """
        where = receipts.stored_path(name, ctx.directory)
        if not where:
            return jsonify({"error": "no such receipt"}), 404
        with open(where, "rb") as handle:
            body = handle.read()
        kind = receipts.kind_of(body) or ".png"
        return Response(body, mimetype=f"image/{kind.lstrip('.')}")


def _live(app, ctx):
    @app.route("/api/rate/live")
    def live_rate():
        """The latest published EUR rates, fetched now.

        Labelled for what it is. `daily_reference` says the figure is the
        ECB's once-a-day fixing rather than an intraday tick, because there
        is no free source for the latter and a card would not settle at it
        anyway -- Visa converts on the day it processes the purchase.
        """
        got = fxlive.quote()
        if not got:
            return jsonify({"available": False,
                            "why": "no rate source answered just now"}), 200
        return jsonify(dict(got, available=True,
                            rates={k: str(v) for k, v in
                                   got["rates"].items()}))

    @app.route("/api/rate/estimate", methods=["POST"])
    def estimate_cost():
        """What a euro amount would bill on a given card, right now."""
        body = request.get_json(silent=True) or request.form
        try:
            base = money.parse(body.get("amount"))
        except money.MoneyError as bad:
            return jsonify({"error": str(bad)}), 400
        if not base:
            return jsonify({"error": "no amount given"}), 400

        got = fxlive.quote()
        if not got:
            return jsonify({"error": "no live rate available"}), 503

        with ctx.connect() as conn:
            card = (cards.get(conn, _int_or_none(body.get("card_id")))
                    if body.get("card_id") else None)
        fee_bp = card["fee_bp"] if card else fxcost.TYPICAL_CARD_FEE_BP
        target = (card["currency"] if card and card["currency"] != money.BASE
                  else (body.get("currency") or "CAD").upper())

        rate = got["rates"].get(target)
        if rate is None:
            return jsonify({"error": f"no live rate for {target}"}), 400

        out = fxcost.estimate(abs(base), rate, fee_bp)
        return jsonify(dict(out, **{
            "currency": target,
            "card": card["label"] if card else None,
            "base_text": money.format(abs(base), money.BASE),
            "converted_text": money.format(out["converted_minor"], target),
            "fee_text": money.format(out["fee_minor"], target),
            "total_text": money.format(out["total_minor"], target),
            "fee_percent": fee_bp / fxcost.BASIS_POINTS * 100,
            "fee_percent_text": f"{fee_bp / fxcost.BASIS_POINTS * 100:.2f}%",
            "published": got["published"],
            "daily_reference": got["daily_reference"],
        }))


def _cards(app, ctx):
    @app.route("/api/cards")
    def list_cards():
        with ctx.connect() as conn:
            return jsonify({
                "cards": cards.balances(conn, ctx.month(), ctx.directory),
                "currencies": list(money.CURRENCIES),
                "default_fee_bp": cards.DEFAULT_FEE_BP,
                "default_fee_text":
                    f"{cards.DEFAULT_FEE_BP / fxcost.BASIS_POINTS * 100:.2f}%",
            })

    @app.route("/api/cards", methods=["POST"])
    def add_card():
        body = request.get_json(silent=True) or request.form
        with ctx.connect() as conn:
            try:
                cid = cards.add(conn, body.get("name"), body.get("currency"),
                                mask=body.get("mask") or "",
                                fee_bp=_int_or_none(body.get("fee_bp")),
                                opening_minor=_int_or_none(
                                    body.get("opening_minor")) or 0)
            except cards.CardError as bad:
                return jsonify({"error": str(bad)}), 400
        return jsonify({"id": cid}), 201


def _card_settings(app, ctx):
    @app.route("/api/cards/<int:cid>", methods=["POST"])
    def change_card(cid):
        body = request.get_json(silent=True) or request.form
        with ctx.connect() as conn:
            try:
                cards.update(conn, cid, name=body.get("name"),
                             mask=body.get("mask"),
                             fee_bp=_int_or_none(body.get("fee_bp")),
                             opening_minor=_int_or_none(
                                 body.get("opening_minor")))
            except cards.CardError as bad:
                return jsonify({"error": str(bad)}), 400
        return jsonify({"id": cid})

    @app.route("/api/cards/<int:cid>", methods=["DELETE"])
    def drop_card(cid):
        with ctx.connect() as conn:
            try:
                cards.remove(conn, cid)
            except cards.CardError as bad:
                return jsonify({"error": str(bad)}), 404
        return jsonify({"deleted": cid})


def _card_insight(app, ctx):
    @app.route("/api/cards/<int:cid>/measured")
    def card_measured(cid):
        """The card's real foreign markup, from its own statements."""
        with ctx.connect() as conn:
            try:
                found = cards.measured_fee(conn, cid, ctx.directory)
            except cards.CardError as bad:
                return jsonify({"error": str(bad)}), 404
        if not found:
            return jsonify({
                "available": False,
                "why": f"fewer than {cards.ENOUGH_TO_MEASURE} purchases on "
                       f"this card have a billed figure to compare against"})
        return jsonify(dict(
            found, available=True,
            percent_text=f"{found['percent']:.2f}%",
            published_text=(
                f"{found['published_bp'] / fxcost.BASIS_POINTS * 100:.2f}%")))

    @app.route("/api/transaction/<int:tid>/card", methods=["POST"])
    def attribute_card(tid):
        body = request.get_json(silent=True) or request.form
        with ctx.connect() as conn:
            try:
                cards.attribute(conn, tid, _int_or_none(body.get("card_id")))
            except cards.CardError as bad:
                return jsonify({"error": str(bad)}), 400
        return jsonify({"id": tid})


def _planning(app, ctx):
    @app.route("/api/trends")
    def trend_report():
        with ctx.connect() as conn:
            return jsonify({
                "months": trends.by_month(conn),
                "movers": trends.movers(conn, ctx.month()),
            })

    @app.route("/api/trends/<category>")
    def category_report(category):
        with ctx.connect() as conn:
            found = trends.drilldown(conn, category, ctx.month())
            return jsonify({
                "category": category,
                "month": found["month"],
                "count": found["count"],
                "spent_text": found["spent_text"],
                "rows": found["rows"],
                "history": trends.category(conn, category),
            })


def _goals_and_subscriptions(app, ctx):
    @app.route("/api/subscriptions")
    def subscription_report():
        with ctx.connect() as conn:
            return jsonify({
                "rows": upcoming.status(conn, ctx.month()),
                "outstanding": upcoming.outstanding(conn, ctx.month()),
                "cost": upcoming.monthly_cost(conn),
                "rises": upcoming.rises(conn),
            })

    @app.route("/api/goals")
    def list_goals():
        with ctx.connect() as conn:
            return jsonify({
                "goals": goals.progress(conn, ctx.month()),
                "contributed": goals.contributed(conn, ctx.month()),
            })

    @app.route("/api/goals", methods=["POST"])
    def add_goal():
        body = request.get_json(silent=True) or request.form
        try:
            target = money.parse(body.get("target"))
        except money.MoneyError as bad:
            return jsonify({"error": str(bad)}), 400
        with ctx.connect() as conn:
            try:
                gid = goals.add(conn, body.get("name"), abs(target),
                                currency=body.get("currency") or money.BASE,
                                due_on=body.get("due_on") or None)
            except goals.GoalError as bad:
                return jsonify({"error": str(bad)}), 400
        return jsonify({"id": gid}), 201

    @app.route("/api/goals/<int:gid>", methods=["DELETE"])
    def drop_goal(gid):
        with ctx.connect() as conn:
            if not goals.remove(conn, gid):
                return jsonify({"error": "no such goal"}), 404
        return jsonify({"deleted": gid})


def _reading_with_context(conn, boxes):
    """The parsed screenshot, plus what the ledger knows that it cannot.

    receipts.parse is pure and has no database, which is what makes it
    testable without an engine. Matching the card and suggesting a category
    need one, so they are added here rather than by giving the parser a
    connection it would only use for this.
    """
    reading = receipts.parse(boxes)
    reading["card"] = cards.by_mask(conn, reading["card_mask"])
    reading["categories"] = ledger.categories(conn)
    reading["suggested_category"] = (
        ledger.categorise(conn, reading["merchant"] or "")
        if reading["merchant"] else None)
    return reading


def _record_confirmed(conn, body, amount, stored):
    """Insert a confirmed screenshot purchase and attach what belongs to it.

    Split out of the route because the route was measuring complexity 19 with
    this inline -- the same Large Method that made create_app unreadable
    before the route groups. The branching is all here: what the ledger says
    happened, whether there is a screenshot to attach, and whether a card was
    named.
    """
    try:
        tid, how = ledger.add(
            conn, body.get("date") or dt.date.today().isoformat(),
            body.get("description"), amount,
            category=body.get("category") or None,
            source="receipt",
            force=_flag(body.get("force")),
            charged_minor=_charged_minor(body),
            charged_currency=body.get("charged_currency") or None)
    except (ValueError, money.MoneyError) as bad:
        return jsonify({"error": str(bad)}), 400

    # Only a new row gets the attachments. A duplicate already has whatever
    # the first save gave it, and overwriting them would let a re-send
    # silently move a purchase onto a different card.
    if how == "added":
        if stored:
            conn.execute("UPDATE transactions SET receipt = ? WHERE id = ?",
                         (stored, tid))
            conn.commit()
        card_id = _int_or_none(body.get("card_id"))
        if card_id:
            try:
                cards.attribute(conn, tid, card_id)
            except cards.CardError as bad:
                return jsonify({"error": str(bad)}), 400

    return jsonify({"id": tid, "result": how}), (
        201 if how == "added" else 200)


def _charged_minor(body):
    """What the card billed, as cents, from either field name.

    Accepts `charged` as written text and reads it with money.parse, which
    already handles both "85.94" and "85,94" -- so the browser never does
    money arithmetic, and there is no second parser in JavaScript to drift
    from this one. `charged_minor` stays accepted for a caller that already
    holds cents.
    """
    written = body.get("charged")
    if written and str(written).strip():
        try:
            return abs(money.parse(str(written)))
        except money.MoneyError:
            return None
    return _int_or_none(body.get("charged_minor"))


def _int_or_none(value):
    """An int from a form field, or None. Never raises.

    Forms send "" for an untouched number input, and int("") is a
    ValueError -- so without this every optional numeric field needs its own
    try block in the route.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _uploaded_image(request_):
    """The bytes of an uploaded screenshot, or a (json, status) failure."""
    sent = request_.files.get("file") or request_.files.get("image")
    if not sent:
        return jsonify({"error": "no image was uploaded"}), 400
    payload = sent.read()
    if not payload:
        return jsonify({"error": "the uploaded image was empty"}), 400
    if not receipts.kind_of(payload):
        return jsonify({
            "error": "that is not an image this app recognises; a PNG, JPEG, "
                     "GIF, BMP or WebP screenshot is what it expects"}), 400
    return payload


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
    # Watch the inbox folder. Started here rather than in create_app so that
    # importing the module -- which every test does -- never spawns a thread
    # that writes to a database.
    folder = sources.inbox_dir()
    os.makedirs(folder, exist_ok=True)
    sources.start_watching()
    print(f"Watching {folder} every {sources.INTERVAL_SECONDS}s")
    print("  drop a bank export there and it imports itself")

    print(f"Wallet FX & Budget on http://{HOST}:{PORT}")
    try:
        app.run(host=HOST, port=PORT, debug=DEBUG)
    finally:
        sources.stop_watching()
