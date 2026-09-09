"""Reading a purchase off a screenshot.

The tests build OCR boxes by hand rather than running the engine. That is the
whole reason ocr.py and receipts.py are separate modules: recognition is a
solved problem somebody else ships, and every way this can go wrong lives in
the parsing. Hand-built boxes also make the cases that matter expressible --
"the euro sign did not survive", "the amount is the tallest text but not the
first" -- which a fixture image cannot pin down.

Every string here came out of the real engine on a real screenshot, including
the mangled ones: "8September2026at14:32" with the spaces eaten, "18,90 " with
the euro sign dropped, "REWESAGTDANKEBERLINDE" run together. Inventing tidier
input would have tested a parser for a problem this one does not have.
"""
import datetime as dt
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ocr        # noqa: E402
import receipts   # noqa: E402

TODAY = dt.date(2026, 9, 9)


def box(text, top=0, height=14, left=24, confidence=0.85):
    """One OCR box. `height` is the interesting parameter: rendered size is
    how the amount is told apart from every other number on the screen."""
    return ocr.Box(text=text, left=left, top=top, width=len(text) * 8,
                   height=height, confidence=confidence)


def read(*boxes, today=TODAY):
    return receipts.parse(list(boxes), today=today)


# ============================================================== the amount

def test_the_amount_is_the_tallest_number_not_the_first():
    """The signal the whole module rests on. A card screen puts the figure
    you opened it to see in the largest type; position is a much weaker clue,
    and the reference number at the top is often the first number of all."""
    found = read(
        box("Reference 8842-QK", top=10, height=12),
        box("Card fee 1,50 EUR", top=40, height=12),
        box("-52,30 EUR", top=80, height=34),
    )
    assert found["amount"]["minor"] == 5230
    assert found["amount"]["currency"] == "EUR"


def test_the_other_amounts_are_kept_and_offered():
    """The heuristic can be wrong, so every candidate survives for a person
    to pick from -- ranked, not discarded."""
    found = read(box("2,50 EUR", top=40, height=12),
                 box("-52,30 EUR", top=80, height=34))
    assert [a["minor"] for a in found["amounts"]] == [5230, 250]


@pytest.mark.parametrize("text,minor,currency", [
    ("-52,30 EUR", 5230, "EUR"),          # comma decimal, code after
    ("EUR 52.30", 5230, "EUR"),           # code before
    ("€3.50", 350, "EUR"),
    ("18,90", 1890, None),                # the engine dropped the euro sign
    ("$85.94", 8594, None),               # "$" cannot say which dollar
    ("CA$85.94", 8594, "CAD"),
    ("US$85.94", 8594, "USD"),
    # Both decimal conventions, read by money.parse. Neither carries a
    # marker, so the currency is genuinely unknown -- the grouping tells you
    # the locale of whoever formatted it, not what the money was.
    ("1.234,56", 123456, None),
    ("1,234.56", 123456, None),
    ("1,234.56 EUR", 123456, "EUR"),
])
def test_amount_shapes(text, minor, currency):
    found = read(box(text, height=30))
    assert found["amount"]["minor"] == minor
    assert found["amount"]["currency"] == currency


@pytest.mark.parametrize("text,why", [
    ("2026", "a year"),
    ("14:32", "a time"),
    ("4417", "the last four of a card"),
    ("8842", "a reference number"),
    ("1.64321", "an exchange rate"),
    ("Reference 8842-QK", "a reference with a dash"),
    ("Posted", "no digits at all"),
])
def test_what_is_not_an_amount(text, why):
    """The rule that keeps a year out of a budget. A bare run of digits needs
    a currency marker or a two-decimal tail before it is money -- without
    that, "2026" is a EUR 20.26 purchase, or worse a EUR 2026 one."""
    found = read(box(text, height=30))
    assert found["amount"] is None, why
    assert "amount" in found["needs"]


def test_a_dollar_sign_is_not_resolved_to_a_currency():
    """On a CIBC statement it is Canadian and on a US receipt it is not, and
    nothing in a screenshot settles it. Guessing here would be silently wrong
    on exactly the transactions this app exists to get right."""
    found = read(box("$85.94", height=30))
    assert found["amount"]["currency"] is None
    assert "currency" in found["needs"]


def test_a_negative_is_noted_but_the_amount_is_positive():
    """A card detail shows a purchase as negative. The sign is recorded and
    the magnitude is what gets stored, because the ledger owns the sign."""
    found = read(box("-52,30 EUR", height=30))
    assert found["amount"]["negative"] is True
    assert found["amount"]["minor"] == 5230


