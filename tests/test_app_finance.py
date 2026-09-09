"""The HTTP surface added for screenshots, live rates, cards and planning.

Kept apart from test_app.py, which covers the original routes, because these
share a heavier fixture: a rate cache, a stubbed rate source, and in one case
a stubbed OCR engine. The theme is the same as everywhere else in this
project -- what the endpoint refuses, and what it declines to claim.
"""
import io
import json
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as web   # noqa: E402
import cards        # noqa: E402
import fetch        # noqa: E402
import fxlive       # noqa: E402
import fxrates      # noqa: E402
import ocr          # noqa: E402

RATES = ("date,CAD,USD\n"
         "2026-09-08,1.6350,1.1650\n"
         "2026-09-07,1.6340,1.1640\n")

QUOTE = json.dumps({"date": "2026-09-09",
                    "rates": {"CAD": 1.6043, "USD": 1.1652}}).encode()

PNG = b"\x89PNG\r\n\x1a\n" + b"pretend this is a screenshot"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    fxrates.reset()
    fxlive.reset()
    path = fxrates.cache_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(RATES)
    application = web.create_app(str(tmp_path))
    application.config["TESTING"] = True
    yield application.test_client()
    fxrates.reset()
    fxlive.reset()


def post(client, url, **fields):
    return client.post(url, data=json.dumps(fields),
                       content_type="application/json")


def upload(client, url, payload, field="file", name="shot.png"):
    return client.post(url, data={field: (io.BytesIO(payload), name)},
                       content_type="multipart/form-data")


def a_card(client, **fields):
    body = {"name": "CIBC Dividend Visa", "currency": "CAD", "mask": "4417"}
    body.update(fields)
    return post(client, "/api/cards", **body).get_json()["id"]


# =================================================================== cards

def test_a_card_can_be_created_and_listed(client):
    cid = a_card(client)
    body = client.get("/api/cards").get_json()
    assert [c["id"] for c in body["cards"]] == [cid]
    assert body["cards"][0]["fee_bp"] == 250
    assert body["default_fee_bp"] == 250
    assert body["currencies"] == ["EUR", "CAD", "USD"]


def test_a_bad_card_is_a_400_with_a_reason(client):
    for fields in ({"name": "", "currency": "CAD"},
                   {"name": "x", "currency": "JPY"},
                   {"name": "x", "currency": "CAD", "fee_bp": 9999}):
        reply = post(client, "/api/cards", **fields)
        assert reply.status_code == 400, fields
        assert reply.get_json()["error"]


def test_a_card_can_be_corrected_and_deleted(client):
    cid = a_card(client)
    assert post(client, f"/api/cards/{cid}", fee_bp=239).status_code == 200
    assert client.get("/api/cards").get_json()["cards"][0]["fee_bp"] == 239
    assert client.delete(f"/api/cards/{cid}").status_code == 200
    assert client.get("/api/cards").get_json()["cards"] == []


def test_deleting_a_card_that_is_not_there_is_a_404(client):
    assert client.delete("/api/cards/999").status_code == 404


def test_correcting_a_card_that_is_not_there_is_a_400(client):
    assert post(client, "/api/cards/999", fee_bp=100).status_code == 400


def test_an_empty_fee_field_leaves_the_fee_alone(client):
    """A form sends "" for an untouched number input, and int("") raises.
    Without _int_or_none every optional field needs its own try block."""
    cid = a_card(client)
    assert post(client, f"/api/cards/{cid}", fee_bp="").status_code == 200
    assert client.get("/api/cards").get_json()["cards"][0]["fee_bp"] == 250


def test_a_purchase_can_be_attributed_to_a_card(client):
    cid = a_card(client)
    tid = post(client, "/api/transaction", date="2026-09-08",
               description="REWE", amount="52.30").get_json()["id"]
    assert post(client, f"/api/transaction/{tid}/card",
                card_id=cid).status_code == 200
    assert client.get("/api/cards").get_json()["cards"][0]["rows"] == 1


def test_attributing_to_a_card_that_is_not_there_is_a_400(client):
    tid = post(client, "/api/transaction", date="2026-09-08",
               description="REWE", amount="52.30").get_json()["id"]
    assert post(client, f"/api/transaction/{tid}/card",
                card_id=999).status_code == 400


