/* common.js — the four things every page here needs.
 *
 * Extracted when a second page appeared. There are two views of this app now:
 * the full one, and a stripped-back one that is just the figures and a way to
 * add a purchase. Both escape untrusted text, both talk to the same API, both
 * need the session token.
 *
 * Copying these into the second page was the obvious thing and the wrong one.
 * `esc` in particular must exist exactly once: the face-recognition app in
 * this family shipped a stored XSS hole by putting a registered name straight
 * into innerHTML, and with two escapers the one that gets fixed is not
 * reliably the one that gets used. `test_frontend.py` asserts there is one.
 */

'use strict';

/* Everything that reaches the DOM goes through this. Transaction descriptions
 * come out of a bank CSV, which is untrusted input by any reasonable
 * definition. */
function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function el(id) { return document.getElementById(id); }

function say(text, kind) {
  var box = el('notice');
  if (!box) { return; }
  if (!text) { box.hidden = true; return; }
  box.textContent = text;
  box.className = 'notice' + (kind ? ' ' + kind : '');
  box.hidden = false;
}

/* The session token, read once from the meta tag the server rendered.
 * Attached to every state-changing request by send(), so no call site has to
 * remember -- forgetting it at one of nineteen call sites would be a 403
 * somebody debugs for an hour. */
var CSRF = (document.querySelector('meta[name="csrf-token"]') || {})
  .content || '';

var UNSAFE = { POST: 1, PUT: 1, PATCH: 1, DELETE: 1 };

function send(url, options) {
  var settings = options || {};
  var method = (settings.method || 'GET').toUpperCase();
  if (UNSAFE[method]) {
    settings.headers = settings.headers || {};
    settings.headers['X-CSRF-Token'] = CSRF;
  }
  return fetch(url, settings);
}

function api(url, options) {
  return send(url, options).then(function (reply) {
    return reply.json().then(function (body) {
      if (reply.status === 401 && body.login) {
        /* The session went away -- expired, or signed out in another tab.
         * Reloading lands on the login page rather than leaving the screen
         * showing figures that are no longer being refreshed. */
        window.location.reload();
        throw new Error('signed out');
      }
      if (!reply.ok) throw new Error(body.error || ('HTTP ' + reply.status));
      return body;
    });
  });
}

function postJson(url, body) {
  return api(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
}

/* Money arrives already formatted from the server, and stays that way.
 * Formatting it here would be a second implementation of the same rounding,
 * and the two would drift -- which is how a total comes to disagree with the
 * rows above it. There is a test forbidding `toFixed(2)` and `/ 100` in this
 * directory for exactly that reason. */

/* ------------------------------------------- explaining a read screenshot */
/* Both views show a reading before recording it, so both have to say what
 * could not be settled. This lived in each of them, which meant the trickiest
 * prose in the app existed twice -- and a change to how an ambiguous date is
 * explained would have needed making in two places, or would have drifted.
 *
 * The two differ in one respect and it is a parameter, not a copy: the
 * stripped-back view fills today in when no date was read, and the full page
 * leaves the field empty. */

/* Below this the reader is guessing at the glyphs, and a figure it is unsure
 * of should say so rather than sit there looking like a fact. */
var UNSURE_BELOW = 0.7;

function readingNotes(reading, options) {
  var opts = options || {};
  var notes = [];

  if (opts.hasEngine === false) {
    notes.push('No reader installed, so nothing was extracted — the image ' +
               'is kept and you can type the figures.');
  } else if (reading.needs.indexOf('amount') >= 0) {
    notes.push('No amount could be read. Type it in.');
  }

  if (reading.needs.indexOf('currency') >= 0) {
    notes.push('The amount had no currency on it' +
      (reading.amount && reading.amount.marker
        ? ' beyond "' + reading.amount.marker + '", which could be Canadian ' +
          'or US'
        : '') +
      '. It is being treated as euros' +
      (opts.fillsToday ? '.' : ' — change the figure if it was not.'));
  }

  if (reading.date && reading.date.ambiguous) {
    notes.push('The date could be ' + reading.date.iso + ' or ' +
      reading.date.alternative + ' — nothing in the image says which' +
      (opts.fillsToday
        ? ', so today is filled in. Change it if it was neither.'
        : '. Pick one.'));
  } else if (opts.hasEngine !== false &&
             reading.needs.indexOf('date') >= 0) {
    notes.push('No date could be read' +
      (opts.fillsToday ? ', so today is filled in.' : ' — pick one.'));
  }

  if (reading.amount && reading.amount.confidence < UNSURE_BELOW) {
    notes.push('The reader was unsure of that figure (' +
      Math.round(reading.amount.confidence * 100) + '% confident).');
  }
  return notes;
}

/* Text nodes, not markup. These sentences quote what was read off an image,
 * and the whole point of the panel is that the read was untrustworthy -- so
 * markup is the last place to put it. textContent cannot be markup, which is
 * a stronger guarantee than remembering to escape. */
function showNotes(boxId, notes) {
  var box = el(boxId);
  box.innerHTML = '';
  notes.forEach(function (note) {
    var line = document.createElement('p');
    line.textContent = note;
    box.appendChild(line);
  });
  box.hidden = notes.length === 0;
}

/* The other amounts found, offered as buttons. The largest is chosen because
 * that is where the figure goes on a purchase screen, but it is a guess, and
 * saying so is cheaper than being wrong quietly. */
function showCandidates(boxId, reading, noun) {
  var box = el(boxId);
  var others = (reading.amounts || []).slice(1);
  if (!others.length) { box.textContent = ''; return; }
  box.innerHTML = 'Other figures in the ' + esc(noun || 'image') + ': ' +
    others.map(function (item) {
      return '<button type="button" class="linky" data-amount="' +
        esc(item.plain) + '">' + esc(item.text) + '</button>';
    }).join(', ') +
    '. The largest is chosen, because that is where the amount goes on a ' +
    'purchase screen — but it is a guess.';
}


/* The category suggestions behind both pages' inputs. Byte-for-byte identical
 * in each view, which is what duplicate code looks like before anybody
 * notices -- app.js called its parameter `c`, which was the other half of the
 * problem. */
function drawCategories(categories) {
  el('categoryList').innerHTML = categories.map(function (name) {
    return '<option value="' + esc(name) + '"></option>';
  }).join('');
}
