/* morphoagent review page. Offline: everything it needs is embedded in #bfm-data.
 * The block between the curation-core markers is plain ES5 without DOM access so
 * that it can be unit-tested outside a browser (tests/test_ui.py). */

/* BEGIN curation-core */
var BfmCore = (function () {
  'use strict';
  var STATE_SCHEMA = 'morphoagent-ui-state/1';
  var KINDS = ['merged', 'fragmented', 'not_a_cell'];

  function num(a, b) { return a - b; }
  function emptyField() { return { deleted: [], merges: [], done: false }; }
  function newState(datasetId) {
    return { schema: STATE_SCHEMA, dataset: datasetId, saved_utc: null, fields: {}, enhance: null, segment: null,
             suggestions: {} };
  }
  function field(state, image) {
    if (!state.fields[image]) { state.fields[image] = emptyField(); }
    return state.fields[image];
  }
  function mergeIndex(fe, lb) {
    for (var i = 0; i < fe.merges.length; i++) { if (fe.merges[i].labels.indexOf(lb) >= 0) { return i; } }
    return -1;
  }
  function isDeleted(fe, lb) { return fe.deleted.indexOf(lb) >= 0; }

  /* Delete tool: toggle one label. A label that is part of a merge cannot be deleted. */
  function toggleDelete(fe, lb) {
    if (mergeIndex(fe, lb) >= 0) { return { ok: false, msg: 'label ' + lb + ' is part of a merge; remove the merge first' }; }
    var i = fe.deleted.indexOf(lb);
    if (i >= 0) { fe.deleted.splice(i, 1); } else { fe.deleted.push(lb); fe.deleted.sort(num); }
    return { ok: true, deleted: i < 0 };
  }

  function addLabel(m, lb) {
    m.labels.push(lb); m.labels.sort(num);
    if (m.auto) { m.target = m.labels[0]; }
  }

  /* Merge tool: join a and b. Joining a label of an existing merge extends it; two merges are united.
   * The target (surviving label) is the smallest label unless the user picked one (auto = false). */
  function merge(fe, a, b) {
    if (a === b) { return { ok: false, msg: 'pick two different labels' }; }
    if (isDeleted(fe, a) || isDeleted(fe, b)) { return { ok: false, msg: 'a deleted label cannot be merged; undelete it first' }; }
    var ia = mergeIndex(fe, a), ib = mergeIndex(fe, b);
    if (ia >= 0 && ia === ib) { return { ok: false, msg: 'labels ' + a + ' and ' + b + ' are already merged' }; }
    if (ia < 0 && ib < 0) {
      fe.merges.push({ labels: [a, b].sort(num), target: Math.min(a, b), auto: true });
    } else if (ia >= 0 && ib < 0) {
      addLabel(fe.merges[ia], b);
    } else if (ib >= 0 && ia < 0) {
      addLabel(fe.merges[ib], a);
    } else {
      var A = fe.merges[ia], B = fe.merges[ib];
      var labels = A.labels.concat(B.labels).sort(num);
      A.target = (A.auto && B.auto) ? labels[0] : (!A.auto ? A.target : B.target);
      A.auto = A.auto && B.auto;
      A.labels = labels;
      fe.merges.splice(ib, 1);
    }
    return { ok: true };
  }

  function setTarget(fe, idx, t) {
    var m = fe.merges[idx];
    if (!m || m.labels.indexOf(t) < 0) { return { ok: false, msg: 'target must be one of the merged labels' }; }
    m.target = t; m.auto = (t === m.labels[0]);
    return { ok: true };
  }
  function removeMerge(fe, idx) { fe.merges.splice(idx, 1); return { ok: true }; }
  function editCount(fe) { return fe ? fe.deleted.length + fe.merges.length : 0; }
  function totalEdits(state) {
    var n = 0;
    for (var k in state.fields) { if (Object.prototype.hasOwnProperty.call(state.fields, k)) { n += editCount(state.fields[k]); } }
    return n;
  }

  /* Rows of the curation CSV read by `morphoagent curate`: per image one delete row (all deleted labels),
   * then one row per merge with an explicit target. Images follow `order`. */
  function rows(state, order) {
    var out = [];
    order.forEach(function (image) {
      var fe = state.fields[image];
      if (!fe) { return; }
      if (fe.deleted.length) {
        out.push({ operation: 'delete', image: image, labels: fe.deleted.slice().sort(num).join(';'), target: '' });
      }
      fe.merges.forEach(function (m) {
        out.push({ operation: 'merge', image: image, labels: m.labels.slice().sort(num).join(';'), target: String(m.target) });
      });
    });
    return out;
  }
  function csvCell(v) {
    v = (v === null || v === undefined) ? '' : String(v);
    return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  }
  function toCSV(rowList) {
    var lines = ['operation,image,labels,target'];
    rowList.forEach(function (r) { lines.push([r.operation, r.image, r.labels, r.target].map(csvCell).join(',')); });
    return lines.join('\n') + '\n';
  }

  function intList(a) {
    if (!Array.isArray(a)) { return []; }
    return a.map(function (x) { return parseInt(x, 10); }).filter(function (x) { return x > 0 && x < 65536; });
  }

  /* Validate an imported state object. Unknown images are dropped with a warning. */
  function normalize(obj, knownImages, datasetId) {
    var warnings = [];
    var st = newState(datasetId);
    if (!obj || typeof obj !== 'object' || obj.schema !== STATE_SCHEMA) {
      return { state: null, warnings: ['not a morphoagent ui_state.json (schema ' + STATE_SCHEMA + ')'] };
    }
    if (obj.dataset && obj.dataset !== datasetId) { warnings.push('state was saved for another dataset id (' + obj.dataset + ')'); }
    var known = {};
    knownImages.forEach(function (k) { known[k] = true; });
    var fields = obj.fields || {};
    Object.keys(fields).forEach(function (image) {
      if (!known[image]) { warnings.push('unknown image ' + image + ' ignored'); return; }
      var f = fields[image] || {};
      var fe = emptyField();
      fe.done = !!f.done;
      fe.deleted = intList(f.deleted).sort(num);
      (Array.isArray(f.merges) ? f.merges : []).forEach(function (m) {
        var labels = intList(m && m.labels).sort(num);
        if (labels.length < 2) { return; }
        var t = parseInt(m.target, 10);
        if (labels.indexOf(t) < 0) { t = labels[0]; }
        fe.merges.push({ labels: labels, target: t, auto: t === labels[0] && m.auto !== false });
      });
      st.fields[image] = fe;
    });
    st.enhance = obj.enhance || null;
    st.segment = obj.segment || null;
    st.suggestions = (obj.suggestions && typeof obj.suggestions === 'object') ? obj.suggestions : {};
    st.saved_utc = obj.saved_utc || null;
    return { state: st, warnings: warnings };
  }

  /* Store assistant suggestions. This NEVER changes any edit; the user accepts them one by one. */
  function setSuggestions(state, image, list) {
    state.suggestions[image] = (Array.isArray(list) ? list : []).filter(function (s) {
      return s && KINDS.indexOf(s.kind) >= 0 && parseInt(s.label, 10) > 0;
    }).map(function (s) {
      return { label: parseInt(s.label, 10), kind: s.kind, with: intList(s['with']), reason: String(s.reason || '').slice(0, 300),
               status: 'open' };
    });
    return state.suggestions[image];
  }

  /* Apply ONE suggestion the user accepted: fragmented + partners -> merge; merged / not_a_cell -> delete
   * (the pipeline cannot split a mask, so a mask covering several cells is removed). */
  function acceptSuggestion(fe, s) {
    var r = { ok: true };
    if (s.kind === 'fragmented' && s['with'] && s['with'].length) {
      for (var i = 0; i < s['with'].length && r.ok; i++) {
        if (mergeIndex(fe, s.label) >= 0 && mergeIndex(fe, s.label) === mergeIndex(fe, s['with'][i])) { continue; }
        r = merge(fe, s.label, s['with'][i]);
      }
    } else if (s.kind === 'fragmented') {
      r = { ok: false, msg: 'no partner label given; merge manually' };
    } else if (!isDeleted(fe, s.label)) {
      r = toggleDelete(fe, s.label);
    }
    return r;
  }

  /* Decision document (schema morphoagent-decision/1) for a proposal entry {proposal, sha256}; the offline page
   * downloads it and `morphoagent decision --import` records it. choice = option id or 'rejected'. */
  var DECISION_SCHEMA = 'morphoagent-decision/1';
  function decisionDoc(entry, choice, comment, nowIso, version) {
    var p = entry.proposal, ids = p.options.map(function (o) { return o.id; });
    if (choice !== 'rejected' && ids.indexOf(choice) < 0) { return null; }
    return { schema: DECISION_SCHEMA, proposal_id: p.proposal_id, stage: p.stage, choice: choice,
             comment: String(comment || '').slice(0, 5000), decided_utc: nowIso.replace(/\.\d+Z$/, '+00:00').replace(/Z$/, '+00:00'),
             proposal_sha256: entry.sha256, source: 'review-ui-download', snapshot: null, morphoagent_version: version };
  }
  /* Web address of a normalised reference ('PMID:<n>' or 'doi:<doi>'); null for anything else. */
  function refUrl(ref, urls) {
    var m = /^PMID:(\d+)$/.exec(ref);
    if (m) { return urls.pmid + m[1] + '/'; }
    m = /^doi:(10\.\S+)$/.exec(ref);
    return m ? urls.doi + m[1] : null;
  }

  return { STATE_SCHEMA: STATE_SCHEMA, DECISION_SCHEMA: DECISION_SCHEMA, decisionDoc: decisionDoc, refUrl: refUrl,
           emptyField: emptyField, newState: newState, field: field, mergeIndex: mergeIndex,
           isDeleted: isDeleted, toggleDelete: toggleDelete, merge: merge, setTarget: setTarget, removeMerge: removeMerge,
           editCount: editCount, totalEdits: totalEdits, rows: rows, toCSV: toCSV, normalize: normalize,
           setSuggestions: setSuggestions, acceptSuggestion: acceptSuggestion };
})();
if (typeof module !== 'undefined' && module.exports) { module.exports = BfmCore; }
/* END curation-core */

