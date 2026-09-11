/* simple.js — photograph a purchase, and see what you have spent.
 *
 * The stripped-back view. One figure and one action: choose an image, it is
 * read on this machine, you glance at what it found, it is recorded. No
 * budgets, importing, cards, subscriptions, goals or trends, and no CAD/USD
 * conversion -- all of that is on the full page.
 *
 * Nothing here has its own copy of anything. esc, el, say, the request
 * wrapper, and the two functions that explain a reading all come from
 * common.js, and it posts to the same /api/receipt/read and
 * /api/receipt/save the full page uses. That is the only kind of second view
 * worth having: one with its own logic would eventually disagree with the
 * first about what a month's spending is.
 *
 * The glance before recording is not ceremony. The reading arrives pre-filled
 * and Record it is one keystroke away, but it is shown, because an OCR misread
 * that books EUR 5,230 instead of EUR 52.30 surfaces a month later in a total
 * nobody can explain -- which is far worse than a picture that could not be
 * read.
 */

'use strict';

/* Enough recent purchases to see the one just added landed and to spot a
 * mistake; few enough that the page stays one screen. */
var RECENT = 12;

var view = { stored: null, hasEngine: true };

function today() {
  /* Local time. toISOString gives UTC, which is yesterday for anyone west of
   * Greenwich in the evening -- and a purchase filed on the wrong day
   * converts at the wrong rate on the full page. */
  var now = new Date();
  return now.getFullYear() + '-' +
    String(now.getMonth() + 1).padStart(2, '0') + '-' +
    String(now.getDate()).padStart(2, '0');
}

/* --------------------------------------------------------------- the total */

function load() {
  return api('/api/overview').then(function (body) {
    el('monthLabel').textContent = body.month;
    el('spentEur').textContent = (body.summary || {}).spent_text || '—';
    drawCategories(body.categories || []);
    return loadRecent();
  }).catch(function (bad) { say(bad.message, 'bad'); });
}


function loadRecent() {
  return api('/api/transactions?limit=' + RECENT).then(function (body) {
    drawRecent(body.transactions || []);
  });
}

function drawRecent(rows) {
  if (!rows.length) {
    el('recentBody').innerHTML =
      '<tr><td colspan="4" class="empty">Nothing recorded yet. ' +
      'Photograph something.</td></tr>';
    el('recentNote').textContent = '';
    return;
  }
  el('recentNote').textContent = 'the last ' + rows.length;
  el('recentBody').innerHTML = rows.map(function (row) {
    /* Everything escaped, including what looks safe: a description can come
     * from a bank file and a category is whatever somebody typed. */
    return '<tr>' +
      '<td class="mono">' + esc(row.spent_on) + '</td>' +
      '<td>' + esc(row.description) +
        (row.category ? ' <span class="tag">' + esc(row.category) +
         '</span>' : '') +
        (row.receipt ? ' <a class="tag" href="/receipt/' +
         encodeURIComponent(row.receipt) + '">image</a>' : '') + '</td>' +
      '<td class="num">' + esc(row.amount_eur_text) + '</td>' +
      '<td class="num"><button class="iconButton" data-delete="' +
        esc(row.id) + '" title="Remove">&times;</button></td>' +
      '</tr>';
  }).join('');
}

/* ------------------------------------------------------------ the picture */

function read(file) {
  if (!file) { return Promise.resolve(); }
  var form = new FormData();
  form.append('file', file);

  el('dropBig').textContent = 'Reading it…';
  say('', '');

  return api('/api/receipt/read', { method: 'POST', body: form })
    .then(function (body) {
      view.stored = body.stored || null;
      view.hasEngine = body.ocr !== false;
      show(body.reading || nothingRead());
      if (!view.hasEngine) { say(body.why, 'warn'); }
      resetDrop();
      return body;
    })
    .catch(function (bad) {
      say(bad.message, 'bad');
      el('reading').hidden = true;
      resetDrop();
    });
}