def test_a_measured_fee_says_so_when_there_is_not_enough_to_measure(client):
    cid = a_card(client)
    body = client.get(f"/api/cards/{cid}/measured").get_json()
    assert body["available"] is False
    assert str(cards.ENOUGH_TO_MEASURE) in body["why"]


def test_measuring_a_card_that_is_not_there_is_a_404(client):
    assert client.get("/api/cards/999/measured").status_code == 404


# =============================================================== live rates

def test_the_live_rate_is_served_with_its_provenance(client, monkeypatch):
    monkeypatch.setattr(fetch, "get", lambda *a, **k: QUOTE)
    body = client.get("/api/rate/live").get_json()
    assert body["available"] is True
    assert body["rates"]["CAD"] == "1.6043"
    assert body["published"] == "2026-09-09"
    assert body["daily_reference"] is True, (
        "the page must be able to say this is a daily fixing, not a tick")


def test_no_rate_source_is_reported_as_unavailable_not_an_error(client,
                                                                monkeypatch):
    """The page is perfectly usable without it, so this is a 200 saying no
    rather than a 500."""
    monkeypatch.setattr(fetch, "get", lambda *a, **k: None)
    reply = client.get("/api/rate/live")
    assert reply.status_code == 200
    assert reply.get_json()["available"] is False


def test_an_estimate_breaks_the_cost_into_conversion_and_fee(client,
                                                             monkeypatch):
    monkeypatch.setattr(fetch, "get", lambda *a, **k: QUOTE)
    cid = a_card(client)
    body = post(client, "/api/rate/estimate", amount="52.30",
                card_id=cid).get_json()

    assert body["currency"] == "CAD"
    assert body["fee_bp"] == 250
    assert body["converted_minor"] + body["fee_minor"] == body["total_minor"]
    assert Decimal(body["effective_rate"]) > Decimal("1.6043")
    assert body["card"] == "CIBC Dividend Visa ..4417"


def test_the_estimate_uses_the_cards_own_fee(client, monkeypatch):
    """The point of storing the fee per card: the same purchase costs more on
    the Visa than on a euro account."""
    monkeypatch.setattr(fetch, "get", lambda *a, **k: QUOTE)
    dear = a_card(client, name="Visa", fee_bp=250)
    free = a_card(client, name="Free card", currency="CAD", mask="1111",
                  fee_bp=0)
    with_fee = post(client, "/api/rate/estimate", amount="100",
                    card_id=dear).get_json()
    without = post(client, "/api/rate/estimate", amount="100",
                   card_id=free).get_json()
    assert with_fee["total_minor"] > without["total_minor"]
    assert without["fee_minor"] == 0


def test_an_estimate_without_a_card_uses_the_published_figure(client,
                                                              monkeypatch):
    monkeypatch.setattr(fetch, "get", lambda *a, **k: QUOTE)
    body = post(client, "/api/rate/estimate", amount="52.30").get_json()
    assert body["fee_bp"] == 250
    assert body["card"] is None


def test_an_unreadable_amount_is_a_400(client, monkeypatch):
    monkeypatch.setattr(fetch, "get", lambda *a, **k: QUOTE)
    for amount in ("abc", "", None):
        assert post(client, "/api/rate/estimate",
                    amount=amount).status_code == 400


def test_an_estimate_with_no_live_rate_is_a_503(client, monkeypatch):
    """Not a zero and not a stale figure: the answer is that it cannot be
    worked out right now."""
    monkeypatch.setattr(fetch, "get", lambda *a, **k: None)
    assert post(client, "/api/rate/estimate",
                amount="52.30").status_code == 503


def test_an_estimate_for_a_currency_with_no_quote_is_a_400(client,
                                                           monkeypatch):
    body = json.dumps({"date": "2026-09-09", "rates": {"CAD": 1.6043}}
                      ).encode()
    monkeypatch.setattr(fetch, "get", lambda *a, **k: body)
    assert post(client, "/api/rate/estimate", amount="52.30",
                currency="USD").status_code == 400


# ================================================================ receipts

def stub_engine(monkeypatch, boxes):
    """An OCR engine that returns fixed boxes, so route tests never need the
    real model."""
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read", lambda _image: boxes)