def test_a_unicode_minus_counts_as_a_sign():
    found = read(box("−52,30 EUR", height=30))
    assert found["amount"]["negative"] is True


def test_the_confidence_travels_with_the_amount():
    """"18,90 " came back at 0.64. A reader deciding whether to trust a
    figure needs to know the engine was unsure of it."""
    found = read(box("18,90", height=27, confidence=0.64))
    assert found["amount"]["confidence"] == 0.64
    assert found["amount"]["height"] == 27


def test_the_same_figure_read_twice_appears_once():
    found = read(box("52,30 EUR", top=10, height=30),
                 box("52,30 EUR", top=90, height=12))
    assert len(found["amounts"]) == 1
    assert found["amounts"][0]["height"] == 30, "the larger rendering is kept"


# ================================================================= the date

@pytest.mark.parametrize("text,iso", [
    ("2026-09-08", "2026-09-08"),
    ("8 September 2026", "2026-09-08"),
    ("8September2026at14:32", "2026-09-08"),      # spaces eaten by the engine
    ("Sep 8, 2026", "2026-09-08"),
    ("September 8 2026", "2026-09-08"),
    ("8 Sep 2026", "2026-09-08"),
])
def test_date_shapes(text, iso):
    found = read(box(text))
    assert found["date"]["iso"] == iso
    assert not found["date"]["ambiguous"]


def test_the_run_together_date_is_found_at_all():
    """This is the case a word boundary silently loses. "2026at14:32" has no
    \\b between the 6 and the a -- both are word characters -- so a trailing
    \\b matched nothing and the date came back None on the first real
    screenshot tried."""
    found = read(box("8September2026at14:32"))
    assert found["date"] is not None
    assert found["date"]["iso"] == "2026-09-08"


def test_a_slash_date_that_could_be_either_says_so():
    """07/09/2026 is the 7th of September to half the world and the 9th of
    July to the other half. Nothing on the screenshot settles it, so it is
    reported as unsettled and the caller has to ask."""
    found = read(box("07/09/2026"))
    assert found["date"]["ambiguous"] is True
    assert found["date"]["iso"] == "2026-09-07"
    assert found["date"]["alternative"] == "2026-07-09"
    assert "date" in found["needs"]


def test_a_slash_date_that_can_only_be_one_thing_is_not_ambiguous():
    """25/12 has no month 25, so there is nothing to ask about."""
    found = read(box("25/12/2025"))
    assert found["date"]["iso"] == "2025-12-25"
    assert found["date"]["ambiguous"] is False


def test_a_written_month_is_never_ambiguous():
    """The one format with no ambiguity in it: a day cannot be read as
    "September"."""
    found = read(box("09 September 2026"))
    assert found["date"]["ambiguous"] is False


def test_a_future_date_is_not_a_purchase():
    """A screenshot of a purchase is of one that happened. A misread digit
    turning 2026 into 2062 must not produce a transaction dated in the
    future, where it would sit outside every month's report."""
    found = read(box("8 September 2062"))
    assert found["date"] is None
    assert "date" in found["needs"]


def test_a_date_from_the_distant_past_is_not_a_purchase():
    found = read(box("8 September 1994"))
    assert found["date"] is None


def test_tomorrow_is_allowed_because_of_timezones():
    """A purchase made late in Berlin can be stamped tomorrow by a card app
    reading a different clock. One day of grace, not more."""
    found = read(box("10 September 2026"), today=dt.date(2026, 9, 9))
    assert found["date"]["iso"] == "2026-09-10"
    assert read(box("11 September 2026"), today=dt.date(2026, 9, 9))["date"] \
        is None


def test_an_impossible_date_is_ignored_rather_than_raising():
    found = read(box("31 February 2026"))
    assert found["date"] is None


# ============================================================= the merchant

def test_the_merchant_is_the_tallest_line_that_is_not_a_figure():
    found = read(
        box("Transaction", top=10, height=14),
        box("REWE SAGT DANKE", top=40, height=20),
        box("-52,30 EUR", top=80, height=34),
        box("8 September 2026", top=130, height=14),
    )
    assert found["merchant"] == "REWE SAGT DANKE"


