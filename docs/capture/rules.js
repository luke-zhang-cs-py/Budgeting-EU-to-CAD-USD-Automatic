/* rules.js — reading a purchase out of OCR text, in the browser.
 *
 * A deliberate second implementation of receipts.py, and the only one in this
 * project. The static page has no server to ask, so the rules have to exist
 * here too; two implementations of anything drift, so this one is not trusted
 * to stay in step -- it is *checked*.
 *
 * `cases.json` beside this file holds the shared cases. The Python suite
 * asserts receipts.parse produces those answers, and a headless-browser test
 * asserts this file produces the same ones. If either side changes a rule and
 * not the other, a test fails rather than the two quietly disagreeing about
 * what an amount is.
 *
 * The three rules, as in receipts.py:
 *
 *   1. The amount is the *tallest* text. Whoever designed that screen made
 *      the figure large because it is the one you opened it to see. Every
 *      other candidate is kept and offered, because the heuristic can be
 *      wrong.
 *   2. A number is only money if it says so -- a currency marker, or exactly
 *      two decimals. Without that, "2026" is a very large purchase, "14:32"
 *      is two of them, and the exchange rate is a third.
 *   3. Nothing is certain enough to record by itself. Whatever could not be
 *      settled comes back in `needs`, and the page asks.
 */

'use strict';

