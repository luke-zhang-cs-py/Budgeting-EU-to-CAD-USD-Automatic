/* finance.js — screenshots, live rates, cards, subscriptions, goals, movers.
 *
 * Loaded after app.js and deliberately reuses its esc(), el(), api(),
 * postJson() and say(). A second copy of those would drift, and the escaper
 * is the one function in this family that must not: the face-recognition app
 * shipped a stored XSS hole by putting a typed name into innerHTML, so
 * everything that reaches the DOM here goes through the same esc().
 *
 * The rule the screenshot flow is built around: **nothing is recorded until
 * a person has seen it.** The reader fills a form; the form is what saves.
 * An OCR misread that books EUR 5230 instead of EUR 52.30 is discovered a
 * month later in a total nobody can explain, which is a far worse failure
 * than a screenshot that could not be read at all.
 */

'use strict';

var fin = {
  cards: [],
  currencies: ['EUR', 'CAD', 'USD'],
  stored: null,          /* the receipt filename the server kept */
  openCategory: null
};

/* --------------------------------------------------------------- helpers */

function options(values, chosen, blank) {
  var out = blank ? '<option value="">' + esc(blank) + '</option>' : '';
  return out + values.map(function (item) {
    var value = item.value === undefined ? item : item.value;
    var label = item.label === undefined ? item : item.label;
    return '<option value="' + esc(value) + '"' +
      (String(value) === String(chosen) ? ' selected' : '') + '>' +
      esc(label) + '</option>';
  }).join('');
}

function cardOptions(chosen) {
  return options(fin.cards.map(function (card) {
    return { value: card.id, label: card.label };
  }), chosen, 'none');
}

/* ------------------------------------------------------- snap a purchase */

function readScreenshot(file) {
  var form = new FormData();
  form.append('file', file);
  say('Reading the screenshot…', '');

  return api('/api/receipt/read', { method: 'POST', body: form })
    .then(function (body) {
      fin.stored = body.stored || null;
      if (!body.ocr) {
        /* No engine installed. The image is still kept and attached to the
         * purchase, which is now typed by hand. */
        showReading({ amount: null, date: null, merchant: null, amounts: [],
                      needs: ['amount', 'date'], card: null,
                      suggested_category: null });
        say(body.why, 'warn');
        return body;
      }
      showReading(body.reading);
      say(body.reading.needs.length
        ? 'Read it — check the highlighted fields before saving.'
        : 'Read it. Check the figures and save.', 'good');
      return body;
    })
    .catch(function (bad) {
      say(bad.message, 'bad');
      el('shotReading').hidden = true;
    });
}

function showReading(reading) {
  el('shotReading').hidden = false;

  el('shotAmount').value = reading.amount ? reading.amount.plain : '';
  el('shotDate').value = reading.date ? reading.date.iso : '';
  el('shotDescription').value = reading.merchant || '';
  el('shotCategory').value = reading.suggested_category || '';
  el('shotCard').innerHTML = cardOptions(reading.card ? reading.card.id : '');
  el('shotCharged').value = '';

  /* What the bank itself disclosed beats anything read off the screen: a
   * "52.30 EUR @ 1.64321" line is the bank stating what it actually did. */
  if (reading.conversion && reading.amount) {
    el('shotAmount').value = reading.conversion.plain;
    el('shotCharged').value = reading.amount.plain;
  }

  showNotes('shotNeeds', readingNotes(reading, { fillsToday: false }));
  showCandidates('shotCandidates', reading, 'screenshot');

  var image = el('shotImage');
  if (fin.stored) {
    image.src = '/receipt/' + encodeURIComponent(fin.stored);
    image.hidden = false;
  } else {
    image.hidden = true;
  }
}

/* Saying what could not be settled, and offering the other figures found,
 * both live in common.js now. Each existed here and again in simple.js --
 * the trickiest prose in the app, written twice, where a change to how an
 * ambiguous date is explained would have had to be made in both or would
 * have drifted.
 *
 * `fillsToday: false` is the one difference between the two views: this one
 * leaves the date field empty for you to pick, and the stripped-back one
 * fills today in. */

function saveScreenshot() {
  return postJson('/api/receipt/save', {
    date: el('shotDate').value,
    description: el('shotDescription').value,
    amount: el('shotAmount').value,
    category: el('shotCategory').value,
    card_id: el('shotCard').value || null,
    charged: el('shotCharged').value,
    charged_currency: chargedCurrency(),
    stored: fin.stored
  }).then(function (body) {
    say(body.result === 'duplicate'
      ? 'Already recorded — nothing added.'
      : 'Saved.', body.result === 'duplicate' ? 'warn' : 'good');
    clearReading();
    refresh();
    loadFinance();
  }).catch(function (bad) { say(bad.message, 'bad'); });
}

