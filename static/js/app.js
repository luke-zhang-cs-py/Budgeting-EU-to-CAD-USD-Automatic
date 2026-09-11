/* Wallet — the full view.
 *
 * The helpers it uses -- esc, el, say, send, api, postJson -- live in
 * common.js, which loads first. They moved there when the stripped-back view
 * appeared and needed the same four things; see the note at the top of that
 * file for why esc in particular must exist only once.
 */

'use strict';

var state = { month: null, categories: [], mapping: null, headers: [] };

/* ------------------------------------------------------------- overview */

function loadOverview() {
  var month = state.month ? ('?month=' + encodeURIComponent(state.month)) : '';
  return api('/api/overview' + month).then(function (body) {
    state.month = body.month;
    state.categories = body.categories;
    drawMonths(body.months, body.month);
    drawTotals(body.summary);
    drawBudgets(body.budgets, body.summary);
    drawTrend(body.trend);
    drawRecurring(body.recurring);
    drawCategories(body.categories);
    drawRates(body.rates);
    drawFx(body.fx);
    drawInbox(body.inbox, null);
    updateDownloadLinks();
  });
}

function drawMonths(months, current) {
  var picker = el('month');
  var all = months.slice();
  if (all.indexOf(current) === -1) all.unshift(current);
  picker.innerHTML = all.map(function (m) {
    return '<option value="' + esc(m) + '"' +
      (m === current ? ' selected' : '') + '>' + esc(m) + '</option>';
  }).join('');
}

function drawTotals(summary) {
  el('spentEur').textContent = summary.spent_text || '€0.00';
  el('spentCad').textContent = summary.spent_cad_text || '—';
  el('spentUsd').textContent = summary.spent_usd_text || '—';

  var left = el('remaining');
  if (summary.budgeted_eur) {
    left.textContent = summary.remaining_text;
    left.className = 'figure ' + (summary.remaining_eur < 0 ? 'over' : 'fine');
    el('leftLabel').textContent = 'Left of ' + summary.budgeted_text;
  } else {
    left.textContent = '—';
    left.className = 'figure';
    el('leftLabel').textContent = 'No budgets set';
  }

  var notes = [];
  if (summary.over.length) notes.push('Over budget: ' + summary.over.join(', '));
  if (summary.close.length) notes.push('Close: ' + summary.close.join(', '));
  if (summary.uncategorised_eur > 0) {
    notes.push('Uncategorised spending — add a rule below.');
  }
  say(notes.join('  ·  '), summary.over.length ? 'bad' : '');

  el('daysLeft').textContent = summary.days_left
    ? summary.days_left + ' days left in the month' : '';
}

function drawBudgets(rows, summary) {
  var box = el('budgetList');
  if (!rows.length) {
    box.innerHTML = '<p class="empty">Nothing spent or budgeted this month.</p>';
    return;
  }
  box.innerHTML = rows.map(function (row) {
    var share = row.share === null ? 0 : Math.min(1, row.share);
    var figures = row.cap_eur
      ? row.spent_text + ' of ' + row.cap_text +
        ' · ' + row.remaining_text + ' left'
      : row.spent_text + ' · no cap';
    var pace = row.pace
      ? '<span class="tag ' + esc(row.pace) + '">' + esc(row.pace) +
        ' pace</span>' : '';
    return '' +
      '<div class="budget">' +
        '<div class="budgetTop">' +
          '<span class="budgetName">' + esc(row.category) +
            ' ' + pace + '</span>' +
          '<span class="budgetFigures">' + esc(figures) + '</span>' +
        '</div>' +
        '<div class="bar ' + esc(row.state) + '">' +
          '<span style="width:' + (share * 100).toFixed(1) + '%"></span>' +
        '</div>' +
      '</div>';
  }).join('');
}

function drawTrend(rows) {
  var box = el('trend');
  if (!rows.length) { box.innerHTML = '<p class="empty">No history yet.</p>'; return; }
  var peak = Math.max.apply(null, rows.map(function (r) {
    return Math.abs(r.spent_eur);
  })) || 1;
  box.innerHTML = rows.map(function (row) {
    var width = (Math.abs(row.spent_eur) / peak * 100).toFixed(1);
    return '<div class="trendRow">' +
      '<span class="m">' + esc(row.month) + '</span>' +
      '<span class="trendBar"><span style="width:' + width + '%"></span></span>' +
      '<span class="v">' + esc(row.spent_text) + '</span>' +
      '</div>';
  }).join('');
}

