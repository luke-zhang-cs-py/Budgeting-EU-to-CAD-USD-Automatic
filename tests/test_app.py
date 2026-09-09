"""The HTTP surface: routes, validation, and the export downloads."""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as web   # noqa: E402
import db           # noqa: E402
import fetch      # noqa: E402
import fxrates      # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    fxrates.reset()
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("date,CAD,USD\n2026-02-02,1.6033,1.1614\n")
    application = web.create_app(str(tmp_path))
    application.config["TESTING"] = True
    yield application.test_client()
    fxrates.reset()


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path))
    yield connection
    connection.close()


def post(client, url, **fields):
    return client.post(url, data=json.dumps(fields),
                       content_type="application/json")


# -------------------------------------------------------------------- pages

def test_the_page_loads(client):
    assert client.get("/").status_code == 200


def test_the_overview_answers_on_an_empty_ledger(client):
    """Day one has no transactions, and the page must still render."""
    body = client.get("/api/overview").get_json()
    assert body["summary"]["spent_eur"] == 0
    assert body["budgets"] == []
    assert body["rates"]["available"] is True


# -------------------------------------------------------------- adding rows

def test_a_manual_entry_is_recorded_as_spending(client):
    """Typed as a positive number and meant as spending. Requiring a minus
    for every coffee is the friction that stops people using a tracker."""
    reply = post(client, "/api/transaction", date="2026-02-02",
                 description="Coffee", amount="3,50")
    assert reply.status_code == 201
    row = client.get("/api/transactions").get_json()["transactions"][0]
    assert row["amount_eur"] == -350
    assert row["amount_cad"] == -561


def test_income_is_recorded_positive_when_marked_as_such(client):
    post(client, "/api/transaction", date="2026-02-02", description="Salary",
         amount="2500", kind="income", category="Income")
    row = client.get("/api/transactions").get_json()["transactions"][0]
    assert row["amount_eur"] == 250000


def test_a_duplicate_entry_reports_itself_rather_than_erroring(client):
    post(client, "/api/transaction", date="2026-02-02", description="Coffee",
         amount="3,50")
    again = post(client, "/api/transaction", date="2026-02-02",
                 description="Coffee", amount="3,50")
    assert again.status_code == 200
    assert again.get_json()["result"] == "duplicate"


def test_an_unreadable_amount_is_a_400_not_a_500(client):
    reply = post(client, "/api/transaction", date="2026-02-02",
                 description="Coffee", amount="abc")
    assert reply.status_code == 400
    assert "error" in reply.get_json()


def test_a_zero_amount_is_refused(client):
    reply = post(client, "/api/transaction", date="2026-02-02",
                 description="Nothing", amount="0")
    assert reply.status_code == 400


def test_a_missing_description_is_refused(client):
    reply = post(client, "/api/transaction", date="2026-02-02",
                 description="", amount="5")
    assert reply.status_code == 400


def test_a_row_can_be_recategorised_and_deleted(client):
    tid = post(client, "/api/transaction", date="2026-02-02",
               description="Tesco", amount="45,50").get_json()["id"]
    assert client.post(f"/api/transaction/{tid}/category",
                       data=json.dumps({"category": "Groceries"}),
                       content_type="application/json").status_code == 200
    assert client.get("/api/transactions").get_json()[
        "transactions"][0]["category"] == "Groceries"
    assert client.delete(f"/api/transaction/{tid}").status_code == 200
    assert client.get("/api/transactions").get_json()["transactions"] == []


def test_an_empty_category_is_refused(client):
    tid = post(client, "/api/transaction", date="2026-02-02",
               description="Tesco", amount="45,50").get_json()["id"]
    assert client.post(f"/api/transaction/{tid}/category",
                       data=json.dumps({"category": "  "}),
                       content_type="application/json").status_code == 400


# ------------------------------------------------------------------ import

CSV = ("Date,Description,Amount,Currency\n"
       "2026-02-02,TESCO STORES,-45.50,EUR\n"
       "2026-02-02,DB BAHN,-39.90,EUR\n")


def upload(client, url, text, **fields):
    data = {"file": (io.BytesIO(text.encode("utf-8")), "statement.csv")}
    data.update(fields)
    return client.post(url, data=data, content_type="multipart/form-data")


