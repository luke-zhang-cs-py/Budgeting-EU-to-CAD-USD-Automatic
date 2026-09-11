/* app.js — the static capture page.
 *
 * Photograph a purchase, it is read here in the browser, it is recorded here
 * in the browser. There is no server: this is a file on GitHub Pages, so
 * there is nothing for it to talk to and nowhere for the image to go.
 *
 * That is the point and also the limitation, and both are said out loud on
 * the page. It cannot see the wallet's ledger, so what it records lives in
 * this browser's localStorage until you export it. The export is the four
 * column names the wallet importer already recognises, so it goes straight
 * in -- the same contract the Tally app in this family uses.
 *
 * The reading rules are in rules.js, which is a second implementation of
 * receipts.py and is checked against the same fixture by the Python suite.
 * See the note at the top of that file.
 */

'use strict';

var KEY = 'wallet.capture.v1';

/* The reader. Loaded from a CDN on first use rather than with the page: it is
 * a couple of megabytes of wasm and a language model, and somebody opening
 * this to look at their total should not pay for that. */
var TESSERACT_URL = 'https://cdn.jsdelivr.net/npm/tesseract.js@5/dist/tesseract.min.js';

/* Tesseract reports confidence out of 100; rules.js works in 0..1, as the
 * server-side engine does. */
var CONFIDENCE_SCALE = 100;

var state = { stored: null, image: null, loading: null };

/* ------------------------------------------------------------- plumbing */

function el(id) { return document.getElementById(id); }

/* Everything that reaches the DOM goes through this. A description is typed
 * by a person and read back out as HTML; the face-recognition app in this
 * family shipped a stored XSS hole doing exactly that. */
function esc(value) {
  if (value === null || value === undefined) { return ''; }
  return String(value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function say(text, kind) {
  var box = el('notice');
  if (!text) { box.hidden = true; return; }
  box.textContent = text;
  box.className = 'notice' + (kind ? ' ' + kind : '');
  box.hidden = false;
}

function today() {
  /* Local time, not toISOString: UTC is yesterday for anyone west of
   * Greenwich in the evening, and a purchase filed on the wrong day converts
   * at the wrong rate once it reaches the wallet. */
  var now = new Date();
  return now.getFullYear() + '-' +
    String(now.getMonth() + 1).padStart(2, '0') + '-' +
    String(now.getDate()).padStart(2, '0');
}

/* ------------------------------------------------------------- the store */

function all() {
  try {
    var raw = window.localStorage.getItem(KEY);
    return raw ? JSON.parse(raw) : [];
  } catch (whatever) {
    /* Private mode, or a corrupt value. An empty list loses less than a page
     * that will not load. */
    return [];
  }
}

function put(rows) {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(rows));
  } catch (full) {
    say('This browser would not save it — private mode, or out of space. ' +
        'Export what you have.', 'bad');
  }
}

/* Money is an integer number of cents here too. 0.1 + 0.2 is
 * 0.30000000000000004, so a float total disagrees with the rows above it. */
function total(rows) {
  return rows.reduce(function (sum, row) { return sum + row.minor; }, 0);
}

function euros(minor) {
  return '€' + RULES.plain(minor);
}

function thisMonth() {
  return today().slice(0, 7);
}

/* -------------------------------------------------------------- drawing */

function draw() {
  var rows = all();
  var month = thisMonth();
  var mine = rows.filter(function (row) {
    return row.date.slice(0, 7) === month;
  });

  el('monthLabel').textContent = month;
  el('spent').textContent = euros(total(mine));
  el('count').textContent = rows.length + (rows.length === 1 ? ' purchase'
                                                             : ' purchases');
  el('recentNote').textContent = rows.length
    ? rows.length + ' here, ' + mine.length + ' this month' : '';

  var seen = {};
  rows.forEach(function (row) {
    if (row.category) { seen[row.category] = true; }
  });
  el('categories').innerHTML = Object.keys(seen).map(function (name) {
    return '<option value="' + esc(name) + '"></option>';
  }).join('');

  if (!rows.length) {
    el('entries').innerHTML = '<li class="empty">Nothing yet. Photograph ' +
      'something.</li>';
  } else {
    el('entries').innerHTML = rows.slice().reverse().map(function (row) {
      return '<li><span class="what"><b>' + esc(row.description) + '</b>' +
        '<span class="when">' + esc(row.date) +
        (row.category ? ' · ' + esc(row.category) : '') + '</span></span>' +
        '<span class="much">' + esc(euros(row.minor)) + '</span>' +
        '<button class="iconButton" data-remove="' + esc(row.id) +
        '" title="Remove">&times;</button></li>';
    }).join('');
  }

  el('storageNote').textContent =
    'In this browser only — ' + rows.length + ' purchase(s) in localStorage ' +
    'under "' + KEY + '". It is not synced, not backed up, and not visible ' +
    'to the wallet app: export the CSV and import it there, which is what ' +
    'the four column names are for.';
}