function chargedCurrency() {
  var card = fin.cards.filter(function (item) {
    return String(item.id) === String(el('shotCard').value);
  })[0];
  return card ? card.currency : null;
}

function clearReading() {
  el('shotReading').hidden = true;
  el('shotFile').value = '';
  fin.stored = null;
}

/* -------------------------------------------------------- the live rate */

function loadLiveRate() {
  return api('/api/rate/live').then(function (body) {
    if (!body.available) {
      el('liveRate').textContent = 'No live rate right now — ' +
        'the stored history is what your transactions use regardless.';
      return;
    }
    var pairs = Object.keys(body.rates).map(function (code) {
      return '1 EUR = ' + body.rates[code] + ' ' + code;
    }).join('  ·  ');
    el('liveRate').textContent = pairs +
      '  ·  published ' + (body.published || 'unknown') +
      ', fetched ' + body.fetched_at.slice(11, 16) +
      (body.daily_reference ? '  ·  a daily reference rate, not a tick' : '');
  }).catch(function () {
    el('liveRate').textContent = 'No live rate right now.';
  });
}

function estimate() {
  return postJson('/api/rate/estimate', {
    amount: el('estimateAmount').value,
    card_id: el('estimateCard').value || null
  }).then(function (body) {
    el('estimateResult').hidden = false;
    el('estimateResult').innerHTML =
      '<div class="line"><span>' + esc(body.base_text) +
        ' at ' + esc(body.rate) + '</span><b>' +
        esc(body.converted_text) + '</b></div>' +
      '<div class="line"><span>' +
        (body.card ? esc(body.card) + ' fee' : 'card fee') + ', ' +
        esc(body.fee_percent_text) + '</span><b>+' +
        esc(body.fee_text) + '</b></div>' +
      '<div class="line total"><span>You would be charged</span><b>' +
        esc(body.total_text) + '</b></div>' +
      '<p class="hint">An all-in rate of ' + esc(body.effective_rate) +
        ' against the published ' + esc(body.rate) + '. ' +
        (body.daily_reference
          ? 'The rate is the ' + esc(body.published || '') +
            ' reference figure; what you are actually billed depends on the ' +
            'day the purchase reaches the network.'
          : '') + '</p>';
  }).catch(function (bad) {
    el('estimateResult').hidden = false;
    el('estimateResult').innerHTML = '<p class="hint">' +
      esc(bad.message) + '</p>';
  });
}

/* ------------------------------------------------------------- the cards */

function loadCards() {
  return api('/api/cards?month=' + encodeURIComponent(state.month || ''))
    .then(function (body) {
      fin.cards = body.cards;
      fin.currencies = body.currencies;
      fin.defaultFeeText = body.default_fee_text;
      drawCards(body.cards);
      el('shotCard').innerHTML = cardOptions('');
      el('estimateCard').innerHTML = cardOptions('');
      el('cardCurrency').innerHTML = options(body.currencies, 'CAD');
      return body;
    });
}

function drawCards(rows) {
  if (!rows.length) {
    el('cardList').innerHTML = '<li class="empty">No cards yet. Add the one ' +
      'you spend on and its fee stops being a guess.</li>';
    el('cardFee').placeholder = fin.defaultFeeText || '2.50%';
    return;
  }
  el('cardList').innerHTML = rows.map(function (card) {
    /* Exact and estimated are shown apart, never summed into one figure that
     * looks exact. A reader who is not told assumes the whole thing is. */
    var how = [];
    if (card.exact_rows) {
      how.push(card.exact_rows + ' from statements');
    }
    if (card.estimated_rows) {
      how.push(card.estimated_rows + ' converted at the day’s rate');
    }
    if (card.unconvertible_rows) {
      how.push(card.unconvertible_rows + ' with no rate available');
    }

    return '<li>' +
      '<span class="what"><b>' + esc(card.label) + '</b>' +
        '<span class="when">' + esc(card.currency) + ' · fee ' +
        esc(card.fee_text) +
        (card.archived ? ' · closed' : '') + '</span></span>' +
      '<span class="much">' + esc(card.spent_text) +
        '<span class="when">' + esc(how.join(', ') || 'nothing yet') +
        '</span></span>' +
      '<span class="acts">' +
        '<button type="button" class="linky" data-measure="' +
          esc(card.id) + '">measure fee</button>' +
        '<button type="button" class="iconButton" data-card="' +
          esc(card.id) + '" title="Remove">×</button>' +
      '</span></li>';
  }).join('');
}