def test_a_field_label_is_not_a_merchant():
    """A screen labels its own fields, and those labels are often the largest
    text after the amount. "Amount" is not a shop."""
    found = read(box("Payment successful", top=10, height=22),
                 box("Amount", top=50, height=20),
                 box("18,90", top=80, height=30),
                 box("Cafe Nero Alexanderplatz", top=140, height=19))
    assert found["merchant"] == "Cafe Nero Alexanderplatz"


def test_a_mostly_numeric_line_is_not_a_merchant():
    found = read(box("4417 8842 1120", top=10, height=30),
                 box("Kaufland", top=60, height=14))
    assert found["merchant"] == "Kaufland"


def test_no_merchant_is_none_rather_than_a_guess():
    found = read(box("-52,30 EUR", height=34))
    assert found["merchant"] is None


# ================================================== what the bank disclosed

def test_a_disclosed_original_and_rate_are_taken_as_authoritative():
    """"Foreign currency 52.30 EUR @ 1.64321" is the bank stating what it
    actually did. It is the same disclosure a .qfx carries and it beats
    anything the parser infers, because 52.30 x 1.64321 is exactly the 85.94
    that was billed."""
    found = read(box("Sep 8, 2026", top=10),
                 box("$85.94", top=40, height=16),
                 box("Foreign currency 52.30 EUR @ 1.64321", top=70,
                     height=13))
    assert found["conversion"] == {"minor": 5230, "currency": "EUR",
                                   "rate": "1.64321", "plain": "52.30"}


def test_a_plain_euro_purchase_reports_no_conversion():
    """The bug this guards. On a euro purchase the only amount on screen is
    "-52,30 EUR", the extractor finds it, and reporting that as a foreign
    original invites comparing the amount against itself -- which yields a
    confident zero-cost conversion that never took place."""
    found = read(box("REWE SAGT DANKE", top=10, height=20),
                 box("-52,30 EUR", top=50, height=34))
    assert found["conversion"] is None


def test_a_disclosure_with_no_rate_still_counts_when_the_figures_differ():
    found = read(box("$85.94", top=10, height=20),
                 box("52.30 EUR", top=50, height=12))
    assert found["conversion"]["minor"] == 5230
    assert found["conversion"]["rate"] is None


# ================================================================= the card

@pytest.mark.parametrize("text", [
    "CIBC Dividend Visa *4417",       # what the engine makes of two bullets
    "CIBC Dividend Visa ••4417",
    "Visa xxxx4417",
    "Card ending 4417",
    "Card ending in 4417",
])
def test_the_card_mask_is_found(text):
    assert read(box(text))["card_mask"] == "4417"


def test_one_bullet_is_enough():
    """The engine renders "••4417" as "*4417". Requiring two mask characters
    found no card at all on the first screenshot tried."""
    assert read(box("Visa *4417"))["card_mask"] == "4417"


def test_a_bare_number_is_not_a_card_mask():
    assert read(box("Reference 8842"))["card_mask"] is None


# ================================================================== overall

def test_nothing_at_all_is_an_empty_reading_not_an_error():
    """A photo of a wall. Every key is present so a caller never has to test
    for one, and `needs` says what is missing."""
    found = receipts.parse([], today=TODAY)
    assert found["amount"] is None
    assert found["date"] is None
    assert found["merchant"] is None
    assert set(found["needs"]) == {"amount", "date"}


def test_a_complete_screenshot_needs_nothing():
    found = read(box("REWE SAGT DANKE", top=40, height=20),
                 box("-52,30 EUR", top=80, height=34),
                 box("8September2026at14:32", top=130, height=14),
                 box("CIBC Dividend Visa *4417", top=170, height=12))
    assert found["needs"] == []
    assert found["amount"]["minor"] == 5230
    assert found["date"]["iso"] == "2026-09-08"
    assert found["card_mask"] == "4417"


def test_every_key_is_always_present():
    for boxes in ([], [box("nothing useful")], [box("-52,30 EUR", height=30)]):
        found = receipts.parse(boxes, today=TODAY)
        for key in ("amount", "amounts", "date", "dates", "merchant",
                    "card_mask", "conversion", "needs", "text"):
            assert key in found, key


def test_none_instead_of_boxes_does_not_raise():
    assert receipts.parse(None, today=TODAY)["amount"] is None


# ================================================================== storage