def test_a_preview_shows_what_would_happen_and_writes_nothing(client):
    body = upload(client, "/api/import/preview", CSV).get_json()
    assert body["readable"] == 2
    assert body["mapping"]["date"] == "Date"
    assert body["spending_total_text"] == "-€85.40"
    assert client.get("/api/transactions").get_json()["transactions"] == []


def test_committing_the_import_records_the_rows(client):
    body = upload(client, "/api/import/commit", CSV).get_json()
    assert body["added"] == 2
    assert len(client.get("/api/transactions").get_json()["transactions"]) == 2


def test_reimporting_the_same_file_adds_nothing(client):
    upload(client, "/api/import/commit", CSV)
    body = upload(client, "/api/import/commit", CSV).get_json()
    assert body["added"] == 0
    assert body["duplicate"] == 2


def test_pasted_text_works_as_well_as_a_file(client):
    reply = client.post("/api/import/preview", data={"text": CSV},
                        content_type="multipart/form-data")
    assert reply.get_json()["readable"] == 2


def test_an_upload_with_nothing_in_it_is_a_400(client):
    assert client.post("/api/import/preview", data={},
                       content_type="multipart/form-data").status_code == 400


def test_a_nonsense_file_is_a_400_with_a_reason(client):
    reply = upload(client, "/api/import/preview", "not a csv at all\n")
    assert reply.status_code == 400
    assert "error" in reply.get_json()


def test_the_column_mapping_can_be_overridden_from_the_form(client):
    """The guess is a guess. The browser must be able to correct it."""
    text = ("when,what,how much\n2026-02-02,TESCO,45.50\n")
    reply = upload(client, "/api/import/preview", text,
                   date="when", description="what", amount="how much",
                   expenses_positive="on")
    body = reply.get_json()
    assert body["readable"] == 1
    assert body["shown"][0]["amount_eur"] == -4550


# ----------------------------------------------------------------- budgets

def test_a_budget_is_set_and_reported(client):
    post(client, "/api/transaction", date="2026-02-02", description="Tesco",
         amount="200", category="Groceries")
    reply = post(client, "/api/budget", category="Groceries", cap="400")
    rows = reply.get_json()["budgets"]
    groceries = next(r for r in rows if r["category"] == "Groceries")
    assert groceries["cap_eur"] == 40000


def test_a_budget_without_a_category_is_refused(client):
    assert post(client, "/api/budget", category="", cap="400").status_code == 400


def test_an_unreadable_cap_is_refused(client):
    assert post(client, "/api/budget", category="Groceries",
                cap="lots").status_code == 400


# ------------------------------------------------------------------- rules

def test_a_rule_is_added_and_fixes_existing_rows(client):
    post(client, "/api/transaction", date="2026-02-02",
         description="NETFLIX.COM", amount="13,99")
    reply = post(client, "/api/rules", keyword="netflix",
                 category="Entertainment")
    assert reply.status_code == 201
    assert reply.get_json()["recategorised"] == 1
    assert client.get("/api/transactions").get_json()[
        "transactions"][0]["category"] == "Entertainment"


def test_a_rule_can_be_listed_and_deleted(client):
    post(client, "/api/rules", keyword="tesco", category="Groceries")
    rules = client.get("/api/rules").get_json()["rules"]
    assert len(rules) == 1
    assert client.delete(f"/api/rules/{rules[0]['id']}").status_code == 200
    assert client.get("/api/rules").get_json()["rules"] == []


def test_a_rule_without_a_keyword_is_refused(client):
    assert post(client, "/api/rules", keyword="",
                category="Groceries").status_code == 400


# ------------------------------------------------------------------ export

def test_the_transaction_download_is_a_csv_attachment(client):
    post(client, "/api/transaction", date="2026-02-02", description="Tesco",
         amount="45,50")
    reply = client.get("/export/transactions.csv?month=2026-02")
    assert reply.status_code == 200
    assert reply.mimetype == "text/csv"
    assert "attachment" in reply.headers["Content-Disposition"]
    assert ".csv" in reply.headers["Content-Disposition"]
    text = reply.get_data(as_text=True)
    assert "amount_cad" in text.splitlines()[0]
    assert "-45.50" in text