/* --------------------------------------------------------- the reader */

function loadReader() {
  /* Loaded once, on the first image. Returns the same promise afterwards so
   * two quick uploads do not fetch two copies. */
  if (state.loading) { return state.loading; }
  state.loading = new Promise(function (resolve, reject) {
    var tag = document.createElement('script');
    tag.src = TESSERACT_URL;
    tag.onload = function () { resolve(window.Tesseract); };
    tag.onerror = function () {
      state.loading = null;
      reject(new Error('Could not load the reader. It comes from a CDN, so ' +
                       'this needs a connection the first time.'));
    };
    document.head.appendChild(tag);
  });
  return state.loading;
}

function progress(fraction, note) {
  el('progress').hidden = false;
  el('bar').style.width = Math.round(fraction * 100) + '%';
  el('progressNote').textContent = note;
}

/* Tesseract's lines, as the boxes rules.js expects.
 *
 * Lines rather than words, because the whole approach rests on the *rendered
 * height* of a figure, and a word box gives that just as well as a line does
 * while splitting "-52,30 EUR" into pieces that no longer look like money. */
function boxesFrom(data) {
  var lines = (data && data.lines) || [];
  return lines.map(function (line) {
    var box = line.bbox || {};
    return {
      text: String(line.text || '').replace(/\s+/g, ' ').trim(),
      left: box.x0 || 0,
      top: box.y0 || 0,
      width: (box.x1 || 0) - (box.x0 || 0),
      height: (box.y1 || 0) - (box.y0 || 0),
      confidence: (line.confidence || 0) / CONFIDENCE_SCALE
    };
  }).filter(function (box) { return box.text.length > 0; });
}

function read(file) {
  if (!file) { return; }
  say('', '');
  state.image = URL.createObjectURL(file);
  el('dropBig').textContent = 'Reading it…';
  progress(0.05, 'fetching the reader…');

  loadReader().then(function (Tesseract) {
    progress(0.2, 'reading the image…');
    return Tesseract.recognize(file, 'eng', {
      logger: function (message) {
        if (message.status === 'recognizing text') {
          progress(0.2 + message.progress * 0.8, 'reading the image…');
        }
      }
    });
  }).then(function (result) {
    el('progress').hidden = true;
    el('dropBig').textContent = 'Choose or drop an image';
    el('file').value = '';

    var boxes = boxesFrom(result.data);
    if (!boxes.length) {
      say('No text could be found in that image. Try a clearer photograph, ' +
          'or a screenshot rather than a photo of a screen.', 'warn');
      return;
    }
    show(RULES.parse(boxes, new Date()));
  }).catch(function (bad) {
    el('progress').hidden = true;
    el('dropBig').textContent = 'Choose or drop an image';
    el('file').value = '';
    say(bad.message, 'bad');
  });
}

/* --------------------------------------------------------- the reading */

function show(reading) {
  el('reading').hidden = false;

  el('amount').value = reading.amount ? reading.amount.plain : '';
  el('date').value = (reading.date && !reading.date.ambiguous)
    ? reading.date.iso : today();
  el('description').value = reading.merchant || '';
  el('category').value = '';

  drawNeeds(reading);
  drawCandidates(reading);

  var image = el('shot');
  if (state.image) {
    image.src = state.image;
    image.hidden = false;
  } else {
    image.hidden = true;
  }
  (reading.amount ? el('description') : el('amount')).focus();
}

/* Text nodes, not markup. These sentences quote what was read off an image,
 * and the whole point of the panel is that the read was untrustworthy. */