PNG = b"\x89PNG\r\n\x1a\n" + b"pretend this is a screenshot"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg body"
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"body"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("payload,suffix", [
    (PNG, ".png"), (JPEG, ".jpg"), (WEBP, ".webp"),
    (b"GIF89a" + b"x", ".gif"), (b"BM" + b"x" * 20, ".bmp"),
])
def test_the_image_type_comes_from_the_bytes(payload, suffix):
    """Sniffed, not taken from the upload's filename. A name is text the
    browser was handed and can claim ".png" about anything."""
    assert receipts.kind_of(payload) == suffix


@pytest.mark.parametrize("payload", [
    b"", None, b"GET / HTTP/1.1", b"%PDF-1.4 not an image",
    b"RIFF" + b"\x00" * 4 + b"WAVE",       # a RIFF container that is audio
])
def test_what_is_not_an_image(payload):
    assert receipts.kind_of(payload) is None


def test_a_stored_receipt_is_named_by_its_contents(data_dir):
    name = receipts.store(PNG)
    assert re.fullmatch(r"[0-9a-f]{32}\.png", name)
    assert (data_dir / receipts.FOLDER / name).read_bytes() == PNG


def test_the_same_screenshot_twice_is_one_file(data_dir):
    """The same rule the imports table has for statements, for the same
    reason: identity is the contents, not the name."""
    first, second = receipts.store(PNG), receipts.store(PNG)
    assert first == second
    assert len(os.listdir(data_dir / receipts.FOLDER)) == 1


def test_two_different_screenshots_are_two_files(data_dir):
    receipts.store(PNG)
    receipts.store(JPEG)
    assert len(os.listdir(data_dir / receipts.FOLDER)) == 2


def test_something_that_is_not_an_image_is_refused(data_dir):
    with pytest.raises(receipts.ReceiptError):
        receipts.store(b"%PDF-1.4")


def test_a_stored_name_resolves_to_its_file(data_dir):
    name = receipts.store(PNG)
    assert os.path.isfile(receipts.stored_path(name))


@pytest.mark.parametrize("name", [
    "../../../etc/passwd",
    "..\\..\\windows\\win.ini",
    "C:/Windows/win.ini",
    "/etc/passwd",
    "notadigest.png",
    "",
    None,
    "5e226d888f4417d1273386bf8be0396c.png/../../x",
])
def test_a_name_this_app_did_not_write_resolves_to_nothing(data_dir, name):
    """The serving route calls this and gets None rather than a file.

    Refused rather than sanitised: turning a suspect name into a plausible
    one means the caller cannot tell an attack from a typo, and every name
    this app writes is a hex digest and an extension.
    """
    receipts.store(PNG)
    assert receipts.stored_path(name) is None


def test_a_well_formed_name_with_no_file_is_none(data_dir):
    assert receipts.stored_path("0" * 32 + ".png") is None


def test_the_folder_is_created_on_demand(data_dir):
    assert not (data_dir / receipts.FOLDER).exists()
    receipts.folder()
    assert (data_dir / receipts.FOLDER).is_dir()


def test_an_explicit_directory_is_honoured(tmp_path):
    """So a caller pointing at another data directory does not silently write
    into the default one."""
    elsewhere = tmp_path / "other"
    name = receipts.store(PNG, str(elsewhere))
    assert (elsewhere / receipts.FOLDER / name).is_file()
    assert receipts.stored_path(name, str(elsewhere))


# ==================================================== the remaining corners

def test_a_zero_amount_is_not_a_purchase():
    """money.parse reads "0,00" as zero, and a zero-euro line on a screenshot
    is a fee waiver or a heading, not something to record."""
    assert read(box("0,00 EUR", height=30))["amount"] is None


def test_the_same_date_appearing_twice_is_listed_once():
    found = read(box("8 September 2026", top=10),
                 box("8 September 2026", top=90))
    assert len(found["dates"]) == 1


def test_a_word_that_is_not_a_month_is_not_a_date():
    """The regex matches any 3-9 letters between a day and a year, so the
    lookup has to reject the ones that are not months."""
    assert read(box("12 Widgets 2026"))["date"] is None


def test_a_very_short_line_is_not_a_merchant():
    found = read(box("OK", top=10, height=40),
                 box("Kaufland", top=60, height=14))
    assert found["merchant"] == "Kaufland"


def test_a_line_that_is_mostly_digits_but_has_letters_is_not_a_merchant():
    found = read(box("4417 8842 1120 x", top=10, height=40),
                 box("Kaufland", top=60, height=14))
    assert found["merchant"] == "Kaufland"