def a_box(text, top=0, height=14):
    return ocr.Box(text=text, left=24, top=top, width=100, height=height,
                   confidence=0.85)


def test_a_screenshot_is_read_and_reported_without_being_saved(client,
                                                               monkeypatch):
    """Reads and answers; it does not record. The confirm step is separate
    because OCR gets amounts wrong, and a tracker that silently books the
    wrong figure is worse than one that cannot read screenshots."""
    stub_engine(monkeypatch, [
        a_box("REWE SAGT DANKE", top=40, height=20),
        a_box("-52,30 EUR", top=80, height=34),
        a_box("8September2026at14:32", top=130),
    ])
    body = upload(client, "/api/receipt/read", PNG).get_json()

    assert body["ocr"] is True
    assert body["reading"]["amount"]["minor"] == 5230
    assert body["reading"]["date"]["iso"] == "2026-09-08"
    assert body["reading"]["needs"] == []
    assert body["stored"]
    assert client.get("/api/transactions").get_json()["transactions"] == [], \
        "reading a screenshot must not record anything"


def test_the_screenshot_is_kept_even_with_no_engine(client, monkeypatch):
    """Without the optional dependency the upload still works: the image is
    attached to the purchase you then type by hand."""
    monkeypatch.setattr(ocr, "available", lambda: False)
    body = upload(client, "/api/receipt/read", PNG).get_json()
    assert body["ocr"] is False
    assert body["stored"]
    assert "requirements-ocr.txt" in body["why"]


def test_a_matching_card_is_found_from_the_screenshot(client, monkeypatch):
    cid = a_card(client)
    stub_engine(monkeypatch, [a_box("-52,30 EUR", height=34),
                              a_box("CIBC Dividend Visa *4417", top=60)])
    body = upload(client, "/api/receipt/read", PNG).get_json()
    assert body["reading"]["card"]["id"] == cid


def test_what_the_reading_could_not_settle_comes_back_in_needs(client,
                                                               monkeypatch):
    stub_engine(monkeypatch, [a_box("$85.94", height=30),
                              a_box("07/09/2026", top=60)])
    reading = upload(client, "/api/receipt/read", PNG).get_json()["reading"]
    assert set(reading["needs"]) == {"currency", "date"}
    assert reading["date"]["alternative"] == "2026-07-09"


@pytest.mark.parametrize("payload,why", [
    (b"", "empty"),
    (b"%PDF-1.4 nope", "not an image"),
    (b"GET / HTTP/1.1", "not an image either"),
])
def test_an_upload_that_is_not_an_image_is_a_400(client, payload, why):
    assert upload(client, "/api/receipt/read", payload).status_code == 400, why


def test_no_file_at_all_is_a_400(client):
    assert client.post("/api/receipt/read").status_code == 400


def test_an_engine_failure_is_a_400_not_a_500(client, monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)

    def boom(_image):
        raise ocr.OcrError("could not read the image")

    monkeypatch.setattr(ocr, "read", boom)
    reply = upload(client, "/api/receipt/read", PNG)
    assert reply.status_code == 400
    assert reply.get_json()["error"]


def test_a_confirmed_purchase_is_recorded_with_its_screenshot(client,
                                                              monkeypatch):
    stub_engine(monkeypatch, [a_box("-52,30 EUR", height=34)])
    stored = upload(client, "/api/receipt/read", PNG).get_json()["stored"]
    cid = a_card(client)

    reply = post(client, "/api/receipt/save", date="2026-09-08",
                 description="REWE SAGT DANKE", amount="52.30",
                 category="Groceries", stored=stored, card_id=cid,
                 charged_minor=8594, charged_currency="CAD")
    assert reply.status_code == 201
    tid = reply.get_json()["id"]

    row = client.get("/api/transactions").get_json()["transactions"][0]
    assert row["id"] == tid
    assert row["receipt"] == stored
    assert row["card_id"] == cid
    assert row["source"] == "receipt"


def test_saving_with_an_unknown_receipt_name_is_a_400(client):
    """The name has to be one this app wrote. Anything else did not come from
    an upload and has no business being recorded against a row."""
    assert post(client, "/api/receipt/save", date="2026-09-08",
                description="REWE", amount="52.30",
                stored="../../etc/passwd").status_code == 400