function drawNeeds(reading) {
  var notes = [];
  if (reading.needs.indexOf('amount') >= 0) {
    notes.push('No amount could be read. Type it in.');
  }
  if (reading.needs.indexOf('currency') >= 0) {
    notes.push('The amount had no currency on it' +
      (reading.amount && reading.amount.marker
        ? ' beyond "' + reading.amount.marker + '", which could be Canadian ' +
          'or US' : '') +
      '. It is being treated as euros.');
  }
  if (reading.date && reading.date.ambiguous) {
    notes.push('The date could be ' + reading.date.iso + ' or ' +
      reading.date.alternative + ' — nothing in the image says which, so ' +
      'today is filled in. Change it if it was neither.');
  } else if (reading.needs.indexOf('date') >= 0) {
    notes.push('No date could be read, so today is filled in.');
  }
  if (reading.amount && reading.amount.confidence < 0.7) {
    notes.push('The reader was unsure of that figure (' +
      Math.round(reading.amount.confidence * 100) + '% confident).');
  }

  var box = el('needs');
  box.innerHTML = '';
  notes.forEach(function (note) {
    var line = document.createElement('p');
    line.textContent = note;
    box.appendChild(line);
  });
  box.hidden = notes.length === 0;
}

function drawCandidates(reading) {
  var others = (reading.amounts || []).slice(1);
  if (!others.length) { el('candidates').textContent = ''; return; }
  el('candidates').innerHTML = 'Other figures in the image: ' +
    others.map(function (item) {
      return '<button type="button" class="linky" data-amount="' +
        esc(item.plain) + '">' + esc(item.text) + '</button>';
    }).join(', ') +
    '. The largest is chosen, because that is where the amount goes on a ' +
    'purchase screen — but it is a guess.';
}

function discard() {
  el('reading').hidden = true;
  if (state.image) { URL.revokeObjectURL(state.image); }
  state.image = null;
  el('file').value = '';
}

function record() {
  var minor = RULES.parseMoney(el('amount').value);
  if (!minor) {
    say('That amount could not be read. Both 3.50 and 3,50 work.', 'bad');
    return;
  }
  var description = el('description').value.trim();
  if (!description) { say('It needs a description.', 'bad'); return; }

  var rows = all();
  rows.push({
    id: String(Date.now()) + Math.random().toString(36).slice(2, 7),
    date: el('date').value || today(),
    description: description,
    category: el('category').value.trim(),
    minor: minor
  });
  rows.sort(function (a, b) { return a.date < b.date ? -1 : 1; });
  put(rows);

  say('Recorded. ' + euros(minor) + ' — export the CSV when you want it in ' +
      'the wallet.', 'good');
  discard();
  draw();
}

/* ------------------------------------------------------------- the file */

/* The file itself is written by RULES.csv, next to the parsing rules, so the
 * one thing this page hands to the wallet can be tested without a DOM --
 * tests/test_shared_rules.py builds a file with it and imports it. */

function download() {
  var rows = all();
  if (!rows.length) { say('Nothing to export yet.', ''); return; }
  var blob = new Blob([RULES.csv(rows)], { type: 'text/csv;charset=utf-8' });
  var url = URL.createObjectURL(blob);
  var link = document.createElement('a');
  link.href = url;
  link.download = 'capture-' + rows[0].date + '-to-' +
    rows[rows.length - 1].date + '.csv';
  link.click();
  URL.revokeObjectURL(url);
  say(rows.length + ' purchase(s) exported. Import it in the wallet app — ' +
      'the columns already match.', 'good');
}

/* ---------------------------------------------------------------- wiring */

el('file').addEventListener('change', function () {
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

el('entries').addEventListener('click', function (event) {
  var id = event.target.dataset && event.target.dataset.remove;
  if (!id) { return; }
  put(all().filter(function (row) { return row.id !== id; }));
  say('Removed.', '');
  draw();
});

[el('export'), el('export2')].forEach(function (button) {
  button.addEventListener('click', download);
});

el('clear').addEventListener('click', function () {
  if (!all().length) { say('There is nothing to delete.', ''); return; }
  /* Confirmed, because it cannot be undone and there is no copy anywhere
   * else -- that is the whole nature of a browser-only store. */
  if (!window.confirm('Delete every purchase recorded here? There is no ' +
                      'copy anywhere else unless you exported it.')) {
    return;
  }
  put([]);
  say('Deleted.', '');
  draw();
});

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

draw();