function measureFee(cardId) {
  return api('/api/cards/' + cardId + '/measured').then(function (body) {
    if (!body.available) { say(body.why, 'warn'); return; }
    say('Measured over ' + body.rows + ' purchases: ' +
      body.percent_text + ' against the ' + body.published_text +
      ' on file — ' + body.cost_text + ' in conversion cost.', 'good');
  }).catch(function (bad) { say(bad.message, 'bad'); });
}

/* ----------------------------------------------------- subscriptions */

function loadSubscriptions() {
  return api('/api/subscriptions?month=' +
             encodeURIComponent(state.month || '')).then(function (body) {
    var cost = body.cost;
    el('subsCost').textContent = cost.count
      ? cost.count + ' live, ' + cost.monthly_text + ' a month — ' +
        cost.yearly_text + ' a year at that rate' +
        (body.outstanding.count
          ? '. ' + body.outstanding.count + ' still expected this month (' +
            body.outstanding.expected_text + ').'
          : '. All landed.')
      : 'Nothing looks recurring yet — three months of a similar charge from ' +
        'the same merchant is what it takes.';
    drawSubscriptions(body.rows);
  });
}

var SUB_STATE = {
  landed: ['●', 'landed'],
  expected: ['○', 'expected'],
  lapsed: ['—', 'not seen lately']
};

function drawSubscriptions(rows) {
  if (!rows.length) { el('subsList').innerHTML = ''; return; }
  el('subsList').innerHTML = rows.map(function (row) {
    var mark = SUB_STATE[row.state];
    var rise = row.rise
      ? '<span class="rise">up ' + esc(row.rise.by_text) + ' from ' +
        esc(row.rise.was_text) + ' — ' + esc(row.rise.yearly_text) +
        ' a year</span>'
      : '';
    return '<li class="subRow ' + esc(row.state) + '">' +
      '<span class="what"><b>' + esc(row.merchant) + '</b>' +
        '<span class="when"><span class="g">' + esc(mark[0]) + '</span> ' +
        mark[1] + ' · ' + row.months + ' months · last ' +
        esc(row.last_seen) + '</span>' + rise + '</span>' +
      '<span class="much">' + esc(row.expected_text) + '</span></li>';
  }).join('');
}

/* ------------------------------------------------------------- the goals */

function loadGoals() {
  return api('/api/goals?month=' + encodeURIComponent(state.month || ''))
    .then(function (body) {
      var given = body.contributed;
      el('goalNote').textContent = given
        ? 'Your caps allowed ' + given.allowed_text + ' and ' +
          given.spent_text + ' has gone, so this month has put aside ' +
          given.saved_text +
          (given.overspent_minor
            ? ' — and is ' + given.overspent_text + ' over.'
            : '.')
        : 'Set a budget cap and this month’s underspend becomes ' +
          'progress. Without caps there is no "left over" to measure.';
      drawGoals(body.goals);
    });
}

function drawGoals(rows) {
  if (!rows.length) { el('goalList').innerHTML = ''; return; }
  el('goalList').innerHTML = rows.map(function (goal) {
    var due = goal.days_left === null
      ? 'no deadline'
      : (goal.days_left < 0 ? 'overdue' : goal.days_left + ' days left');
    var needed = goal.monthly_needed_minor
      ? ' · ' + esc(goal.monthly_needed_text) + ' a month to make it'
      : '';
    return '<li>' +
      '<span class="what"><b>' + esc(goal.name) + '</b>' +
        '<span class="when">' + esc(due) + needed +
        (goal.fundable ? '' : ' · in ' + esc(goal.currency) +
          ', which a euro underspend cannot fund') + '</span>' +
        '<span class="meter"><span style="width:' +
          Math.max(0, Math.min(100, goal.percent)) + '%"></span></span>' +
      '</span>' +
      '<span class="much">' + esc(goal.this_month_text) +
        '<span class="when">of ' + esc(goal.target_text) + '</span></span>' +
      '<button type="button" class="iconButton" data-goal="' +
        esc(goal.id) + '" title="Remove">×</button></li>';
  }).join('');
}

/* ------------------------------------------------------------ the movers */

function loadMovers() {
  return api('/api/trends?month=' + encodeURIComponent(state.month || ''))
    .then(function (body) { drawMovers(body.movers); });
}

function drawMovers(rows) {
  if (!rows.length) {
    el('moverList').innerHTML = '<li class="empty">Nothing far from its own ' +
      'average this month. Three months of history is the minimum before ' +
      'this can say anything.</li>';
    return;
  }
  el('moverList').innerHTML = rows.map(function (row) {
    return '<li class="mover ' + esc(row.direction) + '">' +
      '<button type="button" class="linky" data-mover="' +
        esc(row.category) + '">' + esc(row.category) + '</button>' +
      '<span class="when">' + esc(row.spent_text) + ' against an average of ' +
        esc(row.average_text) + ' over ' + row.months + ' months</span>' +
      '<span class="much ' + row.direction + '">' + esc(row.change_text) +
        ' (' + (row.percent > 0 ? '+' : '') + row.percent + '%)</span></li>';
  }).join('');
}