function drawRecurring(rows) {
  var box = el('recurringList');
  if (!rows.length) {
    box.innerHTML = '<li class="empty">Nothing looks recurring yet — this ' +
      'needs three months of history.</li>';
    return;
  }
  box.innerHTML = rows.map(function (row) {
    return '<li><span class="keyword">' + esc(row.merchant) + '</span>' +
      '<span class="amount">' + esc(row.typical_text) + ' · ' +
      esc(row.months) + ' months · last ' + esc(row.last_seen) +
      '</span></li>';
  }).join('');
}


/* What the card's own conversion cost, over the rows that had a foreign
 * original. Hidden entirely when there are none -- a zero would imply the
 * conversion was free rather than absent. */
function drawFx(fx) {
  var tile = el('fxTile');
  if (!fx || !fx.available) { tile.hidden = true; return; }
  tile.hidden = false;
  el('fxCost').textContent = fx.cost_text;
  el('fxLabel').textContent = 'Card conversion cost, ' + fx.percent + '%';
  el('fxCost').className = 'figure over';
  /* Says what it covers: the same figure means something different over
   * three purchases than over forty. */
  el('fxCost').title = fx.cost_text + ' on ' + fx.rows + ' of ' + fx.of +
    ' rows — billed ' + fx.billed_text + ' against ' + fx.reference_text +
    ' at the reference rate';
}

function drawInbox(inbox, results) {
  if (!inbox) return;
  el('inboxFolder').textContent = inbox.folder;
  el('inboxState').textContent = inbox.watching
    ? 'watching every ' + inbox.intervalSeconds + 's'
    : (inbox.folderExists ? 'not watching — start the app to enable it'
                          : 'folder does not exist yet');
  /* Two different facts, because they answer different questions: how many
   * files are sitting there, and how many of them have not been read. They
   * used to be one number called "waiting" that counted files present, so it
   * said "1 waiting" forever after a successful import. */
  el('inboxWaiting').textContent = inbox.unread
    ? inbox.unread + ' of ' + inbox.files + ' file(s) not read yet'
    : (inbox.files
        ? inbox.files + ' file(s), all read'
        : 'folder is empty');

  var lines = (results || []).map(function (r) {
    var what = r.status === 'imported'
      ? (r.added + ' added, ' + r.duplicate + ' already known')
      : (r.status + ': ' + (r.why || ''));
    return '<li><span class="keyword">' + esc(r.file) + '</span>' +
      '<span class="amount">' + esc(what) + '</span></li>';
  });
  if (lines.length) { el('inboxHistory').innerHTML = lines.join(''); }
}

function drawImportHistory(history) {
  var box = el('inboxHistory');
  if (!history || !history.length) {
    box.innerHTML = '<li class="empty">Nothing imported from the folder yet.</li>';
    return;
  }
  box.innerHTML = history.map(function (h) {
    var what = h.unreadable && !h.added
      ? 'could not be read'
      : h.added + ' added, ' + h.duplicate + ' already known';
    return '<li><span class="keyword">' + esc(h.filename) + '</span>' +
      '<span class="amount">' + esc(what) + ' · ' + esc(h.at) + '</span></li>';
  }).join('');
}

function loadInbox() {
  return api('/api/sources').then(function (body) {
    drawInbox(body.inbox, null);
    drawImportHistory(body.history);
  });
}

function drawRates(rates) {
  el('rateInfo').textContent = rates.available
    ? ('ECB rates: ' + rates.days + ' days, ' + rates.first + ' to ' + rates.last)
    : 'No rate cache — press Refresh rates';
}

/* --------------------------------------------------------- transactions */

function loadTransactions() {
  var query = new URLSearchParams();
  if (state.month) query.set('month', state.month);
  var search = el('search').value.trim();
  if (search) query.set('q', search);
  return api('/api/transactions?' + query.toString()).then(function (body) {
    drawTransactions(body.transactions);
  });
}

