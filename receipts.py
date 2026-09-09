"""
receipts.py
-----------
Turning the text on a purchase screenshot into a purchase.

Pure: it takes the boxes ocr.py produced and returns a *reading*. It touches
no database, no network and no engine, so its tests build boxes by hand and
run in milliseconds without the 60 MB model. That split is the point -- the
recognition is somebody else's solved problem, and all the ways this can go
wrong live here.

Three rules shape the whole module.

**The amount is the biggest thing on the screen.** Not the first number, not
the last: whoever designed that screen made the figure large because it is
the one you opened it to see. Rendered height beats position, and it is the
single most reliable signal on a screenshot. Every other candidate is kept
and offered, ranked, because the heuristic can be wrong.

**A number is not an amount unless it says so.** "2026" is a year, "14:32" is
a time, "1.64321" is an exchange rate and "4417" is the last four of a card.
A token qualifies only if it carries a currency marker or ends in exactly two
decimals. Without that rule the year is a very large purchase.

**Nothing is ever certain enough to save by itself.** Every reading comes back
with what it could not settle -- an unknown currency, a date that is either
the 7th of September or the 9th of July -- and the caller must put it in
front of a person before it becomes a row. A tracker that silently records
the wrong amount is worse than one that cannot read the screenshot at all,
because the first kind is discovered a month later and the second in a
second.
"""
import datetime as dt
import hashlib
import os
import re

import db
import fxcost
import money
import paths

# Where uploaded screenshots are kept, under the data directory. Gitignored
# along with everything else in there -- a folder of pictures of what somebody
# bought is the most personal thing this app holds.
FOLDER = "receipts"

# Recognised by their magic bytes, not by the name the browser sent. A
# filename is attacker-controlled text; the first few bytes of the file are
# the only thing that says what it actually is.
MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)
# WebP is "RIFF....WEBP" -- a container, so the marker is not at offset zero.
RIFF_WEBP = (b"RIFF", b"WEBP")

# A currency marker sitting on or beside the number. "$" is deliberately not
# resolved to a currency: on a CIBC statement it is Canadian and on a US
# receipt it is not, and there is nothing in a screenshot that settles it.
SYMBOLS = {"€": "EUR", "$": None, "£": None, "C$": "CAD", "CA$": "CAD",
           "US$": "USD"}
CODES = ("EUR", "CAD", "USD")

# Words that label a figure rather than name a shop. Matched case-folded
# against the whole box, so a merchant genuinely called "Status Coffee" is
# unaffected.
LABELS = frozenset("""
transaction transactions amount date paid to paid card cards category status
reference ref description merchant total subtotal balance payment successful
purchase details detail posted pending receipt summary charged currency
foreign rate fee tax vat tip change cash credit debit visa mastercard
""".split())

# An amount needs either a currency marker or a two-decimal tail. Anything
# with more decimals is a rate, not a price.
_MONEY = re.compile(r"""
    (?P<lead>[€$£]|CA?\$|US\$|\b(?:EUR|CAD|USD)\b)?   # marker before
    \s*
    (?P<sign>[-+−])?                              # ASCII or unicode minus
    \s*
    (?P<digits>\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)
    \s*
    (?P<trail>[€$£]|\b(?:EUR|CAD|USD)\b)?              # marker after
""", re.VERBOSE)

_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
# "@ 1.64321" -- the rate a bank discloses beside a foreign purchase.
_RATE = re.compile(r"@\s*(\d+[.,]\d{3,8})")
# "*4417", "••4417", "xxxx 4417", "ending 4417".
# One mask character is enough: the engine renders the two bullets in
# "••4417" as a single "*", so requiring two found no card at all.
_MASK = re.compile(r"(?:[*x•·∙]+|ending(?:\s+in)?)\s*(\d{4})",
                   re.IGNORECASE)

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}
for _full, _n in list(_MONTHS.items()):
    _MONTHS[_full[:3]] = _n