(function () {
  'use strict';
  if (typeof document === 'undefined') { return; }
  var C = BfmCore;
  var D = JSON.parse(document.getElementById('bfm-data').textContent);
  var $ = function (id) { return document.getElementById(id); };
  var KEY = 'morphoagent-ui:' + D.dataset_id;
  var IMAGES = D.fields.map(function (f) { return f.image; });
  var S = C.newState(D.dataset_id);
  var tab = D.fields.length ? 3 : 5, cond = '';
  var TOKEN = null;

  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmt(v, d) { return (v === null || v === undefined) ? '-' : (+v).toFixed(d === undefined ? 2 : d); }
  function yamlNum(v) {
    if (v === null || v === undefined || v === '') { return 'null'; }
    v = +v;
    return Number.isInteger(v) ? v.toFixed(1) : String(v);
  }
  var msgTimer = null;
  function showMsg(text, cls) {
    var m = $('msg');
    m.hidden = false; m.className = 'small ' + (cls || ''); m.textContent = text;
    clearTimeout(msgTimer);
    msgTimer = setTimeout(function () { m.hidden = true; }, 5000);
  }
  function download(name, text, mime) {
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: mime || 'text/plain' }));
    a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  function inCond(c) { return !cond || c === cond; }

  // ------------------------------------------------------------------ persistence
  function apiHeaders(extra) {
    var h = extra || {};
    if (TOKEN) { h['X-BFM-Token'] = TOKEN; }
    return h;
  }
  if (D.live) {
    try {
      var q = new URLSearchParams(location.search);
      if (q.get('token')) {
        sessionStorage.setItem('bfm-token', q.get('token'));
        history.replaceState(null, '', location.pathname);
      }
      TOKEN = sessionStorage.getItem('bfm-token');
    } catch (e) { TOKEN = null; }
  }
  var saveTimer = null;
  function persist() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 300);
    updateProgress();
  }
  function saveNow() {
    S.saved_utc = new Date().toISOString();
    var txt = JSON.stringify(S);
    var where = [];
    try { localStorage.setItem(KEY, txt); where.push('browser'); } catch (e) { /* storage unavailable */ }
    if (D.live) {
      fetch('api/save', { method: 'POST', headers: apiHeaders({ 'Content-Type': 'application/json' }), body: txt })
        .then(function (r) {
          $('saveStatus').textContent = r.ok ? 'saved (' + where.concat(['server']).join(' + ') + ')' : 'server save FAILED (' + r.status + ')';
          $('saveStatus').className = 'small ' + (r.ok ? 'ok' : 'bad');
        })
        .catch(function () { $('saveStatus').textContent = 'server not reachable; saved in browser only'; $('saveStatus').className = 'small bad'; });
    } else {
      $('saveStatus').textContent = where.length ? 'autosaved in this browser' : 'autosave unavailable: use Export state';
      $('saveStatus').className = 'small ' + (where.length ? 'ok' : 'warn');
    }
  }
  function adopt(obj, source) {
    var r = C.normalize(obj, IMAGES, D.dataset_id);
    if (!r.state) { showMsg(source + ': ' + r.warnings.join('; '), 'bad'); return false; }
    S = r.state;
    if (r.warnings.length) { showMsg(source + ': ' + r.warnings.join('; '), 'warn'); }
    return true;
  }
  function loadState() {
    var local = null;
    try { local = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) { local = null; }
    if (local) { adopt(local, 'browser storage'); }
    if (!D.live) { return Promise.resolve(); }
    return fetch('api/state', { headers: apiHeaders() }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (srv) {
        if (srv && srv.schema && (!S.saved_utc || (srv.saved_utc && srv.saved_utc > S.saved_utc))) { adopt(srv, 'server state'); }
      }).catch(function () { showMsg('server not reachable; working from browser storage', 'warn'); });
  }

  // ------------------------------------------------------------------ top bar
  function updateProgress() {
    var shown = D.fields.filter(function (f) { return inCond(f.condition); });
    var done = shown.filter(function (f) { return S.fields[f.image] && S.fields[f.image].done; }).length;
    var edited = D.fields.filter(function (f) { return C.editCount(S.fields[f.image]) > 0; }).length;
    $('progress').textContent = 'done ' + done + '/' + shown.length + ' | edits ' + C.totalEdits(S) + ' in ' + edited + ' field(s)';
  }
  function setupTop() {
    var sel = $('cond');
    sel.innerHTML = '<option value="">all</option>' + D.conditions.map(function (c) { return '<option>' + esc(c) + '</option>'; }).join('');
    sel.onchange = function () { cond = sel.value; render(); };
    Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (t) {
      t.onclick = function () { tab = +t.dataset.tab; render(); };
    });
    $('btnExport').onclick = function () { saveNow(); download('ui_state.json', JSON.stringify(S, null, 1), 'application/json'); };
    $('btnImport').onclick = function () { $('fileImport').value = ''; $('fileImport').click(); };
    $('fileImport').onchange = function () {
      var file = $('fileImport').files[0];
      if (!file) { return; }
      file.text().then(function (t) {
        var obj;
        try { obj = JSON.parse(t); } catch (e) { showMsg('not valid JSON', 'bad'); return; }
        if (adopt(obj, file.name)) { history3 = {}; persist(); render(); showMsg('imported ' + file.name, 'ok'); }
      });
    };
  }

  function render() {
    Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (t) { t.classList.toggle('on', +t.dataset.tab === tab); });
    teardown3();
    [render1, render2, render3, render4, render5][tab - 1]();
    updateProgress();
  }

  // ------------------------------------------------------------------ tab 1: enhancement
  var PARAM_SECTION = { median_target: 'figure_default', clip_limit: 'clahe', amount: 'tone_unsharp' };
  function enhanceChoice() {
    if (S.enhance) { return S.enhance; }
    var c = D.enhance.current;
    return { preset: c.preset, params: c.preset === 'figure-default' ? { median_target: c.median_target } : {} };
  }
  function enhanceYaml() {
    var ch = enhanceChoice();
    var lines = ['enhance:', '  preset: ' + ch.preset];
    Object.keys(ch.params || {}).forEach(function (k) {
      lines.push('  ' + PARAM_SECTION[k] + ':', '    ' + k + ': ' + yamlNum(ch.params[k]));
    });
    return lines.join('\n') + '\n';
  }
  function variantMatches(v, ch) {
    if (v.preset !== ch.preset) { return false; }
    if (v.preset !== 'figure-default') { return !ch.params || Object.keys(ch.params).length === 0; }
    return ch.params && Math.abs((ch.params.median_target || 0) - v.median_target) < 1e-6;
  }
  function wipeHtml(id, before, after) {
    return '<div class="wipe" id="w' + id + '"><img class="after" src="' + after + '" alt="after">' +
      '<img class="before" src="' + before + '" alt="before"><div class="bar"></div>' +
      '<span class="lab" style="left:4px">before (unenhanced)</span><span class="lab after-lab" style="right:4px">after</span></div>' +
      '<div><input type="range" min="0" max="100" value="50" data-wipe="' + id + '" style="width:360px"> drag to compare</div>';
  }
  function bindWipe(id) {
    var w = $('w' + id), r = document.querySelector('input[data-wipe="' + id + '"]');
    function upd() {
      var p = +r.value;
      w.querySelector('.before').style.clipPath = 'inset(0 ' + (100 - p) + '% 0 0)';
      w.querySelector('.bar').style.left = p + '%';
    }
    r.oninput = upd; upd();
  }
  function render1() {
    var P = $('page'), E = D.enhance;
    var h = '<div class="note">Steps 2-3. Click the preview you prefer; the choice applies to all images. ' +
      'Enhancement changes only the images used for display and review, never segmentation or measurements. ' +
      'Metrics: edge = edge strength (higher = crisper), noise = flat-background noise (lower = cleaner), p50 = median grey level. ' +
      'The wipe slider compares the unenhanced image (left) with the selected preview (right).</div>';
    if (!E.available) { P.innerHTML = h + '<div class="bad row">' + esc(E.reason) + '</div>'; return; }
    var ch = enhanceChoice();
    h += '<div class="row">Selected: <b class="mono">' + esc(ch.preset) + ' ' + esc(JSON.stringify(ch.params || {})) +
      '</b> <button id="e_reset">Reset to config</button></div>';
    E.samples.forEach(function (s, i) {
      if (!inCond(s.condition)) { return; }
      var sel = s.variants.filter(function (v) { return variantMatches(v, ch); })[0];
      h += '<div class="sample"><b>' + esc(s.image) + '</b> <span class="small">(' + esc(s.condition) + ')</span><div class="row">' +
        wipeHtml(i, s.before, sel ? sel.uri : s.before) + '</div>';
      if (D.live) {
        h += '<div class="row small" data-live="' + i + '">Live (server): preset <select data-lp="' + i + '">' +
          '<option>figure-default</option><option>tone-unsharp</option><option>clahe</option></select> ' +
          '<span data-lpar="' + i + '"></span> <button data-lrun="' + i + '">Render</button> ' +
          '<button data-luse="' + i + '">Use these parameters</button> <span data-lmet="' + i + '" class="mono"></span></div>';
      }
      h += '<div class="cards">';
      s.variants.forEach(function (v, j) {
        var isSel = variantMatches(v, ch);
        h += '<div class="card' + (isSel ? ' sel' : '') + (v.is_current ? ' cur' : '') + '" data-s="' + i + '" data-v="' + j + '">' +
          '<img loading="lazy" src="' + v.uri + '" alt="">' + '<b>' + esc(v.preset) + '</b>' +
          (v.median_target !== null ? ' median ' + fmt(v.median_target, 0) : '') +
          (v.is_current ? '<span class="badge def">config</span>' : '') + (isSel ? '<span class="badge sel">selected</span>' : '') +
          '<br><span class="mono">edge ' + fmt(v.metrics.edge_strength) + ' | noise ' + fmt(v.metrics.flat_noise) +
          ' | p50 ' + fmt(v.metrics.p50, 0) + '</span><br><span class="small">' + esc(v.source) + '</span></div>';
      });
      h += '</div></div>';
    });
    h += '<h4>Config snippet</h4><textarea id="e_yaml" rows="6" readonly></textarea>' +
      '<div><button id="e_copy">Copy</button><button id="e_dl">Download enhance.yaml</button></div>';
    P.innerHTML = h;
    $('e_yaml').value = enhanceYaml();
    $('e_copy').onclick = function () { copyText($('e_yaml').value); };
    $('e_dl').onclick = function () { download('enhance.yaml', enhanceYaml(), 'text/yaml'); };
    $('e_reset').onclick = function () { S.enhance = null; persist(); render(); };
    P.querySelectorAll('.card').forEach(function (c) {
      c.onclick = function () {
        var v = E.samples[+c.dataset.s].variants[+c.dataset.v];
        S.enhance = { preset: v.preset, params: v.preset === 'figure-default' ? { median_target: v.median_target } : {} };
        persist(); render();
      };
      c.onmouseenter = function () {
        var w = $('w' + c.dataset.s);
        if (w) { w.querySelector('.after').src = E.samples[+c.dataset.s].variants[+c.dataset.v].uri; }
      };
    });
    E.samples.forEach(function (s, i) { if ($('w' + i)) { bindWipe(i); } });
    if (D.live) { bindLive(); }
  }
  function sliderFor(preset) {
    if (preset === 'figure-default') { return ['median_target', D.sliders.median_target]; }
    if (preset === 'clahe') { return ['clip_limit', D.sliders.clahe_clip]; }
    return ['amount', D.sliders.unsharp_amount];
  }
  var liveParams = {};
  function bindLive() {
    document.querySelectorAll('[data-live]').forEach(function (row) {
      var i = row.dataset.live, ps = row.querySelector('[data-lp]'), par = row.querySelector('[data-lpar]');
      function drawSlider() {
        var sp = sliderFor(ps.value), r = sp[1];
        var cur = liveParams[ps.value] !== undefined ? liveParams[ps.value] : (sp[0] === 'median_target' ? D.enhance.current.median_target : r[0]);
        par.innerHTML = sp[0] + ' <input type="range" min="' + r[0] + '" max="' + r[1] + '" step="' + r[2] + '" value="' + cur + '"> <span class="mono">' + cur + '</span>';
        var inp = par.querySelector('input');
        inp.oninput = function () { liveParams[ps.value] = +inp.value; par.querySelector('span').textContent = inp.value; };
        liveParams[ps.value] = +inp.value;
      }
      ps.onchange = drawSlider; drawSlider();
      row.querySelector('[data-lrun]').onclick = function () {
        var s = D.enhance.samples[+i], sp = sliderFor(ps.value), params = {};
        params[sp[0]] = liveParams[ps.value];
        row.querySelector('[data-lmet]').textContent = 'rendering...';
        fetch('api/enhance', { method: 'POST', headers: apiHeaders({ 'Content-Type': 'application/json' }),
                               body: JSON.stringify({ image: s.image, preset: ps.value, params: params }) })
          .then(function (r) {
            if (!r.ok) { return r.text().then(function (t) { throw new Error(t); }); }
            var met = r.headers.get('X-BFM-Metrics');
            return r.blob().then(function (b) { return [b, met]; });
          })
          .then(function (res) {
            $('w' + i).querySelector('.after').src = URL.createObjectURL(res[0]);
            var m = res[1] ? JSON.parse(res[1]) : {};
            row.querySelector('[data-lmet]').textContent = 'edge ' + fmt(m.edge_strength) + ' | noise ' + fmt(m.flat_noise) + ' | p50 ' + fmt(m.p50, 0);
          })
          .catch(function (e) { row.querySelector('[data-lmet]').textContent = 'error: ' + e.message; });
      };
      row.querySelector('[data-luse]').onclick = function () {
        var sp = sliderFor(ps.value), params = {};
        params[sp[0]] = liveParams[ps.value];
        S.enhance = { preset: ps.value, params: params };
        persist(); $('e_yaml').value = enhanceYaml();
        showMsg('selected ' + ps.value + ' ' + JSON.stringify(params), 'ok');
      };
    });
  }
  function copyText(t) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(function () { showMsg('copied', 'ok'); }, function () { showMsg('copy failed; select the text and copy it', 'warn'); });
    } else { showMsg('select the text and copy it', 'warn'); }
  }

  // ------------------------------------------------------------------ tab 2: segmentation settings
  function settingById(id) { return D.segment.settings.filter(function (s) { return s.setting === id; })[0]; }
  function chosenSetting() {
    if (S.segment && settingById(S.segment.setting)) { return S.segment.setting; }
    var d = D.segment.settings.filter(function (s) { return s.is_default; })[0];
    return d ? d.setting : null;
  }
  function segmentYaml() {
    var s = settingById(chosenSetting());
    if (!s) { return ''; }
    return 'segment:\n  flow_threshold: ' + yamlNum(s.flow_threshold) + '\n  cellprob_threshold: ' + yamlNum(s.cellprob_threshold) +
      '\n  diameter: ' + yamlNum(s.diameter) + '\n  downscale: ' + yamlNum(s.downscale) + '\n';
  }
  function render2() {
    var P = $('page'), G = D.segment;
    var h = '<div class="note">Step 4. Each panel is one Cellpose-SAM setting on a sample image (red = outlines). ' +
      'Pick the setting whose outlines follow single cells best. Green border = current config (DEFAULT); blue = your choice. ' +
      'n = objects, (>= min) = objects with area >= ' + esc(D.select.area_min_px) + ' px^2. Double-click a panel to enlarge.</div>';
    if (!G.available) { P.innerHTML = h + '<div class="bad row">' + esc(G.reason) + '</div>'; return; }
    var ch = chosenSetting();
    h += '<div class="row">Selected: <b class="mono">' + esc(ch) + '</b> <button id="g_reset">Reset to config</button> ' +
      'Panel width <input type="range" id="g_w" min="120" max="480" value="' + (+(localGet('bfm-panelw') || 240)) + '"></div>';
    G.samples.forEach(function (s, i) {
      if (!inCond(s.condition)) { return; }
      h += '<div class="sample"><b>' + esc(s.image) + '</b> <span class="small">(' + esc(s.condition) + ')</span>' +
        '<div class="panelgrid" data-grid="' + i + '">';
      s.panels.forEach(function (p, j) {
        var st = settingById(p.setting) || {};
        h += '<div class="panel' + (st.is_default ? ' def' : '') + (p.setting === ch ? ' sel' : '') + '" data-set="' + esc(p.setting) + '" data-s="' + i + '" data-p="' + j + '">' +
          '<img loading="lazy" src="' + p.uri + '" alt=""><b>' + esc(p.setting) + '</b>' +
          (st.is_default ? '<span class="badge def">DEFAULT</span>' : '') + (p.setting === ch ? '<span class="badge sel">selected</span>' : '') +
          ' n=' + p.n_objects + ' (' + p.n_objects_area_ge_min + ')<br><span class="mono">ds ' + st.downscale + ' dia ' +
          (st.diameter === null ? 'auto' : st.diameter) + ' flow ' + st.flow_threshold + ' cp ' + st.cellprob_threshold + '</span></div>';
      });
      h += '</div></div>';
    });
    h += '<h4>Config snippet</h4><textarea id="g_yaml" rows="6" readonly></textarea>' +
      '<div><button id="g_copy">Copy</button><button id="g_dl">Download segment.yaml</button></div>' +
      '<div id="g_big" hidden style="position:fixed;inset:40px;background:rgba(0,0,0,.85);z-index:30;text-align:center;cursor:zoom-out"><img style="max-width:100%;max-height:100%"></div>';
    P.innerHTML = h;
    function layout() {
      var w = +$('g_w').value;
      localSet('bfm-panelw', w);
      P.querySelectorAll('.panelgrid').forEach(function (g) {
        var n = G.samples[+g.dataset.grid].n_cols;
        g.style.gridTemplateColumns = 'repeat(' + n + ', ' + w + 'px)';
      });
    }
    $('g_w').oninput = layout; layout();
    $('g_yaml').value = segmentYaml();
    $('g_copy').onclick = function () { copyText($('g_yaml').value); };
    $('g_dl').onclick = function () { download('segment.yaml', segmentYaml(), 'text/yaml'); };
    $('g_reset').onclick = function () { S.segment = null; persist(); render(); };
    $('g_big').onclick = function () { $('g_big').hidden = true; };
    P.querySelectorAll('.panel').forEach(function (el) {
      el.onclick = function () { S.segment = { setting: el.dataset.set }; persist(); render(); };
      el.ondblclick = function (ev) {
        ev.stopPropagation();
        $('g_big').querySelector('img').src = G.samples[+el.dataset.s].panels[+el.dataset.p].uri;
        $('g_big').hidden = false;
      };
    });
  }
  function localGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function localSet(k, v) { try { localStorage.setItem(k, String(v)); } catch (e) { /* ignore */ } }

  // ------------------------------------------------------------------ tab 3: mask review
  var FD = {};            // per-field decoded assets
  var cur = null;         // current image id
  var tool = 'delete', pending = null, hoverLb = 0;
  var view = { s: 1, x: 0, y: 0 };
  var showOutlines = true, showNumbers = true, onlyEdited = false, onlyOpen = false;
  var history3 = {};      // image -> {undo: [], redo: []}
  var keyHandler = null, resizeHandler = null;

  function fieldByImage(im) { return D.fields.filter(function (f) { return f.image === im; })[0]; }
  function loadImg(uri) {
    return new Promise(function (res, rej) { var im = new Image(); im.onload = function () { res(im); }; im.onerror = rej; im.src = uri; });
  }
  function decodeLabels(uri, W, H) {
    var srcP = (typeof createImageBitmap === 'function')
      ? fetch(uri).then(function (r) { return r.blob(); })
        .then(function (b) { return createImageBitmap(b, { colorSpaceConversion: 'none', premultiplyAlpha: 'none' }); })
        .catch(function () { return loadImg(uri); })
      : loadImg(uri);
    return srcP.then(function (src) {
      var c = document.createElement('canvas'); c.width = W; c.height = H;
      var ctx = c.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(src, 0, 0);
      var d = ctx.getImageData(0, 0, W, H).data, m = new Uint16Array(W * H);
      for (var i = 0, j = 0; i < m.length; i++, j += 4) { m[i] = d[j] + 256 * d[j + 1]; }
      return m;
    });
  }
  function ensureField(f) {
    if (FD[f.image]) { return FD[f.image].ready; }
    var fd = { objs: {}, maxLabel: 0, overlay: document.createElement('canvas') };
    f.objects.forEach(function (o) { fd.objs[o.label] = o; fd.maxLabel = Math.max(fd.maxLabel, o.label); });
    fd.overlay.width = f.width; fd.overlay.height = f.height;
    fd.ready = Promise.all([loadImg(f.bg), decodeLabels(f.labels, f.width, f.height)]).then(function (r) {
      fd.bg = r[0]; fd.map = r[1];
      var seen = {}, missing = [];
      for (var i = 0; i < fd.map.length; i++) { seen[fd.map[i]] = true; if (fd.map[i] > fd.maxLabel) { fd.maxLabel = fd.map[i]; } }
      f.objects.forEach(function (o) { if (o.group_rule !== 'below_area_min' && !seen[o.label]) { missing.push(o.label); } });
      fd.missing = missing;
      return fd;
    });
    FD[f.image] = fd;
    return fd.ready;
  }
  function fe() { return C.field(S, cur); }
  function hist(im) { if (!history3[im]) { history3[im] = { undo: [], redo: [] }; } return history3[im]; }
  function mutate(fn) {
    var before = JSON.stringify(fe());
    var r = fn(fe());
    if (r && r.ok === false) { showMsg(r.msg, 'warn'); return r; }
    if (JSON.stringify(fe()) !== before) {
      var h = hist(cur); h.undo.push(before); h.redo = [];
      if (h.undo.length > 200) { h.undo.shift(); }
      persist(); rebuildOverlay(); drawSide();
    }
    return r;
  }
  function undo() {
    var h = hist(cur); if (!h.undo.length) { return; }
    h.redo.push(JSON.stringify(fe())); S.fields[cur] = JSON.parse(h.undo.pop());
    pending = null; persist(); rebuildOverlay(); drawSide();
  }
  function redo() {
    var h = hist(cur); if (!h.redo.length) { return; }
    h.undo.push(JSON.stringify(fe())); S.fields[cur] = JSON.parse(h.redo.pop());
    pending = null; persist(); rebuildOverlay(); drawSide();
  }

  function rebuildOverlay() {
    var f = fieldByImage(cur), fd = FD[cur];
    if (!fd || !fd.map) { return; }
    var e = fe(), W = f.width, H = f.height, map = fd.map, n = fd.maxLabel + 1;
    var canon = new Int32Array(n), style = new Uint8Array(n), fill = new Uint8Array(n), i;
    for (i = 0; i < n; i++) { canon[i] = i; }
    Object.keys(fd.objs).forEach(function (k) {
      var o = fd.objs[k];
      style[o.label] = o.group_rule === 'kept' ? 1 : (o.group_rule === 'frame_contact' ? 2 : 0);
    });
    e.deleted.forEach(function (lb) { if (lb < n) { fill[lb] = 1; } });
    e.merges.forEach(function (m) {
      var st = 0;
      m.labels.forEach(function (lb) { if (lb < n) { st = (style[lb] === 1 || st === 1) ? 1 : Math.max(st, style[lb]); } });
      if (st === 0) { st = 1; }
      m.labels.forEach(function (lb) { if (lb < n) { canon[lb] = m.target; fill[lb] = 2; } });
      if (m.target < n) { style[m.target] = st; }
    });
    if (pending && pending < n) { fill[pending] = 3; }
    if (hoverLb && hoverLb < n && !fill[hoverLb]) { fill[hoverLb] = 4; }
    var kc = D.review.kept_color, fc = D.review.frame_contact_color;
    var lw = Math.max(1, Math.round(W / 700)), dash = Math.max(4, Math.round(22 * f.scale));
    var ctx = fd.overlay.getContext('2d'), img = ctx.createImageData(W, H), px = img.data;
    var FILL = [null, [255, 0, 0, 120], [40, 120, 255, 90], [255, 220, 0, 130], [255, 255, 255, 60]];
    for (var y = 0; y < H; y++) {
      for (var x = 0; x < W; x++) {
        var p = y * W + x, lb = map[p];
        if (!lb) { continue; }
        var q = p * 4, fl = fill[lb];
        if (fl) { var c = FILL[fl]; px[q] = c[0]; px[q + 1] = c[1]; px[q + 2] = c[2]; px[q + 3] = c[3]; }
        if (!showOutlines) { continue; }
        var cl = canon[lb], st = style[cl];
        if (!st) { continue; }
        var edge = false;
        for (var k = 1; k <= lw && !edge; k++) {
          if ((x - k >= 0 && canon[map[p - k]] !== cl) || (x + k < W && canon[map[p + k]] !== cl) ||
              (y - k >= 0 && canon[map[p - k * W]] !== cl) || (y + k < H && canon[map[p + k * W]] !== cl)) { edge = true; }
        }
        if (!edge) { continue; }
        if (st === 1) { px[q] = kc[0]; px[q + 1] = kc[1]; px[q + 2] = kc[2]; px[q + 3] = 255; }
        else if ((((x / dash) | 0) + ((y / dash) | 0)) % 2 === 0) { px[q] = fc[0]; px[q + 1] = fc[1]; px[q + 2] = fc[2]; px[q + 3] = 255; }
      }
    }
    ctx.putImageData(img, 0, 0);
    draw();
  }

  function stageSize() { var s = $('stage'); return [s.clientWidth, s.clientHeight]; }
  function fit() {
    var f = fieldByImage(cur), sz = stageSize();
    view.s = Math.min(sz[0] / f.width, sz[1] / f.height);
    view.x = (sz[0] - f.width * view.s) / 2; view.y = (sz[1] - f.height * view.s) / 2;
    draw();
  }
  function zoomAt(factor, sx, sy) {
    var ns = Math.max(0.05, Math.min(40, view.s * factor));
    view.x = sx - (sx - view.x) * ns / view.s; view.y = sy - (sy - view.y) * ns / view.s; view.s = ns;
    draw();
  }
  function draw() {
    var cv = $('cv'), fd = FD[cur];
    if (!cv || !fd || !fd.bg) { return; }
    var sz = stageSize();
    if (cv.width !== sz[0] || cv.height !== sz[1]) { cv.width = sz[0]; cv.height = sz[1]; }
    var ctx = cv.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.setTransform(view.s, 0, 0, view.s, view.x, view.y);
    ctx.imageSmoothingEnabled = true; ctx.drawImage(fd.bg, 0, 0);
    ctx.imageSmoothingEnabled = false; ctx.drawImage(fd.overlay, 0, 0);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    var f = fieldByImage(cur), e = fe(), sugg = S.suggestions[cur] || [];
    var sugLabels = {};
    sugg.forEach(function (s) { if (s.status === 'open') { sugLabels[s.label] = true; } });
    if (!showNumbers) { return; }
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    f.objects.forEach(function (o) {
      var inEdit = C.isDeleted(e, o.label) || C.mergeIndex(e, o.label) >= 0;
      if (o.group_rule === 'below_area_min' && !inEdit && !sugLabels[o.label]) { return; }
      var fs = Math.max(11, Math.min(30, 0.22 * Math.sqrt(o.area_px) * f.scale * view.s));
      var sx = o.x * view.s + view.x, sy = o.y * view.s + view.y, txt = String(o.label);
      var mi = C.mergeIndex(e, o.label);
      if (mi >= 0 && e.merges[mi].target !== o.label) { txt += '>' + e.merges[mi].target; }
      ctx.font = 'bold ' + Math.round(fs) + 'px Arial, sans-serif';
      ctx.lineWidth = Math.max(3, fs / 6); ctx.strokeStyle = '#000'; ctx.strokeText(txt, sx, sy);
      ctx.fillStyle = C.isDeleted(e, o.label) ? '#ff8080' : '#fff'; ctx.fillText(txt, sx, sy);
      if (sugLabels[o.label]) {
        ctx.fillStyle = '#f3d000'; ctx.beginPath(); ctx.arc(sx + fs, sy - fs * 0.7, Math.max(5, fs / 3), 0, 2 * Math.PI); ctx.fill();
        ctx.strokeStyle = '#000'; ctx.lineWidth = 1; ctx.stroke();
      }
    });
  }
  function labelAt(sx, sy) {
    var f = fieldByImage(cur), fd = FD[cur];
    if (!fd || !fd.map) { return 0; }
    var ix = Math.floor((sx - view.x) / view.s), iy = Math.floor((sy - view.y) / view.s);
    if (ix < 0 || iy < 0 || ix >= f.width || iy >= f.height) { return 0; }
    return fd.map[iy * f.width + ix];
  }
  function tipText(lb) {
    var o = FD[cur].objs[lb], e = fe();
    var t = 'label ' + lb;
    if (o) { t += ' | area ' + Math.round(o.area_px) + ' px^2 | frame contact ' + o.border_contact_px + ' px | ' + o.group; }
    else { t += ' | (not in the object table)'; }
    if (C.isDeleted(e, lb)) { t += ' | DELETE'; }
    var mi = C.mergeIndex(e, lb);
    if (mi >= 0) { t += ' | MERGE ' + e.merges[mi].labels.join('+') + ' -> ' + e.merges[mi].target; }
    return t;
  }
  function clickLabel(lb, shift) {
    if (!lb) { pending = null; rebuildOverlay(); return; }
    if (tool === 'delete' && !shift) { mutate(function (e) { return C.toggleDelete(e, lb); }); return; }
    if (!pending) { pending = lb; rebuildOverlay(); showMsg('merge: now click the label to merge with ' + lb + ' (Esc cancels)', ''); return; }
    var a = pending; pending = null;
    if (a === lb) { rebuildOverlay(); return; }
    mutate(function (e) { return C.merge(e, a, lb); });
    rebuildOverlay();
  }

  function render3() {
    var P = $('page');
    if (!D.fields.length) { P.innerHTML = '<div class="bad row">' + esc(D.fields_reason || 'no fields') + '</div>'; return; }
    var lines = D.llm.enabled ? '<div class="privacy"><b>Assistant enabled.</b> "Ask assistant" sends this field\'s numbered overlay image and object table to ' +
      esc(D.llm.endpoint_host) + '. Images and tables leave this machine. Do not use it for unpublished or sensitive data without permission. ' +
      'Suggestions never edit anything by themselves; accept them one by one.</div>' : '';
    P.innerHTML = '<div id="review"><div><div class="small row"><label><input type="checkbox" id="f_ed"' + (onlyEdited ? ' checked' : '') + '> with edits</label> ' +
      '<label><input type="checkbox" id="f_open"' + (onlyOpen ? ' checked' : '') + '> not done</label></div><div id="flist"></div></div>' +
      '<div id="stagewrap"><div class="row"><span class="tool" data-tool="delete">Delete tool <kbd>D</kbd></span> ' +
      '<span class="tool" data-tool="merge">Merge tool <kbd>M</kbd></span> <button id="b_undo">Undo <kbd>Z</kbd></button><button id="b_redo">Redo <kbd>Y</kbd></button> ' +
      '<button id="b_fit">Fit <kbd>0</kbd></button> <label><input type="checkbox" id="b_done"> field done <kbd>F</kbd></label> ' +
      '<span id="fhead" class="small mono"></span></div><div id="stage"><canvas id="cv"></canvas><div id="tip"></div></div></div>' +
      '<div id="side">' + lines + '<div id="sideBody"></div>' +
      '<div class="note small"><b>Keys</b><br><kbd>D</kbd> delete tool (click toggles DELETE) <br><kbd>M</kbd> merge tool (click two masks; Shift+click merges in any tool)<br>' +
      '<kbd>Esc</kbd> cancel merge selection <br><kbd>T</kbd> make the hovered label the merge target<br><kbd>Z</kbd>/<kbd>Ctrl+Z</kbd> undo, <kbd>Y</kbd>/<kbd>Ctrl+Shift+Z</kbd> redo<br>' +
      '<kbd>N</kbd>/<kbd>&rarr;</kbd> next field, <kbd>P</kbd>/<kbd>&larr;</kbd> previous <br><kbd>F</kbd> toggle done <br>' +
      'wheel / <kbd>+</kbd> <kbd>-</kbd> zoom, drag to pan, <kbd>0</kbd> fit<br><kbd>O</kbd> outlines on/off, <kbd>L</kbd> numbers on/off<br><br>' +
      'Red solid = kept; pale dashed = frame contact; objects below area_min are not drawn (hover still shows them). ' +
      'Red fill = DELETE, blue fill = MERGE (internal outline removed, number shows "label&gt;target"), yellow = first merge click, yellow dot = assistant suggestion.</div></div></div>';
    $('f_ed').onchange = function () { onlyEdited = $('f_ed').checked; drawList(); };
    $('f_open').onchange = function () { onlyOpen = $('f_open').checked; drawList(); };
    P.querySelectorAll('.tool').forEach(function (t) { t.onclick = function () { setTool(t.dataset.tool); }; });
    $('b_undo').onclick = undo; $('b_redo').onclick = redo; $('b_fit').onclick = fit;
    $('b_done').onchange = function () { mutateDone($('b_done').checked); };
    setTool(tool);
    var list = visibleFields();
    if (!cur || !list.some(function (f) { return f.image === cur; })) { cur = list.length ? list[0].image : null; }
    drawList();
    bindStage();
    keyHandler = onKey; document.addEventListener('keydown', keyHandler);
    resizeHandler = function () { if (FD[cur] && FD[cur].bg) { fit(); } }; window.addEventListener('resize', resizeHandler);
    openField(cur);
  }
  function teardown3() {
    if (keyHandler) { document.removeEventListener('keydown', keyHandler); keyHandler = null; }
    if (resizeHandler) { window.removeEventListener('resize', resizeHandler); resizeHandler = null; }
  }
  function setTool(t) {
    tool = t; pending = null;
    document.querySelectorAll('.tool').forEach(function (el) { el.classList.toggle('on', el.dataset.tool === t); });
    if (FD[cur]) { rebuildOverlay(); }
  }
  function mutateDone(v) {
    var e = fe(); if (e.done === v) { return; }
    var before = JSON.stringify(e); e.done = v;
    var h = hist(cur); h.undo.push(before); h.redo = [];
    persist(); drawList(); drawSide();
  }
  function visibleFields() {
    return D.fields.filter(function (f) {
      var e = S.fields[f.image];
      return inCond(f.condition) && (!onlyEdited || C.editCount(e) > 0) && (!onlyOpen || !(e && e.done));
    });
  }
  function drawList() {
    var el = $('flist'); if (!el) { return; }
    el.innerHTML = visibleFields().map(function (f) {
      var e = S.fields[f.image], n = C.editCount(e);
      return '<div data-im="' + esc(f.image) + '" class="' + (f.image === cur ? 'cur ' : '') + (e && e.done ? 'done' : '') + '">' +
        (e && e.done ? '&#10003; ' : '') + esc(f.image) + (n ? ' <span class="badge">' + n + '</span>' : '') +
        (f.image_trimmed ? ' <span class="badge">trimmed</span>' : '') + '</div>';
    }).join('') || '<div class="small">no field matches the filters</div>';
    el.querySelectorAll('[data-im]').forEach(function (d) { d.onclick = function () { openField(d.dataset.im); }; });
    updateProgress();
  }
  function openField(im) {
    cur = im; pending = null; hoverLb = 0;
    drawList();
    if (!im) { $('fhead').textContent = 'no field'; return; }
    var f = fieldByImage(im);
    $('fhead').textContent = 'loading ' + im + ' ...';
    ensureField(f).then(function (fd) {
      if (cur !== im) { return; }
      $('fhead').textContent = im + ' | ' + f.condition + ' | kept ' + f.n_kept + ', frame contact ' + f.n_frame_contact +
        ' | ' + f.width + 'x' + f.height + ' px shown (x' + (1 / f.scale).toFixed(2) + ' = full resolution)';
      if (fd.missing.length) { showMsg('labels ' + fd.missing.join(', ') + ' vanish at the embedded resolution; raise ui.max_width to click them', 'warn'); }
      fit(); rebuildOverlay(); drawSide();
    }).catch(function (err) { $('fhead').textContent = 'could not decode ' + im + ': ' + err; });
  }
  function step(d) {
    var list = visibleFields(), i = list.map(function (f) { return f.image; }).indexOf(cur);
    var j = Math.max(0, Math.min(list.length - 1, i + d));
    if (list[j]) { openField(list[j].image); }
  }
  function bindStage() {
    var cv = $('cv'), tip = $('tip'), drag = null;
    cv.onmousedown = function (ev) { drag = { x: ev.clientX, y: ev.clientY, vx: view.x, vy: view.y, moved: false }; };
    cv.onmousemove = function (ev) {
      var r = cv.getBoundingClientRect(), sx = ev.clientX - r.left, sy = ev.clientY - r.top;
      if (drag && (ev.buttons & 1)) {
        var dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 4) { drag.moved = true; }
        if (drag.moved) { view.x = drag.vx + dx; view.y = drag.vy + dy; draw(); tip.style.display = 'none'; return; }
      }
      var lb = labelAt(sx, sy);
      if (lb !== hoverLb) { hoverLb = lb; rebuildOverlay(); }
      if (lb) { tip.textContent = tipText(lb); tip.style.display = 'block'; tip.style.left = (sx + 14) + 'px'; tip.style.top = (sy + 14) + 'px'; }
      else { tip.style.display = 'none'; }
    };
    cv.onmouseleave = function () { tip.style.display = 'none'; if (hoverLb) { hoverLb = 0; rebuildOverlay(); } };
    cv.onmouseup = function (ev) {
      if (drag && !drag.moved && ev.button === 0) {
        var r = cv.getBoundingClientRect();
        clickLabel(labelAt(ev.clientX - r.left, ev.clientY - r.top), ev.shiftKey);
      }
      drag = null;
    };
    cv.onwheel = function (ev) {
      ev.preventDefault();
      var r = cv.getBoundingClientRect();
      zoomAt(ev.deltaY < 0 ? 1.2 : 1 / 1.2, ev.clientX - r.left, ev.clientY - r.top);
    };
  }
  function onKey(ev) {
    var t = ev.target && ev.target.tagName;
    if (t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT') { return; }
    var k = ev.key, ctrl = ev.ctrlKey || ev.metaKey, sz = stageSize();
    if (ctrl && (k === 'z' || k === 'Z')) { ev.preventDefault(); if (ev.shiftKey) { redo(); } else { undo(); } return; }
    if (ctrl && (k === 'y' || k === 'Y')) { ev.preventDefault(); redo(); return; }
    if (ctrl || ev.altKey) { return; }
    switch (k) {
      case 'd': case 'D': setTool('delete'); break;
      case 'm': case 'M': setTool('merge'); break;
      case 'Escape': pending = null; rebuildOverlay(); break;
      case 'z': case 'Z': undo(); break;
      case 'y': case 'Y': redo(); break;
      case 'n': case 'N': case 'ArrowRight': case 'PageDown': step(1); break;
      case 'p': case 'P': case 'ArrowLeft': case 'PageUp': step(-1); break;
      case 'f': case 'F': mutateDone(!fe().done); break;
      case '0': fit(); break;
      case '+': case '=': zoomAt(1.25, sz[0] / 2, sz[1] / 2); break;
      case '-': case '_': zoomAt(0.8, sz[0] / 2, sz[1] / 2); break;
      case 'o': case 'O': showOutlines = !showOutlines; rebuildOverlay(); break;
      case 'l': case 'L': showNumbers = !showNumbers; draw(); break;
      case 't': case 'T':
        if (hoverLb) {
          var mi = C.mergeIndex(fe(), hoverLb);
          if (mi >= 0) { var lb = hoverLb; mutate(function (e) { return C.setTarget(e, mi, lb); }); }
        }
        break;
      default: return;
    }
    ev.preventDefault();
  }
  function drawSide() {
    var el = $('sideBody'); if (!el || !cur) { return; }
    var e = fe();
    $('b_done').checked = !!e.done;
    var h = '<div class="row"><b>' + esc(cur) + '</b>: ' + C.editCount(e) + ' edit(s)</div>';
    h += '<div class="row">DELETE: ' + (e.deleted.length ? e.deleted.map(function (lb) {
      return '<span class="badge">' + lb + ' <a href="#" data-undel="' + lb + '">x</a></span>';
    }).join(' ') : '<span class="small">none</span>') + '</div>';
    h += '<div class="row">MERGE:' + (e.merges.length ? '' : ' <span class="small">none</span>') + '</div>';
    e.merges.forEach(function (m, i) {
      h += '<div class="row small">' + m.labels.join(' + ') + ' &rarr; target <select data-mt="' + i + '">' +
        m.labels.map(function (lb) { return '<option' + (lb === m.target ? ' selected' : '') + '>' + lb + '</option>'; }).join('') +
        '</select> <button data-mrm="' + i + '">remove</button></div>';
    });
    if (D.live && D.llm.enabled) {
      h += '<div class="row"><button id="b_ask">Ask assistant for this field</button> <span id="askStatus" class="small"></span></div>';
    } else {
      h += '<div class="small row">Assistant suggestions: available only with <span class="mono">morphoagent ui serve</span> and ui.llm.enabled.</div>';
    }
    (S.suggestions[cur] || []).forEach(function (s, i) {
      h += '<div class="sug"><span class="badge sug">' + esc(s.kind) + '</span> <b>' + s.label + '</b>' +
        (s['with'] && s['with'].length ? ' with ' + s['with'].join(', ') : '') + ' - ' + esc(s.reason) + '<br>' +
        (s.status === 'open' ? '<button data-acc="' + i + '">Accept</button><button data-dis="' + i + '">Dismiss</button>' : '<i>' + esc(s.status) + '</i>') + '</div>';
    });
    el.innerHTML = h;
    el.querySelectorAll('[data-undel]').forEach(function (a) {
      a.onclick = function (ev) { ev.preventDefault(); var lb = +a.dataset.undel; mutate(function (x) { return C.toggleDelete(x, lb); }); };
    });
    el.querySelectorAll('[data-mt]').forEach(function (s) {
      s.onchange = function () { var i = +s.dataset.mt, t = +s.value; mutate(function (x) { return C.setTarget(x, i, t); }); };
    });
    el.querySelectorAll('[data-mrm]').forEach(function (b) {
      b.onclick = function () { var i = +b.dataset.mrm; mutate(function (x) { return C.removeMerge(x, i); }); };
    });
    el.querySelectorAll('[data-acc]').forEach(function (b) {
      b.onclick = function () {
        var s = S.suggestions[cur][+b.dataset.acc];
        var r = mutate(function (x) { return C.acceptSuggestion(x, s); });
        if (!r || r.ok !== false) { s.status = 'accepted'; persist(); drawSide(); draw(); }
      };
    });
    el.querySelectorAll('[data-dis]').forEach(function (b) {
      b.onclick = function () { S.suggestions[cur][+b.dataset.dis].status = 'dismissed'; persist(); drawSide(); draw(); };
    });
    if ($('b_ask')) { $('b_ask').onclick = askAssistant; }
    drawList();
  }
  function askAssistant() {
    var im = cur;
    $('askStatus').textContent = 'asking...';
    fetch('api/suggest/' + encodeURIComponent(im), { headers: apiHeaders() })
      .then(function (r) { return r.json().then(function (j) { return [r, j]; }); })
      .then(function (rj) {
        if (!rj[0].ok) { throw new Error(rj[1].error || ('HTTP ' + rj[0].status)); }
        var list = C.setSuggestions(S, im, rj[1].suggestions);
        persist();
        if (cur === im) { drawSide(); draw(); $('askStatus').textContent = list.length + ' suggestion(s)' + (rj[1].cached ? ' (cached)' : ''); }
      })
      .catch(function (e) { if ($('askStatus')) { $('askStatus').textContent = 'error: ' + e.message; } });
  }

  // ------------------------------------------------------------------ tab 4: export
  function render4() {
    var order = D.fields.map(function (f) { return f.image; });
    var rows = C.rows(S, order), csv = C.toCSV(rows);
    var h = '<div class="note">Step 8. <b>curation.csv</b> is read by <span class="mono">morphoagent curate</span> ' +
      '(columns operation,image,labels,target; one delete row per image, one row per merge, target = surviving label). ' +
      '<b>ui_state.json</b> restores this page (Import state).</div>';
    h += '<div class="row"><button id="x_csv">Download curation.csv</button> <button id="x_state">Download ui_state.json</button></div>';
    h += '<div class="row">Apply (from the folder with your config; add --overwrite to replace an earlier 08_curate):<pre id="x_cmd">morphoagent curate -c ' +
      esc(D.config_name) + ' --edits curation.csv --yes</pre></div>';
    h += '<h4>Edits (' + rows.length + ' rows, ' + C.totalEdits(S) + ' edits)</h4><table><tr><th>operation</th><th>image</th><th>labels</th><th>target</th></tr>' +
      rows.map(function (r) { return '<tr><td>' + esc(r.operation) + '</td><td>' + esc(r.image) + '</td><td class="mono">' + esc(r.labels) + '</td><td class="mono">' + esc(r.target) + '</td></tr>'; }).join('') +
      '</table><h4>curation.csv</h4><pre id="x_csvtext">' + esc(csv) + '</pre>';
    var notDone = D.fields.filter(function (f) { return !(S.fields[f.image] && S.fields[f.image].done); }).length;
    h += '<div class="row small">' + notDone + ' field(s) not marked done.</div>';
    h += '<h4>Config snippets</h4><pre id="x_yaml">' + esc((D.enhance.available ? enhanceYaml() : '') + (D.segment.available ? segmentYaml() : '')) + '</pre>';
    $('page').innerHTML = h;
    $('x_csv').onclick = function () { download('curation.csv', csv, 'text/csv'); };
    $('x_state').onclick = function () { saveNow(); download('ui_state.json', JSON.stringify(S, null, 1), 'application/json'); };
  }

  // ------------------------------------------------------------------ tab 5: decisions
  var DEC = D.decisions, onlyPending = true, offlineChoices = {};
  function pendingCount() { return DEC.proposals.filter(function (r) { return !r.decision; }).length; }
  function updateDecisionBadge() {
    var t = document.querySelector('.tab[data-tab="5"]');
    if (t) { t.textContent = '5. Decisions (' + pendingCount() + ' pending)'; }
  }
  function refreshDecisions() {
    if (!D.live) { return Promise.resolve(); }
    return fetch('api/proposals', { headers: apiHeaders() })
      .then(function (r) { if (!r.ok) { throw new Error('HTTP ' + r.status); } return r.json(); })
      .then(function (doc) { DEC = doc; updateDecisionBadge(); })
      .catch(function (e) { showMsg('could not load proposals: ' + e.message, 'bad'); });
  }
  function refLinks(refs) {
    return (refs || []).map(function (r) {
      var u = C.refUrl(r, DEC.reference_urls);
      return u ? '<a target="_blank" rel="noopener noreferrer" href="' + esc(u) + '">' + esc(r) + '</a>' : esc(r);
    }).join('; ');
  }
  function fmtCell(v) {  // non-integer numbers: 4 significant digits (the full value stays in the proposal file)
    return (typeof v === 'number' && isFinite(v) && Math.round(v) !== v) ? String(Number(v.toPrecision(4))) : v;
  }
  function rowsTable(rows, max, maxCols) {
    /* At most maxCols columns are visible; the others are in the table but hidden until
       "+N more columns" is clicked (class "xcol"), so nothing is dropped. */
    if (!rows || !rows.length) { return ''; }
    var cols = [];
    rows.forEach(function (r) { Object.keys(r).forEach(function (k) { if (cols.indexOf(k) < 0) { cols.push(k); } }); });
    var shown = rows.slice(0, max), extra = Math.max(0, cols.length - maxCols);
    function cls(j) { return j >= maxCols ? ' class="xcol"' : ''; }
    return '<div class="tw">' + (extra ? '<button class="small xmore" data-more="' + extra + '">+' + extra + ' more column(s)</button>' : '') +
      '<table class="small"><tr>' + cols.map(function (c, j) { return '<th' + cls(j) + '>' + esc(c).replace(/_/g, '_<wbr>') + '</th>'; }).join('') + '</tr>' +
      shown.map(function (r) { return '<tr>' + cols.map(function (c, j) { return '<td' + (j >= maxCols ? ' class="mono xcol"' : ' class="mono"') + '>' + esc(fmtCell(r[c])) + '</td>'; }).join('') + '</tr>'; }).join('') +
      '</table>' + (rows.length > max ? '<div class="small">' + (rows.length - max) + ' more row(s) in the proposal file</div>' : '') + '</div>';
  }
  function detailsBlock(label, body) {  // collapsed by default
    return '<details class="det small"><summary>' + esc(label) + '</summary>' + body + '</details>';
  }
  function numbersTable(nums) {
    var k = Object.keys(nums || {});
    if (!k.length) { return ''; }
    return '<table class="small">' + k.map(function (x) { return '<tr><th>' + esc(x) + '</th><td class="mono">' + esc(fmtCell(nums[x])) + '</td></tr>'; }).join('') + '</table>';
  }
  function cardHtml(r, i) {
    var p = r.proposal, dec = r.decision, off = offlineChoices[p.proposal_id];
    var h = '<div class="prop' + (dec ? ' decided' : '') + '" data-i="' + i + '"><div class="row"><span class="badge stage">' +
      esc((DEC.stage_titles || {})[p.stage] || p.stage) + '</span> <b>' + esc(p.title) + '</b> <span class="small mono">' + esc(p.proposal_id) +
      ' | ' + esc(p.created_utc) + '</span> ' + (dec ? '<span class="badge sel">decided: ' + esc(dec.choice) + '</span>' :
      '<span class="badge pend">pending</span>') + (off ? ' <span class="badge">downloaded: ' + esc(off) + '</span>' : '') + '</div>';
    if (p.rationale) { h += '<div class="note"><b>Agent rationale.</b> ' + esc(p.rationale) + '</div>'; }
    if (p.evidence && p.evidence.length) {
      h += '<div class="small"><b>Evidence</b><ul>' + p.evidence.map(function (e) {
        return '<li>' + esc(e.text) + (e.tool ? ' <span class="mono">[' + esc(e.tool) + ']</span>' : '') +
          (e.path ? ' <span class="mono">' + esc(e.path) + '</span>' : '') + (e.references && e.references.length ? ' ' + refLinks(e.references) : '') + '</li>';
      }).join('') + '</ul></div>';
    }
    var prov = p.provenance || {};
    if (prov.tool) {
      h += detailsBlock('Details: tool ' + prov.tool, '<pre class="small prov">' + esc(JSON.stringify(prov, null, 1)) + '</pre>');
    }
    h += '<div class="opts">';
    p.options.forEach(function (o, j) {
      var chosen = dec && dec.choice === o.id;
      h += '<div class="opt' + (p.recommended === o.id ? ' rec' : '') + (chosen ? ' chosen' : '') + '"><div><b>' + esc(o.label) + '</b> <span class="mono small">' + esc(o.id) + '</span>' +
        (p.recommended === o.id ? '<span class="badge def">recommended</span>' : '') + (chosen ? '<span class="badge sel">chosen</span>' : '') + '</div>' +
        (o.thumbs || []).map(function (t, k) { return /^data:image\//.test(t) ? '<img class="thumb" data-i="' + i + '" data-o="' + j + '" data-k="' + k + '" src="' + t + '" alt="figure">' : ''; }).join('') +
        (o.description ? '<div class="small">' + esc(o.description) + '</div>' : '') + numbersTable(o.numbers) +
        rowsTable(o.table, DEC.table_max_rows || 8, DEC.table_max_columns || 6) +
        (o.references && o.references.length ? '<div class="small">References: ' + refLinks(o.references) + '</div>' : '') +
        (Object.keys(o.config_changes || {}).length ? detailsBlock('Details: configuration changes (' + Object.keys(o.config_changes).length + ')',
          '<pre class="small">' + esc(JSON.stringify(o.config_changes, null, 1)) + '</pre>') : '');
      if (!dec) { h += '<div><button data-acc="' + i + '" data-o="' + j + '">Accept this option</button>'; }
      else { h += '<div>'; }
      h += '<button data-snap="' + i + '" data-o="' + j + '"' + (D.live ? '' : ' disabled title="needs morphoagent ui serve"') + '>Snapshot</button></div></div>';
    });
    h += '</div>';
    if (dec) {
      h += '<div class="row small">Decided ' + esc(dec.decided_utc) + ' | comment: ' + esc(dec.comment || '-') + ' | snapshot <span class="mono">' + esc(dec.snapshot || '-') + '</span></div>';
    } else {
      h += '<div class="row"><textarea data-com="' + i + '" rows="2" placeholder="Comment (saved with the decision)"></textarea>' +
        '<button data-rej="' + i + '">Reject all options</button> <button data-snap="' + i + '">Snapshot</button></div>';
    }
    return h + '</div>';
  }
  function render5() {
    var P = $('page');
    P.innerHTML = '<div class="small">loading proposals...</div>';
    refreshDecisions().then(drawDecisions);
  }
  function drawDecisions() {
    var P = $('page');
    var h = '<div class="note">Proposals written by the agent. Accept one option or reject the proposal; a decision is final and writes a ' +
      'snapshot (figure, source table, note). ' + (DEC.require_decisions ? 'Later stage tools refuse to run while a proposal of an earlier stage is pending. ' : '') +
      (D.live ? '' : 'Offline page: a decision is downloaded as <span class="mono">&lt;proposal_id&gt;.json</span>; record it with <span class="mono">morphoagent decision -c ' +
      esc(D.config_name) + ' --import &lt;file&gt;</span>. Snapshots need <span class="mono">morphoagent ui serve</span>.') + '</div>';
    h += '<div class="row"><label><input type="checkbox" id="d_pend"' + (onlyPending ? ' checked' : '') + '> pending only</label> ' +
      (D.live ? '<button id="d_ref">Refresh</button>' : '') + ' <span class="small">' + DEC.proposals.length + ' proposal(s), ' + pendingCount() + ' pending</span></div>';
    var shown = 0;
    DEC.proposals.forEach(function (r, i) {
      if (onlyPending && r.decision) { return; }
      h += cardHtml(r, i); shown++;
    });
    if (!shown) { h += '<div class="small row">no proposal to show</div>'; }
    h += '<div id="d_big" hidden class="big"><img alt="figure"></div>';
    P.innerHTML = h;
    updateDecisionBadge();
    $('d_pend').onchange = function () { onlyPending = $('d_pend').checked; drawDecisions(); };
    if ($('d_ref')) { $('d_ref').onclick = function () { refreshDecisions().then(drawDecisions); }; }
    $('d_big').onclick = function () { $('d_big').hidden = true; };
    P.querySelectorAll('button.xmore').forEach(function (b) {
      b.onclick = function () {
        var w = b.parentNode, open = w.classList.toggle('showall');
        b.textContent = open ? 'fewer columns' : '+' + b.dataset.more + ' more column(s)';
      };
    });
    P.querySelectorAll('img.thumb').forEach(function (im) {
      im.onclick = function () { $('d_big').querySelector('img').src = im.src; $('d_big').hidden = false; };
    });
    function comment(i) { var t = P.querySelector('[data-com="' + i + '"]'); return t ? t.value : ''; }
    P.querySelectorAll('[data-acc]').forEach(function (b) {
      b.onclick = function () { var i = +b.dataset.acc, o = DEC.proposals[i].proposal.options[+b.dataset.o]; decideCard(i, o.id, comment(i)); };
    });
    P.querySelectorAll('[data-rej]').forEach(function (b) {
      b.onclick = function () { var i = +b.dataset.rej; decideCard(i, 'rejected', comment(i)); };
    });
    P.querySelectorAll('[data-snap]').forEach(function (b) {
      b.onclick = function () {
        var i = +b.dataset.snap, r = DEC.proposals[i];
        var o = b.dataset.o !== undefined ? r.proposal.options[+b.dataset.o].id : null;
        postJson('api/snapshot', { proposal_id: r.proposal.proposal_id, option_id: o, comment: comment(i) })
          .then(function (res) { showMsg('snapshot written: ' + res.snapshot, 'ok'); })
          .catch(function (e) { showMsg('snapshot failed: ' + e.message, 'bad'); });
      };
    });
  }
  function postJson(url, obj) {
    return fetch(url, { method: 'POST', headers: apiHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(obj) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) { throw new Error(j.error || ('HTTP ' + r.status)); } return j; }); });
  }
  function decideCard(i, choice, text) {
    var r = DEC.proposals[i], p = r.proposal;
    var what = choice === 'rejected' ? 'Reject all options of' : 'Accept option "' + choice + '" of';
    if (!window.confirm(what + ' "' + p.title + '"? A decision cannot be changed.')) { return; }
    if (!D.live) {
      var doc = C.decisionDoc(r, choice, text, new Date().toISOString(), D.morphoagent_version);
      download(p.proposal_id + '.json', JSON.stringify(doc, null, 1), 'application/json');
      offlineChoices[p.proposal_id] = choice;
      showMsg('downloaded ' + p.proposal_id + '.json; run: morphoagent decision -c ' + D.config_name + ' --import ' + p.proposal_id + '.json', 'ok');
      drawDecisions();
      return;
    }
    postJson('api/decide', { proposal_id: p.proposal_id, choice: choice, comment: text, proposal_sha256: r.sha256 })
      .then(function (res) { showMsg('decision saved (' + res.choice + '); snapshot ' + res.snapshot, 'ok'); return refreshDecisions(); })
      .then(drawDecisions)
      .catch(function (e) { showMsg('decision not saved: ' + e.message, 'bad'); refreshDecisions().then(drawDecisions); });
  }

  // ------------------------------------------------------------------ boot
  setupTop();
  updateDecisionBadge();
  loadState().then(function () { render(); saveNow(); });
})();