var RULES = (function () {

  /* "$" is deliberately not resolved. On a CIBC statement it is Canadian and
   * on a US receipt it is not, and nothing in an image settles it. */
  var SYMBOLS = { '€': 'EUR', '$': null, '£': null,
                  'C$': 'CAD', 'CA$': 'CAD', 'US$': 'USD' };
  var CODES = ['EUR', 'CAD', 'USD'];

  /* Words that label a figure rather than name a shop. Matched against the
   * whole box case-folded, so a shop called "Status Coffee" is unaffected. */
  var LABELS = ('transaction transactions amount date paid to paid card ' +
    'cards category status reference ref description merchant total ' +
    'subtotal balance payment successful purchase details detail posted ' +
    'pending receipt summary charged currency foreign rate fee tax vat tip ' +
    'change cash credit debit visa mastercard').split(' ');

  var MONEY = new RegExp(
    '(?:(€|\\$|£|CA?\\$|US\\$|\\b(?:EUR|CAD|USD)\\b))?' +  /* marker before */
    '\\s*([-+\u2212])?\\s*' +                              /* sign */
    '(\\d{1,3}(?:[.,\\s]\\d{3})*(?:[.,]\\d{1,2})?|\\d+(?:[.,]\\d{1,2})?)' +
    '\\s*(€|\\$|£|\\b(?:EUR|CAD|USD)\\b)?', 'g');          /* marker after */

  var TIME = /\b\d{1,2}:\d{2}\b/;
  var MASK = /(?:[*x•·∙]+|ending(?:\s+in)?)\s*(\d{4})/i;

  /* The trailing guard is (?!\d) rather than \b on purpose: OCR eats spaces,
   * so a date arrives as "8September2026at14:32", and there is no word
   * boundary between the "6" and the "a" -- both are word characters. Written
   * as \b this matched nothing at all. */
  var D_TEXT = /(?:^|[^\d])(\d{1,2})\s*([A-Za-z]{3,9})\.?\s*(\d{4})(?!\d)/g;
  var D_TEXT_FIRST = /([A-Za-z]{3,9})\.?\s*(\d{1,2})\s*,?\s*(\d{4})(?!\d)/g;
  var D_ISO = /(?:^|[^\d])(\d{4})-(\d{2})-(\d{2})(?!\d)/g;
  var D_SLASH = /(?:^|[^\d])(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})(?!\d)/g;

  var MONTHS = {};
  ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
   'september', 'october', 'november', 'december'].forEach(function (m, i) {
    MONTHS[m] = i + 1;
    MONTHS[m.slice(0, 3)] = i + 1;
  });

  /* An image of a purchase is not from 1990, and not from next year. */
  var OLDEST_DAYS = 3650;
  var FUTURE_GRACE_DAYS = 1;   /* a timezone straddling midnight */
  var MINOR_UNITS = 2;

  /* ------------------------------------------------------------- money */

  /* Both decimal conventions. "1.234,56" and "1,234.56" are both 123456, and
   * "1,234" is a thousand: the last separator wins, unless the tail is three
   * digits with no other separator, which makes it grouping.
   *
   * The same rule money.py applies, for the same reason -- a German export
   * and an Irish one disagree about which mark is which. */
  function parseMoney(text) {
    var cleaned = String(text).replace(/[^\d.,]/g, '');
    if (!/\d/.test(cleaned)) { return null; }

    var lastDot = cleaned.lastIndexOf('.');
    var lastComma = cleaned.lastIndexOf(',');
    var cut = Math.max(lastDot, lastComma);

    var whole = cleaned, fraction = '';
    if (cut >= 0) {
      var tail = cleaned.slice(cut + 1);
      if (tail.length === 3 && cleaned.indexOf('.') === cleaned.lastIndexOf('.')
          && cleaned.indexOf(',') === cleaned.lastIndexOf(',')
          && (lastDot < 0 || lastComma < 0)) {
        /* Three digits and only one separator in the whole string: grouping,
         * so "1,234" is a thousand rather than 1.234. */
        whole = cleaned.replace(/[.,]/g, '');
      } else {
        whole = cleaned.slice(0, cut).replace(/[.,]/g, '');
        fraction = tail;
      }
    }
    if (fraction.length > MINOR_UNITS) { return null; }
    var minor = parseInt(whole || '0', 10) * Math.pow(10, MINOR_UNITS) +
      parseInt((fraction + '00').slice(0, MINOR_UNITS), 10);
    return minor || null;
  }

  function plain(minor) {
    var whole = Math.floor(Math.abs(minor) / Math.pow(10, MINOR_UNITS));
    var rest = Math.abs(minor) % Math.pow(10, MINOR_UNITS);
    return whole + '.' + (rest < 10 ? '0' : '') + rest;
  }

  function currencyOf(marker) {
    if (!marker) { return null; }
    var text = marker.trim().toUpperCase();
    if (CODES.indexOf(text) >= 0) { return text; }
    var found = SYMBOLS[marker.trim()];
    return found === undefined ? (SYMBOLS[text] || null) : found;
  }

  function amountsIn(boxes) {
    var found = [];
    boxes.forEach(function (box) {
      /* "14:32" would otherwise offer 14 and 32. */
      if (TIME.test(box.text)) { return; }
      MONEY.lastIndex = 0;
      var hit;
      while ((hit = MONEY.exec(box.text)) !== null) {
        if (!hit[0].trim()) { MONEY.lastIndex += 1; continue; }
        var one = oneAmount(hit, box);
        if (one) { found.push(one); }
      }
    });
    found.sort(function (a, b) {
      return (b.height - a.height) || (b.confidence - a.confidence) ||
        (a.top - b.top);
    });
    /* The same figure read twice keeps the larger rendering. */
    var seen = {}, out = [];
    found.forEach(function (item) {
      var key = item.minor + '/' + item.currency;
      if (seen[key]) { return; }
      seen[key] = true;
      out.push(item);
    });
    return out;
  }

  function oneAmount(hit, box) {
    var marker = hit[1] || hit[4];
    var digits = hit[3];

    /* Rule 2. Without a marker the token has to look like money on its own,
     * which means a two-decimal tail -- otherwise a year, a card's last four
     * and a reference number all become purchases. */
    var tail = /[.,](\d{1,2})$/.exec(digits);
    if (!marker && !(tail && tail[1].length === MINOR_UNITS)) { return null; }

    var minor = parseMoney(digits);
    if (!minor) { return null; }

    return {
      minor: Math.abs(minor),
      plain: plain(Math.abs(minor)),
      currency: currencyOf(marker),
      marker: marker || null,
      text: hit[0].trim(),
      negative: Boolean(hit[2]),
      height: Math.round(box.height * 10) / 10,
      top: Math.round(box.top * 10) / 10,
      confidence: Math.round(box.confidence * 1000) / 1000
    };
  }

  /* -------------------------------------------------------------- dates */

  function iso(year, month, day) {
    return year + '-' + String(month).padStart(2, '0') + '-' +
      String(day).padStart(2, '0');
  }

  function realDate(year, month, day, today) {
    if (month < 1 || month > 12 || day < 1 || day > 31) { return null; }
    var made = new Date(Date.UTC(year, month - 1, day));
    if (made.getUTCFullYear() !== year || made.getUTCMonth() !== month - 1 ||
        made.getUTCDate() !== day) {
      return null;                       /* 31 February and friends */
    }
    var now = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(),
                       today.getUTCDate());
    var day_ms = 86400000;
    if (made > now + FUTURE_GRACE_DAYS * day_ms) { return null; }
    if (made < now - OLDEST_DAYS * day_ms) { return null; }
    return iso(year, month, day);
  }

  function reading(value, text, ambiguous, confidence, alternative) {
    return { iso: value, text: text, ambiguous: ambiguous,
             confidence: confidence, alternative: alternative || null };
  }

  function datesIn(boxes, today) {
    var out = [], seen = {};

    boxes.forEach(function (box) {
      datesInText(box.text, today).forEach(function (one) {
        if (seen[one.iso]) { return; }
        seen[one.iso] = true;
        out.push(one);
      });
    });
    out.sort(function (a, b) {
      return (a.ambiguous - b.ambiguous) || (b.confidence - a.confidence);
    });
    return out;
  }

  function datesInText(text, today) {
    var out = [], hit;

    D_ISO.lastIndex = 0;
    while ((hit = D_ISO.exec(text)) !== null) {
      var found = realDate(+hit[1], +hit[2], +hit[3], today);
      if (found) { out.push(reading(found, hit[0].trim(), false, 1.0)); }
    }

    [[D_TEXT, 'dmy'], [D_TEXT_FIRST, 'mdy']].forEach(function (pair) {
      var pattern = pair[0];
      pattern.lastIndex = 0;
      var match;
      while ((match = pattern.exec(text)) !== null) {
        var dayText = pair[1] === 'dmy' ? match[1] : match[2];
        var nameText = pair[1] === 'dmy' ? match[2] : match[1];
        var month = MONTHS[nameText.toLowerCase()] ||
          MONTHS[nameText.slice(0, 3).toLowerCase()];
        if (!month) { continue; }
        var made = realDate(+match[3], month, +dayText, today);
        /* A written month cannot be misread as a day. This is the one date
         * format with no ambiguity in it at all. */
        if (made) { out.push(reading(made, match[0].trim(), false, 0.95)); }
      }
    });

    D_SLASH.lastIndex = 0;
    while ((hit = D_SLASH.exec(text)) !== null) {
      out = out.concat(slashDate(hit, today));
    }
    return out;
  }

  function slashDate(hit, today) {
    var first = +hit[1], second = +hit[2], year = +hit[3];
    if (year < 100) { year += 2000; }

    var asDmy = realDate(year, second, first, today);
    var asMdy = realDate(year, first, second, today);

    if (asDmy && asMdy && asDmy !== asMdy) {
      /* Both are real dates and nothing in the image settles it, so the
       * reading says so and carries the other one. */
      return [reading(asDmy, hit[0].trim(), true, 0.5, asMdy)];
    }
    var only = asDmy || asMdy;
    return only ? [reading(only, hit[0].trim(), false, 0.8)] : [];
  }

  /* ----------------------------------------------------------- merchant */

  function merchantIn(boxes, amounts, dates) {
    var taken = {};
    amounts.forEach(function (a) { taken[a.text] = true; });
    dates.forEach(function (d) { taken[d.text] = true; });

    var best = null;
    boxes.forEach(function (box) {
      var text = box.text.replace(/^[\s:•-]+|[\s:•-]+$/g, '');
      if (text.length < 3) { return; }
      if (Object.keys(taken).some(function (t) {
        return t && box.text.indexOf(t) >= 0;
      })) { return; }

      var words = (text.toLowerCase().match(/[a-z]+/g) || []);
      if (!words.length) { return; }
      if (words.every(function (w) { return LABELS.indexOf(w) >= 0; })) {
        return;
      }
      /* Mostly digits is a reference or a card number, not a name. */
      var digits = (text.match(/\d/g) || []).length;
      if (digits > text.length / 2) { return; }

      if (!best || box.height > best.height) {
        best = { height: box.height, text: text };
      }
    });
    return best ? best.text : null;
  }

  /* -------------------------------------------------------------- parse */

  function parse(boxes, today) {
    var when = today || new Date();
    var list = boxes || [];
    var joined = list.map(function (b) { return b.text; }).join(' ');

    var amounts = amountsIn(list);
    var chosen = amounts.length ? amounts[0] : null;
    var dates = datesIn(list, when);
    var date = dates.length ? dates[0] : null;

    var needs = [];
    if (!chosen) {
      needs.push('amount');
    } else if (chosen.currency === null) {
      needs.push('currency');
    }
    if (!date || date.ambiguous) { needs.push('date'); }

    var mask = MASK.exec(joined);
    return {
      amount: chosen,
      amounts: amounts,
      date: date,
      dates: dates,
      merchant: merchantIn(list, amounts, dates),
      card_mask: mask ? mask[1] : null,
      needs: needs,
      text: joined
    };
  }

  /* ---------------------------------------------------------------- csv */

  /* Exactly the four column names the wallet importer recognises, so the
   * file needs no column mapping at the far end. Expenses are negative,
   * because the far end reads a negative as money out.
   *
   * This lives here rather than in app.js for one reason: it is the only
   * thing this page hands to the other program, and a test can check that
   * hand-off only if the text can be produced without a DOM. It can, so
   * tests/test_shared_rules.py builds a file with this function in a real
   * browser and reads it back with the wallet's own importer. */
  var COLUMNS = ['Date', 'Description', 'Amount', 'Currency'];

  function field(value) {
    /* Quoted only when it has to be, and a quote inside is doubled -- the
     * one rule every CSV reader agrees on. */
    return /[",\n]/.test(value)
      ? '"' + String(value).replace(/"/g, '""') + '"' : String(value);
  }

  function csv(rows) {
    var lines = [COLUMNS.join(',')];
    (rows || []).forEach(function (row) {
      var what = row.description + (row.category ? ' - ' + row.category : '');
      lines.push([row.date, field(what), '-' + plain(row.minor), 'EUR']
                 .join(','));
    });
    return lines.join('\n') + '\n';
  }

  return { parse: parse, parseMoney: parseMoney, plain: plain,
           csv: csv, COLUMNS: COLUMNS };
}());

if (typeof module !== 'undefined') { module.exports = RULES; }