# OCR eats spaces, so "8September2026" arrives as one token. Both spaced and
# run-together forms have to match.
#
# The trailing guard is (?!\d) rather than \b deliberately. The date comes
# back as "8September2026at14:32", and there is no word boundary between the
# "6" and the "a" -- both are word characters. Written as \b this matched
# nothing, and the first screenshot it was tried on came back with no date.
_D_TEXT = re.compile(
    r"(?<!\d)(\d{1,2})\s*([A-Za-z]{3,9})\.?\s*(\d{4})(?!\d)")
_D_TEXT_FIRST = re.compile(
    r"([A-Za-z]{3,9})\.?\s*(\d{1,2})\s*,?\s*(\d{4})(?!\d)")
_D_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_D_SLASH = re.compile(
    r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})(?!\d)")

# A screenshot of a purchase is not from 1990, and it is not from next year.
OLDEST = 3650          # days back a purchase date may be
FUTURE_GRACE = 1       # days forward, for a timezone straddling midnight


def parse(boxes, today=None):
    """A reading of the purchase on this screenshot.

    Returns a dict that always has the same keys, so the caller never has to
    test for their presence -- only for whether a value is None. `needs` lists
    what a person still has to settle, and is empty only when every field was
    unambiguous.
    """
    today = today or dt.date.today()
    boxes = list(boxes or [])
    joined = " ".join(box.text for box in boxes)

    amounts = _amounts(boxes)
    chosen = amounts[0] if amounts else None
    conversion = _conversion(joined, chosen)
    dates = _dates(boxes, today)
    when = dates[0] if dates else None

    needs = []
    if chosen is None:
        needs.append("amount")
    elif chosen["currency"] is None:
        needs.append("currency")
    if when is None:
        needs.append("date")
    elif when["ambiguous"]:
        needs.append("date")

    return {
        "amount": chosen,
        "amounts": amounts,
        "date": when,
        "dates": dates,
        "merchant": _merchant(boxes, amounts, dates),
        "card_mask": _mask(joined),
        "conversion": conversion,
        "needs": needs,
        "text": joined,
    }


# ------------------------------------------------------------------ amounts

def _amounts(boxes):
    """Every amount-looking token, largest rendered first.

    Ranked by height rather than confidence: a big number the engine was
    unsure of is still far more likely to be the purchase amount than a small
    one it read perfectly, and the confidence travels with it either way.
    """
    found = []
    for box in boxes:
        if _TIME.search(box.text):
            # "14:32" would otherwise offer 14 and 32.
            continue
        for match in _MONEY.finditer(box.text):
            reading = _one_amount(match, box)
            if reading:
                found.append(reading)
    found.sort(key=lambda a: (-a["height"], -a["confidence"], a["top"]))
    return _without_duplicates(found)


def _one_amount(match, box):
    """One regex hit as an amount, or None if it is not one."""
    digits = match.group("digits")
    marker = match.group("lead") or match.group("trail")

    # The qualifying rule. Without a currency marker the token has to look
    # like money on its own, which means a two-decimal tail -- otherwise a
    # year, a card's last four and a reference number all become purchases.
    tail = re.search(r"[.,](\d{1,2})$", digits)
    if not marker and not (tail and len(tail.group(1)) == 2):
        return None
    # A second check for "a run of digits with no separator" stood here and
    # could never fire: anything reaching it either had a marker, or had the
    # two-decimal tail the line above insists on -- and a tail is a separator.
    # It was a guard against a case the previous line had already returned on.

    try:
        minor = money.parse(digits)
    except money.MoneyError:                       # pragma: no cover
        return None
    if not minor:
        return None

    return {
        "minor": abs(minor),
        "currency": _currency_of(marker),
        "marker": marker,
        "text": match.group(0).strip(),
        # The editable form, for prefilling an input. Produced here by
        # money.format rather than by the browser dividing by 100: a second
        # implementation of the same rounding is how a total comes to
        # disagree with its own rows, and the page has a test forbidding it.
        "plain": money.format(abs(minor), symbol=False, grouping=False),
        "negative": bool(match.group("sign")),
        "height": round(box.height, 1),
        "top": round(box.top, 1),
        "confidence": round(box.confidence, 3),
    }