function openMover(category) {
  fin.openCategory = category;
  return api('/api/trends/' + encodeURIComponent(category) + '?month=' +
             encodeURIComponent(state.month || '')).then(function (body) {
    /* Each fragment is built and escaped on its own before anything is
     * assembled. Composing two already-escaped fragments is safe and is what
     * the `options`/`pickers` names in the frontend guard's allow-list are
     * for; escaping a fragment a second time would double every ampersand. */
    var rowsHtml = body.rows.map(function (row) {
      return '<tr><td>' + esc(row.spent_on) + '</td><td>' +
        esc(row.description) + '</td><td class="num">' +
        esc(row.amount_eur_text) + '</td></tr>';
    }).join('');

    var sparkHtml = body.history.map(function (point) {
      return '<span title="' + esc(point.month + ': ' + point.spent_text) +
        '">' + esc(point.month.slice(2)) + ' ' + esc(point.spent_text) +
        '</span>';
    }).join('');

    el('moverRows').hidden = false;
    el('moverRows').innerHTML =
      '<h3>' + esc(body.category) + ' · ' + esc(body.month) + ' · ' +
        esc(body.count) + ' purchases, ' + esc(body.spent_text) + '</h3>' +
      '<table><tbody>' + rowsHtml + '</tbody></table>' +
      '<div class="spark">' + sparkHtml + '</div>';
  });
}

/* --------------------------------------------------------------- wiring */

function loadFinance() {
  loadCards();
  loadSubscriptions();
  loadGoals();
  loadMovers();
}

function wireFinance() {
  el('shotFile').addEventListener('change', function () {
    if (this.files && this.files[0]) { readScreenshot(this.files[0]); }
  });

  el('shotForm').addEventListener('submit', function (event) {
    event.preventDefault();
    saveScreenshot();
  });

  el('shotCancel').addEventListener('click', clearReading);

  el('shotCandidates').addEventListener('click', function (event) {
    var value = event.target.dataset && event.target.dataset.amount;
    if (value) { el('shotAmount').value = value; }
  });

  el('estimateForm').addEventListener('submit', function (event) {
    event.preventDefault();
    estimate();
  });

  el('cardForm').addEventListener('submit', function (event) {
    event.preventDefault();
    var percent = Number(el('cardFee').value.replace(',', '.'));
    postJson('/api/cards', {
      name: el('cardName').value,
      currency: el('cardCurrency').value,
      mask: el('cardMask').value,
      /* The form asks for a percentage because that is what a card
       * statement quotes; the server stores basis points. */
      fee_bp: el('cardFee').value === '' ? null : Math.round(percent * 100)
    }).then(function () {
      el('cardName').value = '';
      el('cardMask').value = '';
      el('cardFee').value = '';
      say('Card added.', 'good');
      loadCards();
    }).catch(function (bad) { say(bad.message, 'bad'); });
  });

  el('cardList').addEventListener('click', function (event) {
    var data = event.target.dataset || {};
    if (data.measure) { measureFee(data.measure); }
    if (data.card) {
      api('/api/cards/' + data.card, { method: 'DELETE' })
        .then(function () {
          say('Card removed — its purchases were kept.', '');
          loadCards();
        }).catch(function (bad) { say(bad.message, 'bad'); });
    }
  });

  el('goalForm').addEventListener('submit', function (event) {
    event.preventDefault();
    postJson('/api/goals', {
      name: el('goalName').value,
      target: el('goalTarget').value,
      due_on: el('goalDue').value || null
    }).then(function () {
      el('goalName').value = '';
      el('goalTarget').value = '';
      el('goalDue').value = '';
      say('Goal added.', 'good');
      loadGoals();
    }).catch(function (bad) { say(bad.message, 'bad'); });
  });

  el('goalList').addEventListener('click', function (event) {
    var id = event.target.dataset && event.target.dataset.goal;
    if (!id) { return; }
    api('/api/goals/' + id, { method: 'DELETE' })
      .then(function () { say('Goal removed.', ''); loadGoals(); })
      .catch(function (bad) { say(bad.message, 'bad'); });
  });

  el('moverList').addEventListener('click', function (event) {
    var category = event.target.dataset && event.target.dataset.mover;
    if (category) { openMover(category); }
  });

  /* The month picker belongs to app.js; everything here is month-scoped, so
   * it has to redraw too. Listening rather than reaching into app.js's
   * handler keeps the two files from having to know about each other. */
  el('month').addEventListener('change', loadFinance);
}

wireFinance();
loadFinance();
loadLiveRate();