def test_saving_without_a_receipt_still_works(client):
    """Typed by hand with no screenshot at all."""
    assert post(client, "/api/receipt/save", date="2026-09-08",
                description="CASH", amount="3.50").status_code == 201


def test_a_bad_amount_on_save_is_a_400(client):
    assert post(client, "/api/receipt/save", date="2026-09-08",
                description="REWE", amount="abc").status_code == 400


def test_a_bad_date_on_save_is_a_400(client):
    assert post(client, "/api/receipt/save", date="not a date",
                description="REWE", amount="1.00").status_code == 400


def test_saving_the_same_purchase_twice_is_a_duplicate_not_an_error(client):
    fields = {"date": "2026-09-08", "description": "REWE", "amount": "52.30"}
    assert post(client, "/api/receipt/save", **fields).status_code == 201
    again = post(client, "/api/receipt/save", **fields)
    assert again.status_code == 200
    assert again.get_json()["result"] == "duplicate"


def test_a_stored_screenshot_can_be_served_back(client, monkeypatch):
    stub_engine(monkeypatch, [a_box("-52,30 EUR", height=34)])
    stored = upload(client, "/api/receipt/read", PNG).get_json()["stored"]
    reply = client.get(f"/receipt/{stored}")
    assert reply.status_code == 200
    assert reply.mimetype == "image/png"
    assert reply.get_data() == PNG


@pytest.mark.parametrize("name", [
    "../../../etc/passwd", "nope.png", "0" * 32 + ".png",
])
def test_asking_for_a_receipt_this_app_did_not_write_is_a_404(client, name):
    assert client.get(f"/receipt/{name}").status_code == 404


# ================================================================ planning

def test_the_trend_report_answers_on_an_empty_ledger(client):
    body = client.get("/api/trends").get_json()
    assert body["months"] == []
    assert body["movers"] == []


def test_the_trend_report_carries_months_and_movers(client):
    for month in ("2026-06", "2026-07", "2026-08"):
        post(client, "/api/transaction", date=f"{month}-05",
             description="REWE", amount="250.00", category="Groceries")
    post(client, "/api/transaction", date="2026-09-05", description="REWE",
         amount="380.00", category="Groceries")
    body = client.get("/api/trends?month=2026-09").get_json()
    assert len(body["months"]) == 4
    assert [m["category"] for m in body["movers"]] == ["Groceries"]


def test_a_category_can_be_drilled_into_over_http(client):
    post(client, "/api/transaction", date="2026-09-05", description="REWE",
         amount="52.30", category="Groceries")
    body = client.get("/api/trends/Groceries?month=2026-09").get_json()
    assert body["count"] == 1
    assert len(body["rows"]) == 1
    assert body["history"]


def test_the_subscription_report_answers_on_an_empty_ledger(client):
    body = client.get("/api/subscriptions").get_json()
    assert body["rows"] == []
    assert body["outstanding"]["count"] == 0
    assert body["cost"]["monthly_minor"] == 0
    assert body["rises"] == []


def test_the_subscription_report_finds_a_rise(client):
    for month, amount in (("2026-05", "9.99"), ("2026-06", "9.99"),
                          ("2026-07", "14.99"), ("2026-08", "14.99")):
        post(client, "/api/transaction", date=f"{month}-11",
             description="SPOTIFY AB", amount=amount)
    body = client.get("/api/subscriptions?month=2026-09").get_json()
    assert len(body["rises"]) == 1
    assert body["rises"][0]["rise"]["by_minor"] == 500


def test_a_goal_can_be_created_listed_and_removed(client):
    reply = post(client, "/api/goals", name="Flight home", target="600",
                 due_on="2026-12-20")
    assert reply.status_code == 201
    gid = reply.get_json()["id"]

    body = client.get("/api/goals").get_json()
    assert [g["id"] for g in body["goals"]] == [gid]
    assert body["goals"][0]["target_minor"] == 60000

    assert client.delete(f"/api/goals/{gid}").status_code == 200
    assert client.get("/api/goals").get_json()["goals"] == []


def test_removing_a_goal_that_is_not_there_is_a_404(client):
    assert client.delete("/api/goals/999").status_code == 404