def _currency_of(marker):
    """A currency code from a marker, or None when the marker cannot say.

    "$" returns None on purpose. Guessing it is the same class of mistake as
    guessing a date: right most of the time, and silently wrong on exactly
    the transactions this app exists to get right.
    """
    if not marker:
        return None
    text = marker.strip().upper()
    if text in CODES:
        return text
    return SYMBOLS.get(marker.strip()) or SYMBOLS.get(text)


def _without_duplicates(found):
    """Collapse the same figure read twice, keeping the larger rendering."""
    out, seen = [], set()
    for item in found:
        key = (item["minor"], item["currency"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# -------------------------------------------------------------------- dates

def _dates(boxes, today):
    """Every readable date, most confident first.

    An ambiguous slash date yields one entry flagged `ambiguous` carrying
    both readings, rather than two entries -- the caller has to ask which,
    and offering them as separate candidates would let one be picked by
    accident.
    """
    out, seen = [], set()
    for box in boxes:
        for reading in _dates_in(box.text, today):
            if reading["iso"] in seen:
                continue
            seen.add(reading["iso"])
            out.append(reading)
    out.sort(key=lambda d: (d["ambiguous"], -d["confidence"]))
    return out


def _dates_in(text, today):
    """Readings from one box's text."""
    out = []

    for match in _D_ISO.finditer(text):
        day = _date_or_none(int(match.group(1)), int(match.group(2)),
                            int(match.group(3)), today)
        if day:
            out.append(_reading(day, match.group(0), False, 1.0))

    for pattern, order in ((_D_TEXT, "dmy"), (_D_TEXT_FIRST, "mdy")):
        for match in pattern.finditer(text):
            if order == "dmy":
                dayno, name, year = match.group(1), match.group(2), match.group(3)
            else:
                name, dayno, year = match.group(1), match.group(2), match.group(3)
            month = _MONTHS.get(name.lower()) or _MONTHS.get(name[:3].lower())
            if not month:
                continue
            day = _date_or_none(int(year), month, int(dayno), today)
            if day:
                # A written month cannot be misread as a day. This is the one
                # date format with no ambiguity in it at all.
                out.append(_reading(day, match.group(0), False, 0.95))

    for match in _D_SLASH.finditer(text):
        out.extend(_slash(match, today))
    return out


def _slash(match, today):
    """A d/m/y or m/d/y date, saying so when it cannot tell which."""
    first, second = int(match.group(1)), int(match.group(2))
    year = int(match.group(3))
    year += 2000 if year < 100 else 0

    as_dmy = _date_or_none(year, second, first, today)
    as_mdy = _date_or_none(year, first, second, today)

    if as_dmy and as_mdy and as_dmy != as_mdy:
        # Both are real dates. Nothing in the image settles it, so the reading
        # says so and carries the alternative; the caller must ask.
        reading = _reading(as_dmy, match.group(0), True, 0.5)
        reading["alternative"] = as_mdy.isoformat()
        return [reading]
    only = as_dmy or as_mdy
    return [_reading(only, match.group(0), False, 0.8)] if only else []


def _reading(day, text, ambiguous, confidence):
    return {"iso": day.isoformat(), "text": text, "ambiguous": ambiguous,
            "confidence": confidence, "alternative": None}


def _date_or_none(year, month, dayno, today):
    """A real calendar date inside the window a purchase can fall in."""
    try:
        day = dt.date(year, month, dayno)
    except ValueError:
        return None
    if day > today + dt.timedelta(days=FUTURE_GRACE):
        return None
    if day < today - dt.timedelta(days=OLDEST):
        return None
    return day


# ----------------------------------------------------------------- merchant

def _merchant(boxes, amounts, dates):
    """The shop, or None.

    The tallest line that is not the amount, not a date and not one of the
    words a screen uses to label its own fields. Height again, for the same
    reason: an app puts the merchant's name in the second-largest type on the
    screen.
    """
    amount_texts = {a["text"] for a in amounts}
    date_texts = {d["text"] for d in dates}

    best = None
    for box in boxes:
        text = box.text.strip(" :•-")
        if not text or len(text) < 3:
            continue
        if any(t and t in box.text for t in amount_texts | date_texts):
            continue
        words = re.findall(r"[A-Za-z]+", text.lower())
        if not words:
            continue
        if all(word in LABELS for word in words):
            continue
        # Mostly digits is a reference or a card number, not a name.
        if sum(c.isdigit() for c in text) > len(text) / 2:
            continue
        if best is None or box.height > best.height:
            best = box._replace(text=text)
    return best.text if best else None


# ----------------------------------------------- what the bank already said

def _conversion(joined, chosen):
    """The original amount and rate, when the screenshot discloses them.

    "Foreign currency 52.30 EUR @ 1.64321" is the same disclosure a .qfx
    carries, and it is authoritative: it is the bank stating what it actually
    did, so it beats anything this module infers. Reuses fxcost's extractor
    rather than growing a second one that could disagree with it.

    `chosen` is needed to tell a disclosure from an echo. On a screenshot of a
    plain euro purchase the only amount on screen is "-52,30 EUR", and the
    extractor finds it and reports a foreign original of EUR 52.30 -- which is
    the same figure, restated. Reporting that as a conversion would invite a
    comparison of an amount against itself, and the answer would be a
    confident zero-cost conversion that never happened.
    """
    found = fxcost.foreign_amount(joined)
    if not found:
        return None
    minor, code = found

    rate = None
    match = _RATE.search(joined)
    if match:
        rate = match.group(1).replace(",", ".")

    if (rate is None and chosen
            and minor == chosen["minor"] and code == chosen["currency"]):
        return None
    return {"minor": minor, "currency": code, "rate": rate,
            "plain": money.format(minor, symbol=False, grouping=False)}


def _mask(joined):
    """The card's last four, when the screenshot shows them."""
    match = _MASK.search(joined)
    return match.group(1) if match else None


# ------------------------------------------------------------------ storage

class ReceiptError(ValueError):
    """The upload was not an image this app will keep."""


def kind_of(payload):
    """The file extension implied by the bytes, or None.

    Sniffed rather than taken from the upload's filename. The name is text the
    browser was handed and can say ".png" about anything; these bytes cannot.
    """
    if not payload:
        return None
    for magic, suffix in MAGIC:
        if payload.startswith(magic):
            return suffix
    if (payload.startswith(RIFF_WEBP[0])
            and payload[8:12] == RIFF_WEBP[1]):
        return ".webp"
    return None


def folder(directory=None):
    """The receipts directory, created if it is not there yet."""
    place = os.path.join(paths.data_dir(directory), FOLDER)
    os.makedirs(place, exist_ok=True)
    return place


def store(payload, directory=None):
    """Save a screenshot and return the filename to record against the row.

    The name is a digest of the contents plus the real extension, and never
    anything the browser sent. Two consequences, both wanted:

    - There is no path to traverse. A filename of "../../etc/passwd" cannot
      survive being replaced by a hex digest.
    - The same screenshot uploaded twice is one file. Which is the behaviour
      the imports table already has for statements, for the same reason.
    """
    suffix = kind_of(payload)
    if not suffix:
        raise ReceiptError(
            "that is not an image this app recognises; a PNG, JPEG, GIF, BMP "
            "or WebP screenshot is what it expects")

    name = hashlib.sha256(payload).hexdigest()[:db.DIGEST_CHARS] + suffix
    where = os.path.join(folder(directory), name)
    if not os.path.exists(where):
        with open(where, "wb") as handle:
            handle.write(payload)
    return name


def stored_path(name, directory=None):
    """The absolute path of a stored receipt, or None if the name is not one.

    Every name this app writes is a hex digest and an extension, so anything
    else did not come from `store` -- and rather than sanitising a suspect
    name into a plausible one, this refuses it. A serving route calls this and
    gets None for "../../secrets" instead of a file.
    """
    if not name or not re.fullmatch(r"[0-9a-f]{4,64}\.[a-z]{3,4}", str(name)):
        return None
    where = os.path.join(folder(directory), str(name))
    return where if os.path.isfile(where) else None