function drawTransactions(rows) {
  var body = el('txBody');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">' +
      'Nothing here. Add spending or import a statement.</td></tr>';
    el('txNote').textContent = '';
    return;
  }
  var options = state.categories;
  body.innerHTML = rows.map(function (row) {
    var direction = row.amount_eur < 0 ? 'out' : 'in';
    var lag = row.rate_lag_days > 0
      ? '<span class="lag"> +' + esc(row.rate_lag_days) + 'd</span>' : '';
    var rate = row.rate_date
      ? esc(row.rate_date) + lag
      : '<span class="lag">' + esc(row.rate_note || 'no rate') + '</span>';
    var picker = '<select data-id="' + esc(row.id) + '" class="cat">' +
      options.map(function (c) {
        return '<option value="' + esc(c) + '"' +
          (c === row.category ? ' selected' : '') + '>' + esc(c) + '</option>';
      }).join('') + '</select>';
    return '<tr>' +
      '<td class="date">' + esc(row.spent_on) + '</td>' +
      '<td>' + esc(row.description) + '</td>' +
      '<td>' + picker + '</td>' +
      '<td class="num money ' + direction + '">' + esc(row.amount_eur_text) + '</td>' +
      '<td class="num">' + esc(row.amount_cad_text || '—') + '</td>' +
      '<td class="num">' + esc(row.amount_usd_text || '—') + '</td>' +
      '<td class="rate">' + rate + '</td>' +
      '<td class="num">' + (row.fx
        ? '<span class="lag" title="' + esc('billed ' + row.fx.billed_text +
            ', ' + row.fx.reference_text + ' at the reference rate') + '">' +
          esc(row.fx.cost_text) + '</span>'
        : '—') + '</td>' +
      '<td><button class="iconButton" data-delete="' + esc(row.id) +
        '" title="Delete">×</button></td>' +
      '</tr>';
  }).join('');
  el('txNote').textContent = rows.length + ' shown. "+3d" means the rate ' +
    'came from three days earlier — the last business day the ECB published.';
}

function updateDownloadLinks() {
  var query = new URLSearchParams();
  if (state.month) query.set('month', state.month);
  var search = el('search').value.trim();
  if (search) query.set('q', search);
  el('downloadCsv').href = '/export/transactions.csv?' + query.toString();
  el('downloadSummary').href = '/export/summary.csv?month=' +
    encodeURIComponent(state.month || '');
}

/* Whether the downloadable file adds up to the ledger it came from.
 *
 * Reported as agreement rather than as a figure, on purpose. The reconcile
 * total is the net of everything including income, so printing it beside
 * "Spent this month" -- which excludes income -- would put two different
 * numbers labelled similarly next to each other and invite the reader to
 * think one of them is wrong. */
function loadReconcile() {
  var month = state.month ? ('?month=' + encodeURIComponent(state.month)) : '';
  return api('/api/reconciles' + month).then(function (body) {
    el('reconcile').textContent = body.agrees
      ? 'CSV matches the ledger'
      : 'CSV does not match the ledger — please report this';
  }).catch(function () {
    el('reconcile').textContent = '';
  });
}

function refresh() {
  return loadOverview().then(loadTransactions).then(loadRules)
    .then(loadInbox).then(loadReconcile);
}

/* ----------------------------------------------------------------- rules */

function loadRules() {
  return api('/api/rules').then(function (body) { drawRules(body.rules); });
}

function drawRules(rules) {
  var box = el('ruleList');
  if (!rules.length) {
    box.innerHTML = '<li class="empty">No rules yet.</li>';
    return;
  }
  box.innerHTML = rules.map(function (rule) {
    return '<li><span class="keyword">' + esc(rule.keyword) + '</span>' +
      '<span class="amount">' + esc(rule.category) + '</span>' +
      '<button class="iconButton" data-rule="' + esc(rule.id) +
      '" title="Remove">×</button></li>';
  }).join('');
}

/* ---------------------------------------------------------------- import */

function chosenMapping() {
  var mapping = {};
  Array.prototype.forEach.call(
    document.querySelectorAll('#mappingRow select'), function (picker) {
      mapping[picker.dataset.field] = picker.value;
    });
  var flag = el('expensesPositive');
  if (flag) mapping.expenses_positive = flag.checked ? 'on' : '';
  return mapping;
}