@pytest.mark.parametrize("fields,why", [
    ({"name": "", "target": "600"}, "no name"),
    ({"name": "x", "target": "abc"}, "unreadable target"),
    ({"name": "x", "target": "0.50"}, "too small to track"),
    ({"name": "x", "target": "600", "due_on": "next Tuesday"}, "bad date"),
])
def test_a_bad_goal_is_a_400_with_a_reason(client, fields, why):
    reply = post(client, "/api/goals", **fields)
    assert reply.status_code == 400, why
    assert reply.get_json()["error"]


def test_the_goal_report_shows_what_the_month_contributed(client):
    post(client, "/api/budget", category="Groceries", cap="300")
    post(client, "/api/transaction", date="2026-09-05", description="REWE",
         amount="52.30", category="Groceries")
    post(client, "/api/goals", name="Flight home", target="600")

    body = client.get("/api/goals?month=2026-09").get_json()
    assert body["contributed"]["saved_minor"] == 24770
    assert body["goals"][0]["this_month_minor"] == 24770


# ================================================== the remaining branches

def test_saving_a_receipt_against_a_card_that_is_not_there_is_a_400(client):
    assert post(client, "/api/receipt/save", date="2026-09-08",
                description="REWE", amount="52.30",
                card_id=999).status_code == 400


def test_estimating_nothing_is_a_400(client, monkeypatch):
    """money.parse reads "0.00" as zero, and there is nothing to estimate."""
    monkeypatch.setattr(fetch, "get", lambda *a, **k: QUOTE)
    reply = post(client, "/api/rate/estimate", amount="0.00")
    assert reply.status_code == 400
    assert reply.get_json()["error"]


def test_a_measured_fee_is_served_once_there_is_enough_to_measure(client):
    """Three statement-priced purchases on the card, so the markup can be
    compared against the reference rate rather than assumed."""
    cid = a_card(client)
    for n in range(3):
        tid = post(client, "/api/receipt/save", date="2026-09-08",
                   description=f"REWE {n}", amount="52.30",
                   charged_minor=8594, charged_currency="CAD",
                   card_id=cid).get_json()["id"]
        assert tid

    body = client.get(f"/api/cards/{cid}/measured").get_json()
    assert body["available"] is True
    assert body["rows"] == 3
    assert body["published_bp"] == 250


def test_the_billed_figure_is_taken_as_written_text(client):
    """Sent as "85,94" or "85.94" and read by money.parse, which already
    handles both conventions -- so the browser does no money arithmetic and
    there is no second parser in JavaScript to drift from this one."""
    # Distinct merchants, not distinct amounts in the description. normalise
    # strips punctuation, so "REWE 85.94" and "REWE 85,94" fingerprint
    # identically and the second really is a duplicate -- the first version of
    # this test hit that and read it as a failure of the parsing.
    for shop, written, cents in (("ALDI", "85.94", 8594),
                                 ("LIDL", "85,94", 8594),
                                 ("EDEKA", "1.234,56", 123456)):
        reply = post(client, "/api/receipt/save", date="2026-09-08",
                     description=shop, amount="52.30",
                     charged=written, charged_currency="CAD")
        assert reply.status_code == 201, written
        rows = client.get("/api/transactions").get_json()["transactions"]
        row = [r for r in rows if r["description"] == shop][0]
        assert row["charged_minor"] == cents, written


def test_an_unreadable_billed_figure_is_dropped_not_fatal(client):
    """The purchase itself is fine; only the optional billed figure was
    unreadable. Refusing the whole row would lose a real expense to a stray
    keystroke in a field that is allowed to be empty."""
    reply = post(client, "/api/receipt/save", date="2026-09-08",
                 description="REWE", amount="52.30", charged="not a number",
                 charged_currency="CAD")
    assert reply.status_code == 201
    row = client.get("/api/transactions").get_json()["transactions"][0]
    assert row["charged_minor"] is None


def test_cents_can_still_be_sent_directly(client):
    """A caller that already holds cents -- the importers do -- is unchanged."""
    post(client, "/api/receipt/save", date="2026-09-08", description="REWE",
         amount="52.30", charged_minor=8594, charged_currency="CAD")
    row = client.get("/api/transactions").get_json()["transactions"][0]
    assert row["charged_minor"] == 8594
