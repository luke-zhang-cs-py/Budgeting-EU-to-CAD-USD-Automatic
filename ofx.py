"""
ofx.py
------
Reading the .qfx / .ofx files CIBC actually offers.

There is no way to sync with CIBC programmatically. Three separate things had
to be checked before accepting that:

  * **No open-banking API.** The Consumer-Driven Banking Act received Royal
    Assent in March 2026 and CIBC is a mandatory participant, but Phase 1 read
    access has no operational date.
  * **No OFX Direct Connect.** CIBC supports Web Connect only -- a manual
    download. Direct Connect is supported by very few Canadian banks, and not
    by this one.
  * **No aggregator worth using.** The ones that reach Canadian banks either
    need a commercial agreement or log into your online banking on your
    behalf, which breaches CIBC's own agreement and can void your
    fraud-liability protection.

So the file is the interface. But the *right* file is not the CSV: CIBC also
exports OFX, and OFX carries three things its CSV throws away.

    <FITID>          the bank's own unique id for the transaction
    <CURSYM>         the currency the purchase was actually made in
    <CURRATE>        the exact rate the bank converted at

The first makes duplicate detection authoritative instead of heuristic -- two
identical coffees on one day are genuinely two transactions to the bank, and
it says so. The other two make the conversion cost exact: no scraping "52.30
EUR" out of a description and hoping, and the bank's own rate is right there
to hold against the ECB's.

The format is SGML in version 1 and XML in version 2, and banks are loose
about closing tags in the SGML dialect. This reads both without a schema, by
pulling the leaf values out of each STMTTRN block, because a strict parser
fails on real files and the useful behaviour is to read what is there.
"""
import datetime as dt
import re
from decimal import Decimal, InvalidOperation

import money

# Enough of a marker to recognise the format without trusting the extension:
# CIBC names its download .qfx, others .ofx, and a synced folder renames
# things.
MARKERS = ("<OFX>", "OFXHEADER")

# One transaction. Non-greedy so consecutive blocks do not merge, and
# DOTALL because the blocks span lines.
_BLOCK = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.IGNORECASE | re.DOTALL)

# A leaf value. In the SGML dialect the closing tag is usually absent, so the
# value runs to the next tag or the end of the line -- which is why this reads
# up to "<" rather than to a matching close.
_LEAF = re.compile(r"<([A-Z0-9.]+)>([^<\r\n]*)", re.IGNORECASE)

# The currency block sits inside the transaction, so its CURSYM would
# otherwise be read as the transaction's own field.
_CURRENCY_BLOCK = re.compile(
    r"<(ORIGCURRENCY|CURRENCY)>(.*?)(?=</\1>|<STMTTRN>|$)",
    re.IGNORECASE | re.DOTALL)


class OfxProblem(Exception):
    """The file is not OFX, or holds no transactions."""


def looks_like_ofx(text):
    """Recognised by content, not by file extension."""
    head = text[:4096].upper()
    return any(marker in head for marker in MARKERS)


def _leaves(chunk):
    """{tag: value} for one block, first occurrence winning.

    First wins because the currency sub-block is stripped before this runs;
    anything still repeating is a bank writing a field twice, and the first is
    the one the specification puts there.
    """
    out = {}
    for tag, value in _LEAF.findall(chunk):
        key = tag.upper()
        if key not in out:
            out[key] = value.strip()
    return out


def _date(text):
    """A DTPOSTED as a date.

    The specification is YYYYMMDD followed by optional time and an optional
    bracketed timezone -- "20260902120000.000[-5:EST]". Only the day is kept:
    the ledger stores days, and a time component would make two purchases on
    one date look like different dates to everything downstream.
    """
    digits = re.match(r"\s*(\d{8})", text or "")
    if not digits:
        raise ValueError(f"unreadable DTPOSTED: {text!r}")
    return dt.datetime.strptime(digits.group(1), "%Y%m%d").date()


def _rate(text):
    """A CURRATE, or None.

    Zero is treated as absent rather than as a rate: some banks emit
    <CURRATE>0</CURRATE> on domestic rows, and dividing by it later would
    raise where the honest answer is "not a foreign transaction".
    """
    try:
        value = Decimal((text or "").strip())
    except InvalidOperation:
        return None
    return value if value > 0 else None


def _currency(chunk):
    """(symbol, rate) from the transaction's currency block, or (None, None).

    ORIGCURRENCY means "this is what the purchase was made in, and here is
    the rate to the account's currency". CURRENCY means the amount is already
    in that currency. Only the first tells you a conversion happened, so a
    plain CURRENCY matching the account is not treated as foreign.
    """
    for match in _CURRENCY_BLOCK.finditer(chunk):
        inner = _leaves(match.group(2))
        symbol = (inner.get("CURSYM") or "").upper() or None
        if symbol:
            return symbol, _rate(inner.get("CURRATE"))
    return None, None


def _describe(name, memo):
    """One description from NAME and MEMO.

    Banks fill either or both, and CIBC leans on MEMO -- often repeating NAME
    as its prefix, so joining them blindly gives "REWE SAGT DANKE REWE SAGT
    DANKE 4821 BERLIN". When one contains the other the longer one is the
    whole story; otherwise they are genuinely different halves and both are
    kept, because the merchant is sometimes only in one of them.
    """
    name, memo = (name or "").strip(), (memo or "").strip()
    if not memo:
        return name
    if not name:
        return memo
    if name.upper() in memo.upper():
        return memo
    if memo.upper() in name.upper():
        return name
    return f"{name} {memo}"


def transactions(text):
    """Every STMTTRN in the file, as dicts. Raises if there are none.

    Each carries what the file said rather than an interpretation of it:
    `amount` signed as the bank signed it, `fitid` as given, and the original
    currency and rate when the bank disclosed a conversion.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", errors="replace")
    if not looks_like_ofx(text):
        raise OfxProblem("this does not look like an OFX or QFX file")

    found = []
    for number, block in enumerate(_BLOCK.findall(text), start=1):
        symbol, rate = _currency(block)
        # Stripped so the sub-block's own tags cannot be mistaken for the
        # transaction's -- CURSYM sitting inside ORIGCURRENCY is the one that
        # matters, and reading it as a transaction field would be silent.
        flat = _CURRENCY_BLOCK.sub("", block)
        leaves = _leaves(flat)

        try:
            on = _date(leaves.get("DTPOSTED"))
            amount = money.parse(leaves.get("TRNAMT"))
        except (ValueError, money.MoneyError) as bad:
            found.append({"block": number, "problem": str(bad)})
            continue

        description = _describe(leaves.get("NAME", ""), leaves.get("MEMO", ""))

        found.append({
            "block": number,
            "problem": None,
            "spent_on": on.isoformat(),
            "description": description,
            "amount": amount,
            "type": leaves.get("TRNTYPE", "").upper(),
            "fitid": leaves.get("FITID") or None,
            "origin_currency": symbol,
            "origin_rate": rate,
        })

    if not found:
        raise OfxProblem("no transactions in the file")
    return found


def account_currency(text):
    """The currency the account itself is denominated in, or None.

    CIBC bills a Canadian card in CAD, so this is what a purchase was
    converted *into* -- the other half of the ORIGCURRENCY pair, and the
    thing the importer would otherwise have to be told.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", errors="replace")
    match = re.search(r"<CURDEF>\s*([A-Z]{3})", text, re.IGNORECASE)
    return match.group(1).upper() if match else None