def test_the_summary_download_works(client):
    post(client, "/api/transaction", date="2026-02-02", description="Tesco",
         amount="200", category="Groceries")
    post(client, "/api/budget", category="Groceries", cap="400")
    text = client.get("/export/summary.csv?month=2026-02").get_data(
        as_text=True)
    assert "Groceries" in text


def test_the_file_is_reported_to_reconcile_with_the_ledger(client):
    post(client, "/api/transaction", date="2026-02-02", description="Tesco",
         amount="45,50")
    body = client.get("/api/reconciles?month=2026-02").get_json()
    assert body["agrees"] is True
    assert body["file_eur"] == body["ledger_eur"] == -4550


# ------------------------------------------------------------------- rates

def test_a_failed_rate_refresh_is_reported_not_a_server_error(client,
                                                              monkeypatch):
    """Offline is a normal state, not a 500."""
    monkeypatch.setattr(fetch, "get", lambda *a, **k: None)
    reply = client.post("/api/rates/refresh")
    assert reply.status_code == 200
    assert reply.get_json()["ok"] is False


# ------------------------------------------------------------------ safety

def test_the_server_binds_loopback_by_default(monkeypatch):
    """The sibling app in this family bound 0.0.0.0 with DEBUG on, putting an
    interactive console on every interface. This holds financial history.

    HOST is cleared first, because this is a claim about the default and the
    module reads the environment -- the lesson from that same repo.
    """
    import importlib
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    reloaded = importlib.reload(web)
    assert reloaded.HOST == "127.0.0.1"
    assert reloaded.DEBUG is False
    monkeypatch.undo()
    importlib.reload(web)


def test_an_oversized_upload_is_capped():
    assert web.MAX_UPLOAD_BYTES <= 16 * 1024 * 1024


# ------------------------------------------------- the last failure branches

def test_committing_an_import_with_no_file_is_a_400(client):
    """The commit route has its own copy of the upload failure path; only the
    preview route's was exercised, so a broken commit would have surfaced as a
    500 rather than a message."""
    reply = client.post("/api/import/commit", data={},
                        content_type="multipart/form-data")
    assert reply.status_code == 400
    assert "error" in reply.get_json()


def test_an_uploaded_file_with_no_bytes_says_so(client):
    """A zero-byte file is a different mistake from choosing no file, and the
    reason has to distinguish them or the user re-picks the same empty file."""
    reply = client.post(
        "/api/import/preview",
        data={"file": (io.BytesIO(b""), "empty.csv")},
        content_type="multipart/form-data")
    assert reply.status_code == 400
    assert reply.get_json()["error"] == "the file is empty"


def test_a_negative_cap_is_refused_by_the_api(client):
    """money.parse reads "-100" happily -- it is a real amount. The refusal
    comes from budgets.set_cap, and that branch had no test, so a negative cap
    would have been a 500 instead of a message."""
    reply = post(client, "/api/budget", category="Groceries", cap="-100")
    assert reply.status_code == 400
    assert "negative" in reply.get_json()["error"]


# --------------------------------------------------------- the watched folder

def test_the_sources_endpoint_reports_the_folder(client):
    body = client.get("/api/sources").get_json()
    assert body["inbox"]["folder"].endswith("inbox")
    assert body["inbox"]["watching"] is False
    assert body["history"] == []


def test_scanning_on_demand_imports_what_is_waiting(client, tmp_path):
    """The button beside the folder, for when you do not want to wait for
    the timer."""
    import sources
    inbox = sources.inbox_dir(str(tmp_path))
    os.makedirs(inbox, exist_ok=True)
    with open(os.path.join(inbox, "cibc.csv"), "w", encoding="utf-8") as handle:
        handle.write("Date,Description,Amount,Currency\n"
                     "2026-02-02,REWE SAGT DANKE,-52.30,EUR\n")

    reply = client.post("/api/sources/scan", data={},
                        content_type="multipart/form-data")
    assert reply.status_code == 200
    body = reply.get_json()
    assert body["results"][0]["added"] == 1
    assert body["history"][0]["filename"] == "cibc.csv"
    assert len(client.get("/api/transactions").get_json()["transactions"]) == 1