function importFormData(withMapping) {
  var data = new FormData();
  var file = el('importFile').files[0];
  if (file) data.append('file', file);
  if (withMapping) {
    var mapping = chosenMapping();
    Object.keys(mapping).forEach(function (key) {
      data.append(key, mapping[key]);
    });
    if (el('useFileCategories').checked) {
      data.append('use_file_categories', 'on');
    }
  }
  return data;
}

function previewImport(withMapping) {
  if (!el('importFile').files[0]) {
    say('Choose a CSV file first.', 'bad');
    return Promise.resolve();
  }
  return api('/api/import/preview',
             { method: 'POST', body: importFormData(withMapping) })
    .then(function (body) {
      state.mapping = body.mapping;
      state.headers = body.headers;
      drawMapping(body);
      drawPreview(body);
      el('importPreview').hidden = false;
      say('');
    })
    .catch(function (bad) { say(bad.message, 'bad'); });
}

function drawMapping(body) {
  var fields = [
    ['date', 'Date'], ['description', 'Description'], ['amount', 'Amount'],
    ['amount_out', 'Paid out'], ['amount_in', 'Paid in'],
    ['currency', 'Currency'], ['category', 'Category']
  ];
  var pickers = fields.map(function (pair) {
    var field = pair[0];
    var chosen = body.mapping[field] || '';
    var options = ['<option value="">— none —</option>'].concat(
      body.headers.map(function (header) {
        return '<option value="' + esc(header) + '"' +
          (header === chosen ? ' selected' : '') + '>' + esc(header) +
          '</option>';
      })).join('');
    return '<label class="field"><span>' + esc(pair[1]) + '</span>' +
      '<select data-field="' + esc(field) + '">' + options + '</select></label>';
  }).join('');

  var flag = '<label class="check"><input type="checkbox" id="expensesPositive"' +
    (body.mapping.expenses_positive ? ' checked' : '') +
    '><span>Expenses are positive numbers</span></label>';

  el('mappingRow').innerHTML = pickers + flag;
  Array.prototype.forEach.call(
    document.querySelectorAll('#mappingRow select, #expensesPositive'),
    function (control) {
      control.addEventListener('change', function () { previewImport(true); });
    });
}

function drawPreview(body) {
  el('previewSummary').textContent =
    body.readable + ' rows readable, ' + body.unreadable + ' not. ' +
    'Spending in this file: ' + body.spending_total_text + '. ' +
    'Nothing is saved until you press Import.';

  el('previewBody').innerHTML = body.shown.map(function (row) {
    return '<tr><td class="date">' + esc(row.spent_on) + '</td>' +
      '<td>' + esc(row.description) + '</td>' +
      '<td class="num">' + esc(row.amount_text) + '</td></tr>';
  }).join('');

  el('previewProblems').innerHTML = (body.problems || []).map(function (p) {
    return '<li>row ' + esc(p.row) + ': ' + esc(p.why) + '</li>';
  }).join('');
}

/* ----------------------------------------------------------------- wiring */