function nothingRead() {
  /* No reader installed. The picture is still kept and attached to the
   * purchase, which is now typed -- so the page still works, it just does
   * less. */
  return { amount: null, date: null, merchant: null, amounts: [],
           needs: ['amount', 'date'], suggested_category: null,
           conversion: null };
}

function resetDrop() {
  el('dropBig').textContent = 'Choose or drop an image';
  el('shotFile').value = '';
}

function show(reading) {
  el('reading').hidden = false;

  el('amount').value = reading.amount ? reading.amount.plain : '';
  el('date').value = (reading.date && !reading.date.ambiguous)
    ? reading.date.iso : today();
  el('description').value = reading.merchant || '';
  el('category').value = reading.suggested_category || '';

  /* What the bank itself disclosed beats anything read off the screen: a
   * "52.30 EUR @ 1.64321" line is the bank stating what it actually did, so
   * the euro figure comes from there rather than from the billed total. */
  if (reading.conversion && reading.conversion.plain) {
    el('amount').value = reading.conversion.plain;
  }

  showNotes('needs', readingNotes(reading, {
    hasEngine: view.hasEngine, fillsToday: true
  }));
  showCandidates('candidates', reading);

  var image = el('shotImage');
  if (view.stored) {
    image.src = '/receipt/' + encodeURIComponent(view.stored);
    image.hidden = false;
  } else {
    image.hidden = true;
  }

  /* Land the cursor where the attention should go. */
  (reading.amount ? el('description') : el('amount')).focus();
}

/* ------------------------------------------------------------- recording */

function record() {
  return postJson('/api/receipt/save', {
    date: el('date').value,
    description: el('description').value,
    amount: el('amount').value,
    category: el('category').value,
    stored: view.stored
  }).then(function (body) {
    say(body.result === 'duplicate'
      ? 'That looks like one already recorded, so nothing was added.'
      : 'Recorded.', body.result === 'duplicate' ? 'warn' : 'good');
    discard();
    return load();
  }).catch(function (bad) { say(bad.message, 'bad'); });
}

function discard() {
  el('reading').hidden = true;
  view.stored = null;
  resetDrop();
}

/* ---------------------------------------------------------------- wiring */

el('shotFile').addEventListener('change', function () {
  read(this.files && this.files[0]);
});

el('saveForm').addEventListener('submit', function (event) {
  event.preventDefault();
  record();
});

el('discard').addEventListener('click', discard);

el('candidates').addEventListener('click', function (event) {
  var value = event.target.dataset && event.target.dataset.amount;
  if (value) { el('amount').value = value; }
});

el('recentBody').addEventListener('click', function (event) {
  var id = event.target.dataset && event.target.dataset.delete;
  if (!id) { return; }
  send('/api/transaction/' + id, { method: 'DELETE' })
    .then(function () { say('Removed.', ''); return load(); })
    .catch(function (bad) { say(bad.message, 'bad'); });
});

/* Dropping an image on the page, and pasting one. A screenshot is usually
 * already on the clipboard, so paste saves having to put it on disk first. */
['dragenter', 'dragover'].forEach(function (name) {
  el('drop').addEventListener(name, function (event) {
    event.preventDefault();
    el('drop').classList.add('over');
  });
});
['dragleave', 'drop'].forEach(function (name) {
  el('drop').addEventListener(name, function () {
    el('drop').classList.remove('over');
  });
});
el('drop').addEventListener('drop', function (event) {
  event.preventDefault();
  var dropped = event.dataTransfer && event.dataTransfer.files;
  if (dropped && dropped.length) { read(dropped[0]); }
});

window.addEventListener('paste', function (event) {
  var items = (event.clipboardData || {}).items || [];
  for (var i = 0; i < items.length; i += 1) {
    if (items[i].type && items[i].type.indexOf('image/') === 0) {
      read(items[i].getAsFile());
      return;
    }
  }
});

load();