function wire() {
  el('addDate').value = new Date().toISOString().slice(0, 10);

  el('month').addEventListener('change', function () {
    state.month = this.value;
    refresh();
  });

  el('search').addEventListener('input', function () {
    updateDownloadLinks();
    loadTransactions();
  });

  /* Scan now had an id, a label, and no handler at all. The endpoint existed,
   * was tested server-side and was in the README; clicking the button did
   * nothing. Found by the dead-id guard in test_frontend.py -- the same shape
   * as the reconcile check in the Tally app, where an empty element was
   * hiding a feature nobody had connected. */
  el('scanNow').addEventListener('click', function () {
    var button = this;
    button.disabled = true;
    button.textContent = 'Scanning…';
    api('/api/sources/scan', { method: 'POST' }).then(function (body) {
      var results = body.results || [];
      var added = results.reduce(function (total, one) {
        return total + (one.added || 0);
      }, 0);
      say(results.length
        ? results.length + ' file(s) read, ' + added + ' added.'
        : 'Nothing waiting in the folder.', added ? 'good' : '');
      drawInbox(body.inbox, results);
      return refresh();
    }).catch(function (bad) {
      say(bad.message, 'bad');
    }).then(function () {
      button.disabled = false;
      button.textContent = 'Scan now';
    });
  });

  el('refreshRates').addEventListener('click', function () {
    var button = this;
    button.disabled = true;
    button.textContent = 'Fetching…';
    api('/api/rates/refresh', { method: 'POST' }).then(function (body) {
      say(body.ok ? 'Rates updated.' : 'Could not reach the ECB — the app ' +
          'still works on the rates already cached.', body.ok ? 'good' : 'bad');
      return refresh();
    }).catch(function (bad) {
      say(bad.message, 'bad');
    }).then(function () {
      button.disabled = false;
      button.textContent = 'Refresh rates';
    });
  });

  el('addForm').addEventListener('submit', function (event) {
    event.preventDefault();
    postJson('/api/transaction', {
      date: el('addDate').value,
      description: el('addDescription').value,
      amount: el('addAmount').value,
      category: el('addCategory').value,
      kind: el('addKind').value
    }).then(function (body) {
      if (body.result === 'duplicate') {
        say('That looks like one already recorded, so nothing was added.', '');
      } else {
        say('Added.', 'good');
        el('addDescription').value = '';
        el('addAmount').value = '';
      }
      return refresh();
    }).catch(function (bad) { say(bad.message, 'bad'); });
  });

  el('budgetForm').addEventListener('submit', function (event) {
    event.preventDefault();
    postJson('/api/budget', {
      category: el('budgetCategory').value,
      cap: el('budgetCap').value
    }).then(function () {
      say('Budget saved.', 'good');
      el('budgetCap').value = '';
      return refresh();
    }).catch(function (bad) { say(bad.message, 'bad'); });
  });

  el('ruleForm').addEventListener('submit', function (event) {
    event.preventDefault();
    postJson('/api/rules', {
      keyword: el('ruleKeyword').value,
      category: el('ruleCategory').value
    }).then(function (body) {
      say(body.recategorised
        ? ('Rule added — ' + body.recategorised + ' existing rows updated.')
        : 'Rule added.', 'good');
      el('ruleKeyword').value = '';
      return refresh();
    }).catch(function (bad) { say(bad.message, 'bad'); });
  });

  el('importForm').addEventListener('submit', function (event) {
    event.preventDefault();
    previewImport(false);
  });

  el('importCancel').addEventListener('click', function () {
    el('importPreview').hidden = true;
    el('importFile').value = '';
    say('');
  });

  el('importConfirm').addEventListener('click', function () {
    var button = this;
    button.disabled = true;
    api('/api/import/commit', { method: 'POST', body: importFormData(true) })
      .then(function (body) {
        var parts = [body.added + ' added'];
        if (body.duplicate) parts.push(body.duplicate + ' already known');
        if (body.unreadable) parts.push(body.unreadable + ' unreadable');
        if (body.recategorised) parts.push(body.recategorised + ' categorised');
        say(parts.join(', ') + '.', 'good');
        el('importPreview').hidden = true;
        el('importFile').value = '';
        return refresh();
      })
      .catch(function (bad) { say(bad.message, 'bad'); })
      .then(function () { button.disabled = false; });
  });

  /* Delegated, because these controls are redrawn on every refresh. */
  document.addEventListener('change', function (event) {
    var picker = event.target.closest ? event.target.closest('select.cat') : null;
    if (!picker) return;
    postJson('/api/transaction/' + picker.dataset.id + '/category',
             { category: picker.value })
      .then(refresh)
      .catch(function (bad) { say(bad.message, 'bad'); });
  });

  document.addEventListener('click', function (event) {
    var target = event.target;
    if (target.dataset && target.dataset.delete) {
      send('/api/transaction/' + target.dataset.delete, { method: 'DELETE' })
        .then(refresh);
    }
    if (target.dataset && target.dataset.rule) {
      send('/api/rules/' + target.dataset.rule, { method: 'DELETE' })
        .then(refresh);
    }
  });
}

wire();
refresh().catch(function (bad) { say(bad.message, 'bad'); });
