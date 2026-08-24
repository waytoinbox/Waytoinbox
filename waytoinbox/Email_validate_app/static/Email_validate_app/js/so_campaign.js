/*
 * so_campaign.js — Sales Outreach "New Campaign" page.
 *
 * Lives in a static file rather than inline in the template on purpose: the
 * sequence editor deals in merge tags like {{first_name}}, and Django's template
 * lexer rewrites those if they appear in template source. (That bug is live in
 * i_SO_Email_Creator.html's refreshPreview.)
 */
var SOCampaignPage = (function () {
  'use strict';

  var CFG = {};
  var MAX_STEPS = 10, MAX_VARIANTS = 4, LABELS = 'ABCD';
  var MAX_SUBSEQ = 3, SUBSEQ_MAX_STEPS = 5, SUBSEQ_MAX_VARIANTS = 4;

  var SEQ = {
    campaignId: 0,
    steps: [],
    sel: { step: 0, variant: 0 },
    dirty: false, saving: false, seq: 0, pendingDelete: null
  };

  // Subsequences — chained "if no reply after N days, branch onto this
  // track" follow-ups (Step 4). `expanded` is the index of the one subsequence
  // currently being edited (accordion — only one rail+editor DOM exists, it's
  // re-pointed at whichever subsequence is expanded, same shape as SEQ.sel).
  var SUBSEQ = { list: [], expanded: -1, sel: { step: 0, variant: 0 } };

  var quill = null, sourceMode = false, autosaveTimer = null, scoreTimer = null, estimateTimer = null;
  var subQuill = null;
  var TAGS = [];

  /* ── helpers ─────────────────────────────────────────────────────────── */
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function uid(p) { return p + '_' + Math.random().toString(36).slice(2, 9); }
  function toast(msg, kind) {
    if (window.WTI && WTI.toast) WTI.toast(msg, kind || 'success');
  }
  function blankVariant(label) {
    return { cid: uid('v'), id: null, label: label, name: 'Variation ' + label,
             subject: '', preheader: '', html: '' };
  }
  function curStep()    { return SEQ.steps[SEQ.sel.step]; }
  function curVariant() { var s = curStep(); return s ? s.variants[SEQ.sel.variant] : null; }

  function blankSubsequence(name) {
    return { cid: uid('sub'), id: null, name: name || '', triggerDays: 3, isActive: true,
             steps: [{ cid: uid('s'), id: null, waitDays: 0, variants: [blankVariant('A')] }] };
  }
  function curSub()         { return SUBSEQ.list[SUBSEQ.expanded]; }
  function curSubStep()     { var sub = curSub(); return sub ? sub.steps[SUBSEQ.sel.step] : null; }
  function curSubVariant()  { var s = curSubStep(); return s ? s.variants[SUBSEQ.sel.variant] : null; }

  /* ── dirty / autosave ────────────────────────────────────────────────── */
  function setStatus(text, unsaved) {
    var el = $('socSaveStatus');
    if (el) el.innerHTML = 'Status: <b class="' + (unsaved ? 'unsaved' : '') + '">' + esc(text) + '</b>';
  }
  function markDirty() {
    SEQ.dirty = true;
    setStatus('Unsaved changes…', true);
    clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(autosave, 2500);
  }

  function collectPayload(action) {
    commitEditor();
    commitSubEditor();
    return {
      campaign_id: SEQ.campaignId,
      action: action,
      name: ($('socName').value || '').trim(),
      recipient_list_ids:    comboIds('socRdList', 'list'),
      recipient_segment_ids: comboIds('socRdList', 'segment'),
      exclude_list_ids:      comboIds('socRdExcList', 'list'),
      exclude_segment_ids:   comboIds('socRdExcList', 'segment'),
      email_account_ids:     accountIds(),
      sender_name: ($('socSenderName') ? $('socSenderName').value.trim() : ''),
      reply_to:    ($('socReplyTo') ? $('socReplyTo').value.trim() : ''),
      schedule_date: $('socScheduleDate') ? $('socScheduleDate').value : '',
      schedule_time: (function () {
        try { return WTITZ.get24hrTime(TZCFG); } catch (e) { return ''; }
      })(),
      schedule_timezone: $('socCampaignTz') ? $('socCampaignTz').value : 'Asia/Kolkata',
      send_window_enabled: !!($('socWindowToggle') && $('socWindowToggle').checked),
      send_weekdays: Array.prototype.map.call(
        document.querySelectorAll('#socDayChips .soc-day-chip.active'),
        function (c) { return c.dataset.day; }
      ),
      send_hour_start: (function () {
        try { return WTITZ.get24hrTime({ hour: 'socWindowStartHour', minute: 'socWindowStartMinute', ampm: 'socWindowStartAmPm' }); }
        catch (e) { return ''; }
      })(),
      send_hour_end: (function () {
        try { return WTITZ.get24hrTime({ hour: 'socWindowEndHour', minute: 'socWindowEndMinute', ampm: 'socWindowEndAmPm' }); }
        catch (e) { return ''; }
      })(),
      sequence: SEQ.steps.map(function (s, i) {
        return {
          client_id: s.cid, id: s.id, order: i,
          wait_days: i === 0 ? 0 : (s.waitDays || 0),
          wait_hours: 0,
          variants: s.variants.map(function (v) {
            return { client_id: v.cid, id: v.id, label: v.label, name: v.name,
                     subject: v.subject, preheader: v.preheader,
                     html_body: v.html, weight: 1 };
          })
        };
      }),
      subsequences: SUBSEQ.list.map(function (sub, i) {
        return {
          client_id: sub.cid, id: sub.id, name: sub.name, order: i,
          trigger_days: sub.triggerDays, is_active: sub.isActive,
          steps: sub.steps.map(function (s, j) {
            return {
              client_id: s.cid, id: s.id, order: j,
              wait_days: j === 0 ? 0 : (s.waitDays || 0),
              wait_hours: 0,
              variants: s.variants.map(function (v) {
                return { client_id: v.cid, id: v.id, label: v.label, name: v.name,
                         subject: v.subject, preheader: v.preheader,
                         html_body: v.html, weight: 1 };
              })
            };
          })
        };
      })
    };
  }

  function applyIdMap(map) {
    if (!map) return;
    SEQ.steps.forEach(function (s) {
      if (map[s.cid]) s.id = map[s.cid];
      s.variants.forEach(function (v) { if (map[v.cid]) v.id = map[v.cid]; });
    });
    SUBSEQ.list.forEach(function (sub) {
      if (map[sub.cid]) sub.id = map[sub.cid];
      sub.steps.forEach(function (s) {
        if (map[s.cid]) s.id = map[s.cid];
        s.variants.forEach(function (v) { if (map[v.cid]) v.id = map[v.cid]; });
      });
    });
  }

  function autosave() {
    if (SEQ.saving) { clearTimeout(autosaveTimer); autosaveTimer = setTimeout(autosave, 1200); return; }
    var name = ($('socName').value || '').trim();
    if (!name) { setStatus('Add a campaign name to save', true); return; }

    SEQ.saving = true;
    var mySeq = ++SEQ.seq;
    setStatus('Saving…', true);

    fetch(CFG.autosaveUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CFG.csrf },
      body: JSON.stringify(collectPayload('save_draft'))
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        SEQ.saving = false;
        if (mySeq !== SEQ.seq) return;            // a newer edit superseded this
        if (d.status !== 'ok') {
          setStatus('Could not save', true);
          return;
        }
        applyIdMap(d.id_map);
        if (!SEQ.campaignId && d.campaign_id) {
          SEQ.campaignId = d.campaign_id;
          history.replaceState(null, '', CFG.editBaseUrl + d.campaign_id + '/edit/');
        }
        SEQ.dirty = false;
        setStatus('Saved', false);
      })
      .catch(function () { SEQ.saving = false; setStatus('Offline — changes not saved', true); });
  }

  /* ── step rail ───────────────────────────────────────────────────────── */
  function renderRail() {
    var rail = $('socStepRail');
    rail.innerHTML = '';

    SEQ.steps.forEach(function (step, i) {
      var card = document.createElement('div');
      card.className = 'soc-step-card' + (i === SEQ.sel.step ? ' selected' : '');
      card.dataset.idx = i;

      var chips = step.variants.map(function (v, j) {
        var active = (i === SEQ.sel.step && j === SEQ.sel.variant) ? ' active' : '';
        return '<button type="button" class="soc-var-chip' + active + '" data-v="' + j + '">' + esc(v.label) + '</button>';
      }).join('');

      var waitRow = (i < SEQ.steps.length - 1)
        ? '<div class="soc-step-wait">Wait <input type="number" class="soc-wait-days" min="0" max="90" value="' +
          (SEQ.steps[i + 1].waitDays || 0) + '" data-waitfor="' + (i + 1) + '"/> Day, then</div>'
        : '';

      card.innerHTML =
        '<span class="soc-step-drag" draggable="true"><i class="fas fa-grip-vertical"></i></span>' +
        '<div class="soc-step-top">' +
          '<i class="fas fa-envelope-open-text"></i>' +
          '<span class="soc-step-title">Step ' + (i + 1) + '</span>' +
          '<span class="soc-pill">' + step.variants.length + ' variation' + (step.variants.length > 1 ? 's' : '') + '</span>' +
          '<button type="button" class="soc-step-del" title="Delete step"><i class="fas fa-trash"></i></button>' +
        '</div>' +
        '<div class="soc-var-tabs">' + chips +
          '<button type="button" class="soc-var-add" title="Add variation"' +
            (step.variants.length >= MAX_VARIANTS ? ' disabled' : '') + '>+</button>' +
          '<button type="button" class="soc-var-del" title="Remove selected variation"' +
            (step.variants.length <= 1 ? ' disabled' : '') + '>&minus;</button>' +
        '</div>' +
        '<input type="text" class="soc-var-name" maxlength="255" placeholder="Variation Name" value="' +
          esc(step.variants[i === SEQ.sel.step ? SEQ.sel.variant : 0].name) + '"/>' +
        waitRow;

      rail.appendChild(card);
    });

    $('socAddStep').disabled = SEQ.steps.length >= MAX_STEPS;
    renderTestTargets();
  }

  /* Selects `val` ("step:variant") in the socTestTarget single-select combo —
     updates the hidden input, the option list's .selected state, and the
     trigger's visible label. Shared by renderTestTargets() (after rebuilding
     the option list) and the "Test" toolbar button (which jumps straight to
     whichever step/variant is currently open in the editor). */
  function setTestTarget(val) {
    var hidden = $('socTestTarget'), list = $('socTestTargetList'), tags = $('socTestTargetTags');
    if (!hidden || !list || !tags) return;
    hidden.value = val;
    var selOpt = null;
    list.querySelectorAll('.soc-rd-option').forEach(function (o) {
      var isSel = o.dataset.value === val;
      o.classList.toggle('selected', isSel);
      if (isSel) selOpt = o;
    });
    tags.innerHTML = selOpt
      ? '<span class="soc-rd-tag-name">' + esc(selOpt.dataset.label) + '</span>'
      : '<span class="soc-rd-placeholder">Select a step &amp; variation</span>';
  }

  function renderTestTargets() {
    var hidden = $('socTestTarget'), list = $('socTestTargetList');
    if (!hidden || !list) return;
    var prev = hidden.value;
    list.innerHTML = '';
    var values = [];
    SEQ.steps.forEach(function (s, i) {
      s.variants.forEach(function (v, j) {
        var val = i + ':' + j;
        var label = 'Step ' + (i + 1) + v.label + (v.subject ? ' — ' + v.subject.slice(0, 40) : '');
        values.push(val);
        var opt = document.createElement('div');
        opt.className = 'soc-rd-option';
        opt.dataset.value = val;
        opt.dataset.label = label;
        opt.innerHTML = '<span class="soc-rd-name">' + esc(label) + '</span>';
        list.appendChild(opt);
      });
    });
    setTestTarget(prev && values.indexOf(prev) !== -1 ? prev : (SEQ.sel.step + ':' + SEQ.sel.variant));
  }

  /* Open/close/select wiring for the socTestTarget single-select combo —
     same .soc-rd trigger/panel shell as Include/Exclude/Send From, but no
     search box (the step/variant list is short) and clicking an option
     selects-and-closes instead of toggling a checkbox (there's only ever
     one step/variant under test). */
  function wireTestTargetCombo() {
    var root = $('socTestTargetRoot'), trigger = $('socTestTargetTrigger'), panel = $('socTestTargetPanel'),
        list = $('socTestTargetList');
    if (!root) return;

    function open()  { panel.classList.add('open'); trigger.classList.add('open');
                       trigger.setAttribute('aria-expanded', 'true'); }
    function close() { panel.classList.remove('open'); trigger.classList.remove('open');
                       trigger.setAttribute('aria-expanded', 'false'); }

    trigger.addEventListener('click', function () {
      panel.classList.contains('open') ? close() : open();
    });
    trigger.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); panel.classList.contains('open') ? close() : open(); }
      if (e.key === 'Escape') close();
    });
    document.addEventListener('click', function (e) { if (!root.contains(e.target)) close(); });
    list.addEventListener('click', function (e) {
      var opt = e.target.closest('.soc-rd-option');
      if (!opt) return;
      setTestTarget(opt.dataset.value);
      close();
    });
  }

  /* ── subsequences (Step 4) ───────────────────────────────────────────────
     Adapted duplicate of the step-rail/editor pattern above, not a shared
     refactor — SUBSEQ.list holds several independent step-collections (one
     per subsequence) instead of SEQ's single one, but only the "expanded"
     (currently being edited) subsequence has a live rail+editor DOM; the
     others show as a collapsed summary card. See renderSubseqList(). */

  function renderSubseqList() {
    var wrap = $('socSubseqList');
    if (!SUBSEQ.list.length) {
      wrap.innerHTML =
        '<div class="so-empty" style="padding:20px 0;border:none;">' +
          '<i class="fas fa-code-branch" style="font-size:22px;color:var(--ink-3);"></i>' +
          '<p style="margin-top:8px;">No subsequences yet — add one to follow up automatically when a prospect doesn\'t reply.</p>' +
        '</div>';
    } else {
      wrap.innerHTML = SUBSEQ.list.map(function (sub, i) {
        var n = sub.steps.length;
        return (
          '<div class="soc-subseq-card' + (i === SUBSEQ.expanded ? ' expanded' : '') + '" data-idx="' + i + '">' +
            '<div class="soc-subseq-card-head">' +
              '<i class="fas fa-code-branch"></i>' +
              '<input type="text" class="soc-subseq-name" maxlength="255" placeholder="Subsequence ' + (i + 1) + '" value="' + esc(sub.name) + '"/>' +
              '<span class="soc-subseq-trigger">No reply after <input type="number" class="soc-subseq-days" min="1" max="30" value="' + sub.triggerDays + '"/> day(s)</span>' +
              '<span class="soc-pill">' + n + ' step' + (n > 1 ? 's' : '') + '</span>' +
              '<button type="button" class="soc-subseq-toggle" title="' + (i === SUBSEQ.expanded ? 'Collapse' : 'Edit steps') + '">' +
                '<i class="fas fa-chevron-' + (i === SUBSEQ.expanded ? 'up' : 'down') + '"></i></button>' +
              '<button type="button" class="soc-subseq-del" title="Delete subsequence"><i class="fas fa-trash"></i></button>' +
            '</div>' +
          '</div>'
        );
      }).join('');
    }
    $('socAddSubseq').disabled = SUBSEQ.list.length >= MAX_SUBSEQ;

    var show = SUBSEQ.expanded >= 0 && SUBSEQ.list[SUBSEQ.expanded];
    $('socSubseqEditorCard').hidden = !show;
    if (show) {
      var sub = SUBSEQ.list[SUBSEQ.expanded];
      $('socSubseqEditorTitle').textContent = 'Editing: ' + (sub.name || ('Subsequence ' + (SUBSEQ.expanded + 1)));
      renderSubRail();
      loadSubEditor();
    }
  }

  function wireSubseqList() {
    var wrap = $('socSubseqList');
    wrap.addEventListener('click', function (e) {
      var card = e.target.closest('.soc-subseq-card');
      if (!card) return;
      var idx = +card.dataset.idx;

      if (e.target.closest('.soc-subseq-del')) {
        if (!window.confirm('Delete this subsequence and its steps?')) return;
        SUBSEQ.list.splice(idx, 1);
        if (SUBSEQ.expanded === idx) SUBSEQ.expanded = -1;
        else if (SUBSEQ.expanded > idx) SUBSEQ.expanded -= 1;
        renderSubseqList(); markDirty();
        return;
      }
      if (e.target.closest('.soc-subseq-name') || e.target.closest('.soc-subseq-days')) return;

      commitSubEditor();
      SUBSEQ.expanded = (SUBSEQ.expanded === idx) ? -1 : idx;
      SUBSEQ.sel = { step: 0, variant: 0 };
      renderSubseqList();
    });
    wrap.addEventListener('input', function (e) {
      var card = e.target.closest('.soc-subseq-card');
      if (!card) return;
      var idx = +card.dataset.idx;
      if (e.target.classList.contains('soc-subseq-name')) {
        SUBSEQ.list[idx].name = e.target.value; markDirty();
      }
      if (e.target.classList.contains('soc-subseq-days')) {
        SUBSEQ.list[idx].triggerDays = Math.max(1, Math.min(30, parseInt(e.target.value, 10) || 3));
        markDirty();
      }
    });
    $('socAddSubseq').addEventListener('click', function () {
      if (SUBSEQ.list.length >= MAX_SUBSEQ) return;
      SUBSEQ.list.push(blankSubsequence('Subsequence ' + (SUBSEQ.list.length + 1)));
      SUBSEQ.expanded = SUBSEQ.list.length - 1;
      SUBSEQ.sel = { step: 0, variant: 0 };
      renderSubseqList();
      markDirty();
    });
  }

  function renderSubRail() {
    var sub = curSub();
    if (!sub) return;
    var rail = $('socSubStepRail');
    rail.innerHTML = '';

    sub.steps.forEach(function (step, i) {
      var card = document.createElement('div');
      card.className = 'soc-step-card' + (i === SUBSEQ.sel.step ? ' selected' : '');
      card.dataset.idx = i;

      var chips = step.variants.map(function (v, j) {
        var active = (i === SUBSEQ.sel.step && j === SUBSEQ.sel.variant) ? ' active' : '';
        return '<button type="button" class="soc-var-chip' + active + '" data-v="' + j + '">' + esc(v.label) + '</button>';
      }).join('');

      var waitRow = (i < sub.steps.length - 1)
        ? '<div class="soc-step-wait">Wait <input type="number" class="soc-wait-days" min="0" max="90" value="' +
          (sub.steps[i + 1].waitDays || 0) + '" data-waitfor="' + (i + 1) + '"/> Day, then</div>'
        : '';

      card.innerHTML =
        '<span class="soc-step-drag" draggable="true"><i class="fas fa-grip-vertical"></i></span>' +
        '<div class="soc-step-top">' +
          '<i class="fas fa-envelope-open-text"></i>' +
          '<span class="soc-step-title">Step ' + (i + 1) + '</span>' +
          '<span class="soc-pill">' + step.variants.length + ' variation' + (step.variants.length > 1 ? 's' : '') + '</span>' +
          '<button type="button" class="soc-step-del" title="Delete step"><i class="fas fa-trash"></i></button>' +
        '</div>' +
        '<div class="soc-var-tabs">' + chips +
          '<button type="button" class="soc-var-add" title="Add variation"' +
            (step.variants.length >= SUBSEQ_MAX_VARIANTS ? ' disabled' : '') + '>+</button>' +
          '<button type="button" class="soc-var-del" title="Remove selected variation"' +
            (step.variants.length <= 1 ? ' disabled' : '') + '>&minus;</button>' +
        '</div>' +
        '<input type="text" class="soc-var-name" maxlength="255" placeholder="Variation Name" value="' +
          esc(step.variants[i === SUBSEQ.sel.step ? SUBSEQ.sel.variant : 0].name) + '"/>' +
        waitRow;

      rail.appendChild(card);
    });

    $('socSubAddStep').disabled = sub.steps.length >= SUBSEQ_MAX_STEPS;
  }

  function wireSubRail() {
    var rail = $('socSubStepRail');

    rail.addEventListener('click', function (e) {
      var sub = curSub();
      if (!sub) return;
      var card = e.target.closest('.soc-step-card');
      if (!card) return;
      var idx = +card.dataset.idx;

      if (e.target.closest('.soc-step-del')) {
        if (sub.steps.length <= 1) { toast('A subsequence needs at least one step.', 'warning'); return; }
        if (!window.confirm('Delete step ' + (idx + 1) + ' and its variation(s)?')) return;
        sub.steps.splice(idx, 1);
        SUBSEQ.sel = { step: Math.max(0, Math.min(SUBSEQ.sel.step, sub.steps.length - 1)), variant: 0 };
        renderSubRail(); loadSubEditor(); markDirty();
        return;
      }
      if (e.target.closest('.soc-var-add')) {
        var st = sub.steps[idx];
        if (st.variants.length >= SUBSEQ_MAX_VARIANTS) return;
        commitSubEditor();
        var src = st.variants[SUBSEQ.sel.step === idx ? SUBSEQ.sel.variant : 0];
        var nv = blankVariant(LABELS[st.variants.length]);
        nv.subject = src.subject; nv.html = src.html; nv.name = (src.name || 'Variation') + ' (copy)';
        st.variants.push(nv);
        SUBSEQ.sel = { step: idx, variant: st.variants.length - 1 };
        renderSubRail(); loadSubEditor(); markDirty();
        return;
      }
      if (e.target.closest('.soc-var-del')) {
        var s2 = sub.steps[idx];
        if (s2.variants.length <= 1) return;
        commitSubEditor();
        var vi = (SUBSEQ.sel.step === idx) ? SUBSEQ.sel.variant : 0;
        s2.variants.splice(vi, 1);
        s2.variants.forEach(function (v, k) { v.label = LABELS[k]; });
        SUBSEQ.sel = { step: idx, variant: Math.max(0, vi - 1) };
        renderSubRail(); loadSubEditor(); markDirty();
        return;
      }
      var chip = e.target.closest('.soc-var-chip');
      if (chip) {
        commitSubEditor();
        SUBSEQ.sel = { step: idx, variant: +chip.dataset.v };
        renderSubRail(); loadSubEditor();
        return;
      }
      if (e.target.closest('.soc-var-name') || e.target.closest('.soc-wait-days')) return;

      if (idx !== SUBSEQ.sel.step) {
        commitSubEditor();
        SUBSEQ.sel = { step: idx, variant: 0 };
        renderSubRail(); loadSubEditor();
      }
    });

    rail.addEventListener('input', function (e) {
      var sub = curSub();
      if (!sub) return;
      var card = e.target.closest('.soc-step-card');
      if (!card) return;
      var idx = +card.dataset.idx;
      if (e.target.classList.contains('soc-var-name')) {
        var vi = (SUBSEQ.sel.step === idx) ? SUBSEQ.sel.variant : 0;
        sub.steps[idx].variants[vi].name = e.target.value;
        markDirty();
      }
      if (e.target.classList.contains('soc-wait-days')) {
        var target = +e.target.dataset.waitfor;
        var val = Math.max(0, Math.min(90, parseInt(e.target.value, 10) || 0));
        if (sub.steps[target]) { sub.steps[target].waitDays = val; markDirty(); }
      }
    });

    $('socSubAddStep').addEventListener('click', function () {
      var s = curSub();
      if (!s || s.steps.length >= SUBSEQ_MAX_STEPS) return;
      commitSubEditor();
      s.steps.push({ cid: uid('s'), id: null, waitDays: 3, variants: [blankVariant('A')] });
      SUBSEQ.sel = { step: s.steps.length - 1, variant: 0 };
      renderSubRail(); loadSubEditor(); markDirty();
    });
  }

  function wireSubDrag() {
    var rail = $('socSubStepRail'), dragIdx = null;

    rail.addEventListener('dragstart', function (e) {
      var handle = e.target.closest('.soc-step-drag');
      if (!handle) { e.preventDefault(); return; }
      var card = handle.closest('.soc-step-card');
      dragIdx = +card.dataset.idx;
      card.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', String(dragIdx));
    });
    rail.addEventListener('dragover', function (e) {
      if (dragIdx === null) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
    });
    rail.addEventListener('drop', function (e) {
      if (dragIdx === null) return;
      e.preventDefault();
      var sub = curSub();
      if (!sub) return;
      var card = e.target.closest('.soc-step-card');
      var to = card ? +card.dataset.idx : sub.steps.length - 1;
      if (to !== dragIdx) {
        commitSubEditor();
        var moved = sub.steps.splice(dragIdx, 1)[0];
        sub.steps.splice(to, 0, moved);
        SUBSEQ.sel = { step: to, variant: 0 };
        renderSubRail(); loadSubEditor(); markDirty();
      }
      dragIdx = null;
    });
    rail.addEventListener('dragend', function () {
      dragIdx = null;
      rail.querySelectorAll('.dragging').forEach(function (c) { c.classList.remove('dragging'); });
    });
  }

  function commitSubEditor() {
    var v = curSubVariant();
    if (!v) return;
    v.subject = $('socSubSubject').value;
    if (subQuill) v.html = emailSafeHtml(subQuill.root.innerHTML);
  }

  function loadSubEditor() {
    var step = curSubStep(), v = curSubVariant();
    if (!step || !v) return;
    $('socSubEditorBadge').textContent = 'Step ' + (SUBSEQ.sel.step + 1) + v.label;
    $('socSubSubject').value = v.subject || '';
    if (subQuill) {
      subQuill.setContents(subQuill.clipboard.convert({ html: v.html || '' }), 'silent');
      subQuill.history.clear();
    }
    updateSubCharCount();
  }

  function updateSubCharCount() {
    var n = subQuill ? subQuill.getText().trim().length : 0;
    $('socSubCharCount').textContent = 'Characters : ' + n;
  }

  function initSubQuill() {
    if (typeof Quill === 'undefined') return;
    subQuill = new Quill('#socSubQuillEditor', {
      theme: 'snow',
      modules: { toolbar: '#socSubQuillToolbar' },
      placeholder: 'Write the follow-up email…',
    });
    subQuill.on('text-change', function (_d, _o, source) {
      if (source === 'user') { markDirty(); updateSubCharCount(); }
    });
  }

  function wireSubEditor() {
    $('socSubSubject').addEventListener('input', function () {
      var v = curSubVariant(); if (v) v.subject = this.value;
      markDirty();
    });
  }

  function wireSubMergeMenu() {
    var menu = $('socSubMergeMenu');
    menu.innerHTML = TAGS.map(function (t) {
      return '<button type="button" data-tag="' + esc(t.value) + '">' + esc(t.label) + '</button>';
    }).join('');
    $('socSubMergeBtn').addEventListener('click', function (e) {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
    });
    menu.addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (!b || !subQuill) return;
      var range = subQuill.getSelection(true);
      subQuill.insertText(range ? range.index : subQuill.getLength(), b.dataset.tag, 'user');
      menu.hidden = true;
      markDirty(); updateSubCharCount();
    });
    document.addEventListener('click', function () { menu.hidden = true; });
  }

  /* ── editor ──────────────────────────────────────────────────────────── */
  function commitEditor() {
    var v = curVariant();
    if (!v) return;
    v.subject   = $('socSubject').value;
    v.preheader = $('socPreheader').value;
    if (sourceMode)   v.html = $('socSourceArea').value;
    else if (quill)   v.html = emailSafeHtml(quill.root.innerHTML);
    // no editor available: leave v.html as-is rather than wiping the body
  }

  /* Quill's table module emits a bare <table>; email clients need explicit attrs. */
  function emailSafeHtml(html) {
    if (html.indexOf('<table') === -1) return html;
    var box = document.createElement('div');
    box.innerHTML = html;
    box.querySelectorAll('table').forEach(function (t) {
      t.setAttribute('role', 'presentation');
      t.setAttribute('cellpadding', '0');
      t.setAttribute('cellspacing', '0');
      t.setAttribute('border', '0');
      if (!t.style.width) t.style.width = '100%';
      if (!t.style.borderCollapse) t.style.borderCollapse = 'collapse';
      t.querySelectorAll('td, th').forEach(function (c) {
        if (!c.style.border)  c.style.border = '1px solid #dddddd';
        if (!c.style.padding) c.style.padding = '8px';
      });
    });
    return box.innerHTML;
  }

  function loadEditor() {
    var step = curStep(), v = curVariant();
    if (!step || !v) return;
    $('socEditorBadge').textContent = 'Step ' + (SEQ.sel.step + 1) + v.label;
    $('socSubject').value   = v.subject || '';
    $('socPreheader').value = v.preheader || '';
    $('socPreheader').hidden = !v.preheader;
    $('socPreheaderToggle').textContent = v.preheader ? 'Hide preheader' : 'Add preheader';

    if (sourceMode) toggleSource(false);
    if (quill) {
      quill.setContents(quill.clipboard.convert({ html: v.html || '' }), 'silent');
      quill.history.clear();               // undo must not cross a variation boundary
    } else if ($('socSourceArea')) {
      $('socSourceArea').value = v.html || '';   // editor-unavailable fallback
    }
    updateCharCount();
    scheduleScore();
  }

  function updateCharCount() {
    var n;
    if (sourceMode)   n = $('socSourceArea').value.length;
    else if (quill)   n = quill.getText().trim().length;
    else              n = 0;
    $('socCharCount').textContent = 'Characters : ' + n;
  }

  function toggleSource(force) {
    var want = (typeof force === 'boolean') ? force : !sourceMode;
    if (want === sourceMode || !quill) return;
    var ta = $('socSourceArea'), ed = $('socQuillEditor'), note = $('socSourceNote');
    if (want) {
      ta.value = emailSafeHtml(quill.root.innerHTML);
      ed.style.display = 'none'; ta.style.display = 'block'; note.style.display = 'block';
    } else {
      quill.setContents(quill.clipboard.convert({ html: ta.value }), 'silent');
      ed.style.display = ''; ta.style.display = 'none'; note.style.display = 'none';
    }
    sourceMode = want;
    $('socTbSource').classList.toggle('active', want);
    updateCharCount();
  }

  /* ── content score ───────────────────────────────────────────────────── */
  var lastScore = null;
  function scheduleScore() {
    clearTimeout(scoreTimer);
    scoreTimer = setTimeout(runScore, 1500);
  }
  function runScore() {
    var v = curVariant();
    if (!v) return;
    commitEditor();
    fetch(CFG.scoreUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CFG.csrf },
      body: JSON.stringify({ subject: v.subject, html_body: v.html })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status !== 'ok') return;
        lastScore = d;
        var pill = $('socScorePill');
        pill.textContent = d.label;
        pill.className = 'soc-score-pill ' + d.label.toLowerCase();
      })
      .catch(function () { /* score is advisory */ });
  }
  function showScoreModal() {
    if (!lastScore) { runScore(); return; }
    $('socScoreSummary').textContent =
      'Score ' + lastScore.score + ' — ' + lastScore.label + '. Lower is better.';
    $('socScoreReasons').innerHTML = lastScore.reasons.map(function (r) {
      var ico = r.severity === 'warn' ? 'fa-exclamation-triangle warn' : 'fa-info-circle info';
      return '<div class="soc-reason"><i class="fas ' + ico + '"></i><span>' + esc(r.text) + '</span></div>';
    }).join('');
    $('socScoreModal').classList.add('open');
  }

  /* ── recipient combobox ──────────────────────────────────────────────── */
  function comboIds(listId, type) {
    var list = $(listId);
    if (!list) return [];
    return Array.prototype.slice
      .call(list.querySelectorAll('.soc-rd-chk:checked'))
      .map(function (c) { return c.closest('.soc-rd-option'); })
      .filter(function (o) { return o.dataset.type === type; })
      .map(function (o) { return parseInt(o.dataset.id, 10); });
  }
  function accountIds() {
    return comboIds('socSenderList', 'account');
  }

  function makeCombo(ids, placeholder, onChange) {
    var root = $(ids.root), trigger = $(ids.trigger), panel = $(ids.panel),
        tags = $(ids.tags), search = $(ids.search), list = $(ids.list);
    if (!root) return null;
    var noRes = list.querySelector('.soc-rd-no-results');

    function open()  { panel.classList.add('open'); trigger.classList.add('open');
                       trigger.setAttribute('aria-expanded', 'true');
                       search.value = ''; filter(''); search.focus(); }
    function close() { panel.classList.remove('open'); trigger.classList.remove('open');
                       trigger.setAttribute('aria-expanded', 'false'); }

    function filter(q) {
      var visible = 0;
      list.querySelectorAll('.soc-rd-option').forEach(function (o) {
        var m = !q || o.dataset.label.toLowerCase().indexOf(q) >= 0;
        o.style.display = m ? '' : 'none';
        if (m) visible++;
      });
      list.querySelectorAll('.soc-rd-group').forEach(function (g) {
        var n = g.nextElementSibling, any = false;
        while (n && !n.classList.contains('soc-rd-group')) {
          if (n.classList.contains('soc-rd-option') && n.style.display !== 'none') any = true;
          n = n.nextElementSibling;
        }
        g.style.display = any ? '' : 'none';
      });
      if (noRes) noRes.hidden = visible !== 0;
    }

    function summary() {
      var checked = list.querySelectorAll('.soc-rd-chk:checked');
      if (!checked.length) {
        tags.innerHTML = '<span class="soc-rd-placeholder">' + esc(placeholder) + '</span>';
      } else {
        tags.innerHTML = Array.prototype.map.call(checked, function (c) {
          var o = c.closest('.soc-rd-option');
          return '<span class="soc-rd-tag"><span class="soc-rd-tag-name">' + esc(o.dataset.label) +
                 '</span><button type="button" class="soc-rd-tag-x" data-key="' +
                 esc(o.dataset.type + '__' + o.dataset.id) + '">&times;</button></span>';
        }).join('');
      }
      if (onChange) onChange();
    }

    trigger.addEventListener('click', function (e) {
      if (e.target.closest('.soc-rd-tag-x')) return;
      panel.classList.contains('open') ? close() : open();
    });
    trigger.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); panel.classList.contains('open') ? close() : open(); }
      if (e.key === 'Escape') close();
    });
    document.addEventListener('click', function (e) { if (!root.contains(e.target)) close(); });
    search.addEventListener('input', function () { filter(this.value.trim().toLowerCase()); });
    list.addEventListener('change', function (e) {
      if (e.target.classList.contains('soc-rd-chk')) { summary(); markDirty(); }
    });
    tags.addEventListener('click', function (e) {
      var x = e.target.closest('.soc-rd-tag-x');
      if (!x) return;
      e.stopPropagation();
      var opt = list.querySelector('.soc-rd-option[data-type="' + x.dataset.key.split('__')[0] +
                                   '"][data-id="' + x.dataset.key.split('__')[1] + '"]');
      if (opt) { opt.querySelector('.soc-rd-chk').checked = false; summary(); markDirty(); }
    });

    summary();
    return { summary: summary, list: list };
  }

  var includeCombo = null, excludeCombo = null, senderCombo = null;
  var lastEstimateCount = 0;

  /* Anything selected in Include cannot also be Excluded */
  function syncExclude() {
    if (!excludeCombo) return;
    var keys = {};
    $('socRdList').querySelectorAll('.soc-rd-chk:checked').forEach(function (c) {
      var o = c.closest('.soc-rd-option');
      keys[o.dataset.type + '__' + o.dataset.id] = true;
    });
    var resync = false;
    excludeCombo.list.querySelectorAll('.soc-rd-option').forEach(function (o) {
      var dis = !!keys[o.dataset.type + '__' + o.dataset.id];
      var chk = o.querySelector('.soc-rd-chk');
      o.classList.toggle('soc-rd-option--disabled', dis);
      chk.disabled = dis;
      if (dis && chk.checked) { chk.checked = false; resync = true; }
    });
    if (resync) excludeCombo.summary();
  }

  /* Combined daily_limit across every checked sender account, plus how long
     step 1 alone would take to reach the current recipient estimate. Recomputed
     on every sender-combo change and every refreshEstimate() completion — see
     wireCombos() and refreshEstimate() below. */
  function updateCapacityReadout() {
    var infoEl = $('socDailyLimitInfo');
    if (!infoEl) return;
    var list = $('socSenderList');
    var combined = 0, n = 0;
    if (list) {
      list.querySelectorAll('.soc-rd-chk:checked').forEach(function (c) {
        var o = c.closest('.soc-rd-option');
        combined += parseInt(o.dataset.dailyLimit, 10) || 0;
        n++;
      });
    }
    if (!n) {
      infoEl.innerHTML = '<i class="fas fa-info-circle"></i> Select one or more sending accounts above to see combined daily capacity.';
      return;
    }
    var html = '<i class="fas fa-info-circle"></i> ' + n + ' account' + (n > 1 ? 's' : '') +
               ' selected — up to <strong>' + combined.toLocaleString() + '</strong> emails/day combined.';
    if (lastEstimateCount > 0 && combined > 0) {
      var days = Math.ceil(lastEstimateCount / combined);
      html += ' Estimated <strong>~' + days + ' day' + (days > 1 ? 's' : '') +
              '</strong> for the first email to reach everyone.';
      if (days > 1) {
        html += '<br/><span style="color:var(--ink-3);">💡 Add another sending account to increase daily capacity and finish faster.</span>';
      }
    }
    infoEl.innerHTML = html;
  }

  /* ── Review & Launch summary ─────────────────────────────────────────── */
  /* Renders a live summary (Audience/Sequence/Subsequence/Sender/Capacity/
     Send option/Schedule/Timezone/Window) into whichever target element id
     is passed — used by both the Review & Test step and the Launch step's
     condensed recap. Purely reads state already tracked client-side for
     other purposes (accountIds(), lastEstimateCount, SEQ/SUBSEQ) — no new
     server round trip. */
  function renderReviewSummary(targetId) {
    var el = $(targetId);
    if (!el) return;

    var listCount = comboIds('socRdList', 'list').length + comboIds('socRdList', 'segment').length;
    var prospectCount = lastEstimateCount;
    var stepCount = SEQ.steps.length;

    var subCount = SUBSEQ.list.length;
    var subLabel = subCount
      ? subCount + ' configured (' + SUBSEQ.list.map(function (s) {
          return 'no reply after ' + (s.triggerDays || 3) + 'd';
        }).join(', ') + ')'
      : 'Not configured';

    var accIds = accountIds();
    var combined = 0;
    var senderList = $('socSenderList');
    if (senderList) {
      senderList.querySelectorAll('.soc-rd-chk:checked').forEach(function (c) {
        combined += parseInt(c.closest('.soc-rd-option').dataset.dailyLimit, 10) || 0;
      });
    }

    var sendOptEl = document.querySelector('input[name="socSendOption"]:checked');
    var isSchedule = !!(sendOptEl && sendOptEl.value === 'schedule');
    var sendOpt = isSchedule ? 'Schedule for Later' : 'Send Now';
    var scheduleText = '—';
    if (isSchedule) {
      var d = $('socScheduleDate') ? $('socScheduleDate').value : '';
      scheduleText = d || 'Not set yet';
    }
    var tz = $('socCampaignTz') ? $('socCampaignTz').value : 'Asia/Kolkata';
    var windowOn = !!($('socWindowToggle') && $('socWindowToggle').checked);
    var windowText = windowOn
      ? (Array.prototype.map.call(
          document.querySelectorAll('#socDayChips .soc-day-chip.active'),
          function (c) { return c.textContent; }
        ).join(', ') || 'No days selected')
      : 'Unrestricted (every day, all hours)';

    function row(label, value) {
      return '<div class="soc-review-row"><span class="soc-review-label">' + esc(label) +
             '</span><span class="soc-review-value">' + esc(value) + '</span></div>';
    }

    el.innerHTML =
      row('Audience', prospectCount + ' prospect' + (prospectCount === 1 ? '' : 's') +
          ' across ' + listCount + ' list/segment' + (listCount === 1 ? '' : 's')) +
      row('Sequence', stepCount + ' step' + (stepCount === 1 ? '' : 's')) +
      row('Subsequence', subLabel) +
      row('Sender Accounts', accIds.length + ' account' + (accIds.length === 1 ? '' : 's')) +
      row('Combined Daily Capacity', combined.toLocaleString() + '/day') +
      row('Send Option', sendOpt) +
      row('Schedule', scheduleText) +
      row('Timezone', tz) +
      row('Sending Window', windowText);
  }

  function refreshEstimate() {
    clearTimeout(estimateTimer);
    estimateTimer = setTimeout(function () {
      var inc = comboIds('socRdList', 'list').length + comboIds('socRdList', 'segment').length;
      var box = $('socEstimate');
      if (!inc) {
        box.hidden = true;
        lastEstimateCount = 0;
        updateCapacityReadout();
        return;
      }
      var body = new URLSearchParams({
        list_ids:            comboIds('socRdList', 'list').join(','),
        segment_ids:         comboIds('socRdList', 'segment').join(','),
        exclude_list_ids:    comboIds('socRdExcList', 'list').join(','),
        exclude_segment_ids: comboIds('socRdExcList', 'segment').join(',')
      });
      fetch(CFG.estimateUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': CFG.csrf },
        body: body.toString()
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          box.hidden = false;
          $('socEstimateCount').textContent = (d.count || 0).toLocaleString();
          lastEstimateCount = d.count || 0;
          updateCapacityReadout();
        })
        .catch(function () { box.hidden = true; });
    }, 400);
  }

  /* ── test send ───────────────────────────────────────────────────────── */
  function addTestRow(value) {
    var wrap = $('socTestRecipients');
    if (wrap.children.length >= 5) { toast('Maximum 5 test recipients.', 'warning'); return; }
    var row = document.createElement('div');
    row.className = 'soc-test-row';
    row.innerHTML = '<input type="email" placeholder="you@example.com" value="' + esc(value || '') + '"/>' +
                    '<button type="button" class="soc-test-remove" title="Remove"><i class="fas fa-times"></i></button>';
    row.querySelector('.soc-test-remove').addEventListener('click', function () {
      row.remove();
      if (!wrap.children.length) addTestRow('');
    });
    wrap.appendChild(row);
  }

  function sendTest() {
    commitEditor();
    var target = ($('socTestTarget').value || '0:0').split(':');
    var step = SEQ.steps[parseInt(target[0], 10)];
    var v = step && step.variants[parseInt(target[1], 10)];
    var res = $('socTestResult');
    if (!v) return;

    var emails = Array.prototype.map.call($('socTestRecipients').querySelectorAll('input'),
      function (i) { return i.value.trim(); }).filter(Boolean);
    if (!emails.length) { showTestResult(false, 'Add at least one recipient.'); return; }
    if (!accountIds().length) { showTestResult(false, 'Select an email account in Sender Settings.'); return; }
    if (!SEQ.campaignId) { showTestResult(false, 'Still saving your changes — try again in a moment.'); return; }

    var btn = $('socTestSend');
    btn.disabled = true;
    btn.innerHTML = '<span class="btn-spinner"></span> Sending…';
    fetch(CFG.testUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CFG.csrf },
      body: JSON.stringify({
        campaign_id: SEQ.campaignId,
        to_emails: emails, subject: v.subject, html_body: v.html,
        email_account_id: accountIds()[0],
        sender_name: $('socSenderName') ? $('socSenderName').value.trim() : ''
      })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-paper-plane"></i> Send Test';
        if (d.status === 'ok') showTestResult(true, 'Test sent to ' + d.sent + ' recipient(s).');
        else showTestResult(false, d.message || 'Send failed.');
      })
      .catch(function () {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-paper-plane"></i> Send Test';
        showTestResult(false, 'Network error.');
      });
  }
  function showTestResult(ok, msg) {
    var r = $('socTestResult');
    r.hidden = false;
    r.className = 'soc-test-result ' + (ok ? 'ok' : 'error');
    r.textContent = msg;
  }

  /* ── save / send ─────────────────────────────────────────────────────── */
  function clearErrors() {
    document.querySelectorAll('.soc-error').forEach(function (e) {
      e.textContent = ''; e.classList.remove('show');
    });
  }
  function showErrors(errors) {
    var map  = { name: 'err-socName', recipients: 'err-socRecipients', account: 'err-socAccount',
                sequence: 'err-socSequence', schedule: 'err-socSchedule',
                send_window: 'err-socWindow', subsequences: 'err-socSubsequences' };
    // Which wizard step each field lives on, so an error on a hidden panel is
    // actually visible rather than silently failing to scroll into view.
    // Steps: 1 Audience, 2 Sequence, 3 Subsequence, 4 Settings, 5 Review & Test, 6 Launch.
    var step = { name: 1, recipients: 1, account: 4, sequence: 2, schedule: 4, send_window: 4, subsequences: 3 };
    var first = null, firstStep = null;
    Object.keys(errors).forEach(function (k) {
      var el = $(map[k] || 'err-socName');
      if (el) {
        el.textContent = errors[k]; el.classList.add('show');
        if (!first) { first = el; firstStep = step[k] || 1; }
      }
    });
    if (firstStep && window.SOWizard) window.SOWizard.goToStep(firstStep);
    if (first) first.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function submit(action, btn) {
    clearErrors();
    clearTimeout(autosaveTimer);
    var label = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="btn-spinner"></span> Working…';

    fetch(CFG.saveUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CFG.csrf },
      body: JSON.stringify(collectPayload(action))
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        btn.disabled = false;
        btn.innerHTML = label;
        var d = res.d;
        if (d.status !== 'ok') { showErrors(d.errors || { name: d.message || 'Could not save.' }); return; }
        applyIdMap(d.id_map);
        SEQ.dirty = false;
        if (!SEQ.campaignId && d.campaign_id) {
          SEQ.campaignId = d.campaign_id;
          history.replaceState(null, '', CFG.editBaseUrl + d.campaign_id + '/edit/');
        }
        setStatus('Saved', false);
        if (action === 'save_draft') {
          toast('Draft saved.', 'success');
        } else {
          toast(action === 'schedule' ? 'Campaign scheduled.' : 'Campaign is sending.', 'success');
          setTimeout(function () { window.location.href = CFG.campaignsUrl; }, 700);
        }
      })
      .catch(function () {
        btn.disabled = false; btn.innerHTML = label;
        toast('Network error — please retry.', 'error');
      });
  }

  /* ── Quill ───────────────────────────────────────────────────────────── */
  /* Quill is loaded from a CDN. If that request is blocked (offline, proxy,
     ad-blocker) we must NOT let the failure cascade — the rest of the form has
     to keep working, so this degrades to the plain HTML source editor. */
  function editorUnavailable(reason) {
    var ed = $('socQuillEditor'), ta = $('socSourceArea'), tb = $('socQuillToolbar');
    if (tb) tb.style.display = 'none';
    if (ed) ed.style.display = 'none';
    if (ta) { ta.style.display = 'block'; }
    var note = $('socSourceNote');
    if (note) {
      note.style.display = 'block';
      note.innerHTML = '<i class="fas fa-exclamation-triangle"></i> Rich-text editor could not load (' +
                       esc(reason) + '). You can still write the email as HTML here, and every other ' +
                       'part of this page works normally.';
    }
    sourceMode = true;
    quill = null;
    if (ta) ta.addEventListener('input', function () { markDirty(); updateCharCount(); });
  }

  var SEQ_SIZE_STEPS = ['12px', '14px', '16px', '18px', '24px', '32px'];
  var SEQ_LINEHEIGHT_STEPS = ['1', '1.2', '1.5', '1.8', '2'];

  // Icon + value + up/down stepper for Font size / Line height — not a
  // Quill picker at all, just steps the current selection's format through
  // its whitelist and mirrors the new value in the small display span.
  // Same pattern as SO Inbox's Compose editor (so_inbox.js::stepFormat).
  function stepFormat(formatName, steps, dir, displayEl) {
    if (!quill) return;
    var range = quill.getSelection(true);
    if (!range) return;
    var current = quill.getFormat(range)[formatName] || steps[0];
    var idx = steps.indexOf(current);
    if (idx === -1) idx = 0;
    idx = dir === 'up' ? Math.min(steps.length - 1, idx + 1) : Math.max(0, idx - 1);
    quill.format(formatName, steps[idx]);
    if (displayEl) displayEl.textContent = steps[idx].replace('px', '');
    quill.focus();
  }

  function updateStepperDisplays() {
    if (!quill) return;
    var range = quill.getSelection();
    var fmt = range ? quill.getFormat(range) : {};
    $('socSeqSizeValue').textContent = (fmt.size || '14px').replace('px', '');
    $('socSeqLineHeightValue').textContent = fmt.lineheight || '1';
  }

  function initQuill() {
    if (typeof Quill === 'undefined') { editorUnavailable('script blocked or offline'); return; }
    var Size = Quill.import('attributors/style/size');
    Size.whitelist = SEQ_SIZE_STEPS;
    Quill.register(Size, true);
    Quill.register(Quill.import('attributors/style/color'), true);
    Quill.register(Quill.import('attributors/style/align'), true);

    // Line height — Parchment's class name differs between Quill 1.x and 2.x
    try {
      var Parchment = Quill.import('parchment');
      var StyleAttr = Parchment.StyleAttributor || (Parchment.Attributor && Parchment.Attributor.Style);
      if (StyleAttr) {
        var LineHeight = new StyleAttr('lineheight', 'line-height', {
          scope: Parchment.Scope.BLOCK, whitelist: SEQ_LINEHEIGHT_STEPS
        });
        Quill.register({ 'formats/lineheight': LineHeight }, true);
      }
    } catch (e) { /* line-height select degrades to a no-op */ }

    quill = new Quill('#socQuillEditor', {
      theme: 'snow',
      modules: {
        table: true,
        history: { delay: 800, maxStack: 200, userOnly: true },
        toolbar: { container: '#socQuillToolbar', handlers: { image: imageHandler } }
      }
    });

    updateStepperDisplays();
    quill.on('selection-change', updateStepperDisplays);
    quill.on('text-change', function (_d, _o, source) {
      if (source === 'user') markDirty();
      updateCharCount();
      scheduleScore();
      updateStepperDisplays();
    });

    $('socSeqSizeUp').addEventListener('click', function () { stepFormat('size', SEQ_SIZE_STEPS, 'up', $('socSeqSizeValue')); });
    $('socSeqSizeDown').addEventListener('click', function () { stepFormat('size', SEQ_SIZE_STEPS, 'down', $('socSeqSizeValue')); });
    $('socSeqLineHeightUp').addEventListener('click', function () { stepFormat('lineheight', SEQ_LINEHEIGHT_STEPS, 'up', $('socSeqLineHeightValue')); });
    $('socSeqLineHeightDown').addEventListener('click', function () { stepFormat('lineheight', SEQ_LINEHEIGHT_STEPS, 'down', $('socSeqLineHeightValue')); });

    // Insert-link tooltip — Quill positions it position:absolute relative to
    // the editor container using coordinates tied to exactly where the
    // selection sits, which can land it oddly placed. Centered above the
    // editor instead (fixed viewport coordinates), same fix as SO Inbox's
    // Compose editor (so_inbox.js::initComposeEditor).
    var tooltipEl = document.querySelector('#socQuillEditor .ql-tooltip');
    if (tooltipEl && typeof MutationObserver !== 'undefined') {
      new MutationObserver(function () {
        if (tooltipEl.classList.contains('ql-hidden')) return;
        var editorEl = document.querySelector('#socQuillEditor');
        var eRect = editorEl ? editorEl.getBoundingClientRect() : { left: 0, width: window.innerWidth, top: 0 };
        tooltipEl.style.position = 'fixed';
        tooltipEl.style.left = (eRect.left + eRect.width / 2) + 'px';
        tooltipEl.style.top = (eRect.top + 40) + 'px';
        tooltipEl.style.transform = 'translateX(-50%)';
        tooltipEl.style.margin = '0';
      }).observe(tooltipEl, { attributes: true, attributeFilter: ['class'] });
    }
  }

  function imageHandler() {
    var input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.onchange = function () {
      var file = input.files && input.files[0];
      if (!file) return;
      var fd = new FormData();
      fd.append('image', file);
      toast('Uploading image…', 'info');
      fetch(CFG.imageUrl, { method: 'POST', headers: { 'X-CSRFToken': CFG.csrf }, body: fd })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.status !== 'ok' || !d.url) { toast(d.message || 'Upload failed.', 'error'); return; }
          // Absolute URL — a relative src is dead once the mail leaves the app.
          var abs = new URL(d.url, window.location.origin).href;
          var range = quill.getSelection(true);
          quill.insertEmbed(range ? range.index : 0, 'image', abs, 'user');
          markDirty();
        })
        .catch(function () { toast('Upload failed.', 'error'); });
    };
    input.click();
  }

  /* ── popovers ────────────────────────────────────────────────────────── */
  var EMOJI = ('😀 😃 😄 😁 😊 😍 🤩 😎 🤝 👋 👍 👏 🙌 💪 🙏 🤔 😅 😉 🥳 🚀 ' +
               '✨ ⭐ 🔥 💡 🎯 📈 📊 💼 🏆 ✅ ❗ ❓ ⏰ 📅 📌 🔔 💬 📣 📩 📬 ' +
               '💰 💳 🎁 🎉 🧠 🛠️ ⚙️ 🔍 🔗 📎 📝 📄 🗂️ 🧩 🌟 ☀️ 🌍 ⚡ 🥇 🤖').split(' ');

  function positionPop(pop, anchor) {
    var r = anchor.getBoundingClientRect();
    pop.style.left = Math.max(8, Math.min(r.left + window.scrollX, window.innerWidth - 320)) + 'px';
    pop.style.top  = (r.bottom + window.scrollY + 6) + 'px';
  }
  function closePops() {
    ['socEmojiPop', 'socTagPop', 'socTablePop'].forEach(function (id) { $(id).classList.remove('open'); });
  }
  function togglePop(pop, anchor) {
    var wasOpen = pop.classList.contains('open');
    closePops();
    if (!wasOpen) { positionPop(pop, anchor); pop.classList.add('open'); }
  }

  function insertAtCursor(text) {
    if (!quill && !sourceMode) return;
    if (sourceMode) {
      var ta = $('socSourceArea'), p = ta.selectionStart || 0;
      ta.value = ta.value.slice(0, p) + text + ta.value.slice(ta.selectionEnd || p);
      ta.focus();
    } else {
      var range = quill.getSelection(true);
      quill.insertText(range ? range.index : 0, text, 'user');
    }
    markDirty();
    updateCharCount();
  }

  function buildPopovers() {
    $('socEmojiGrid').innerHTML = EMOJI.map(function (e) {
      return '<button type="button">' + e + '</button>';
    }).join('');
    $('socEmojiGrid').addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (b) { insertAtCursor(b.textContent); closePops(); }
    });

    $('socTagList').innerHTML = TAGS.map(function (t) {
      return '<button type="button" data-tag="' + esc(t.value) + '">' + esc(t.label) + '</button>';
    }).join('');
    $('socTagList').addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (b) { insertAtCursor(b.dataset.tag); closePops(); }
    });

    var grid = $('socTableGrid'), html = '';
    for (var r = 1; r <= 6; r++) {
      for (var c = 1; c <= 8; c++) html += '<span data-r="' + r + '" data-c="' + c + '"></span>';
    }
    grid.innerHTML = html;
    grid.addEventListener('mouseover', function (e) {
      var cell = e.target.closest('span');
      if (!cell) return;
      var R = +cell.dataset.r, C = +cell.dataset.c;
      grid.querySelectorAll('span').forEach(function (s) {
        s.classList.toggle('on', +s.dataset.r <= R && +s.dataset.c <= C);
      });
      $('socTableLbl').textContent = R + ' × ' + C;
    });
    grid.addEventListener('click', function (e) {
      var cell = e.target.closest('span');
      if (!cell) return;
      insertTable(+cell.dataset.r, +cell.dataset.c);
      closePops();
    });
  }

  function insertTable(rows, cols) {
    var mod = quill.getModule('table');
    if (mod && typeof mod.insertTable === 'function') {
      quill.focus();
      mod.insertTable(rows, cols);
      markDirty();
      return;
    }
    // Quill build without the table module — fall back to the source view
    toast('Tables are inserted through the HTML view in this editor build.', 'warning');
    toggleSource(true);
  }

  /* ── schedule ────────────────────────────────────────────────────────── */
  var TZCFG = {
    date: 'socScheduleDate', hour: 'socScheduleHour', minute: 'socScheduleMinute',
    ampm: 'socScheduleAmPm', hidden: 'socCampaignTz', trigger: 'socTzTrigger',
    dropdown: 'socTzDropdown', search: 'socTzSearch', list: 'socTzList',
    label: 'socTzLabelText', optClass: 'soc-sch-tz-opt'
  };

  function updateSendOptionUI() {
    var sched = document.querySelector('input[name="socSendOption"]:checked').value === 'schedule';
    $('socScheduleGroup').hidden = !sched;
    $('socSendBtn').textContent = sched ? 'Schedule Campaign' : 'Send Campaign';
  }

  /* Populate the Sending Days & Hours time selects, then either restore an
     existing campaign's saved window or apply a sensible default for a new
     one. Must run before wireForm() wires the toggle/chip click handlers, and
     after the <select> elements exist in the DOM (they do — this only needs
     to run after populateScheduleControls fills in their <option>s, done
     right here, not after any async work). */
  function initSendWindowControls() {
    WTITZ.populateScheduleControls({ hour: 'socWindowStartHour', minute: 'socWindowStartMinute' });
    WTITZ.populateScheduleControls({ hour: 'socWindowEndHour',   minute: 'socWindowEndMinute' });

    function setTime(prefix, hhmm) {
      var parts = (hhmm || '').split(':');
      var h24 = parseInt(parts[0], 10);
      if (isNaN(h24)) return false;
      var min = parts[1] || '00';
      var ampm = h24 >= 12 ? 'PM' : 'AM';
      var h12 = h24 % 12 || 12;
      var minRound = Math.round(parseInt(min, 10) / 5) * 5;
      if (minRound >= 60) minRound = 55;
      if ($(prefix + 'Hour'))   $(prefix + 'Hour').value   = h12;
      if ($(prefix + 'Minute')) $(prefix + 'Minute').value = String(minRound).padStart(2, '0');
      if ($(prefix + 'AmPm'))   $(prefix + 'AmPm').value   = ampm;
      return true;
    }

    var ed = $('socEditingData');
    if (ed) {
      var d = JSON.parse(ed.textContent);
      if ($('socWindowToggle')) $('socWindowToggle').checked = !!d.send_window_enabled;
      if ($('socWindowGroup'))  $('socWindowGroup').hidden = !d.send_window_enabled;
      (d.send_weekdays || []).forEach(function (day) {
        var chip = document.querySelector('#socDayChips .soc-day-chip[data-day="' + day + '"]');
        if (chip) chip.classList.add('active');
      });
      setTime('socWindowStart', d.send_hour_start);
      setTime('socWindowEnd', d.send_hour_end);
    } else {
      // New campaign: 9 AM - 6 PM is a nicer starting point than the
      // populate-default noon, and Mon-Fri is pre-checked — neither is saved
      // unless the user actually turns the toggle on and hits Save.
      setTime('socWindowStart', '09:00');
      setTime('socWindowEnd', '18:00');
      ['mon', 'tue', 'wed', 'thu', 'fri'].forEach(function (day) {
        var chip = document.querySelector('#socDayChips .soc-day-chip[data-day="' + day + '"]');
        if (chip) chip.classList.add('active');
      });
    }
  }

  /* ── drag reorder ────────────────────────────────────────────────────── */
  function wireDrag() {
    var rail = $('socStepRail'), dragIdx = null;

    rail.addEventListener('dragstart', function (e) {
      var handle = e.target.closest('.soc-step-drag');
      if (!handle) { e.preventDefault(); return; }
      var card = handle.closest('.soc-step-card');
      dragIdx = +card.dataset.idx;
      card.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', String(dragIdx));
    });
    rail.addEventListener('dragover', function (e) {
      if (dragIdx === null) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
    });
    rail.addEventListener('drop', function (e) {
      if (dragIdx === null) return;
      e.preventDefault();
      var card = e.target.closest('.soc-step-card');
      var to = card ? +card.dataset.idx : SEQ.steps.length - 1;
      if (to !== dragIdx) {
        commitEditor();
        var moved = SEQ.steps.splice(dragIdx, 1)[0];
        SEQ.steps.splice(to, 0, moved);
        SEQ.sel = { step: to, variant: 0 };
        renderRail(); loadEditor(); markDirty();
      }
      dragIdx = null;
    });
    rail.addEventListener('dragend', function () {
      dragIdx = null;
      rail.querySelectorAll('.dragging').forEach(function (c) { c.classList.remove('dragging'); });
    });
  }

  /* ── rail interactions ───────────────────────────────────────────────── */
  function wireRail() {
    var rail = $('socStepRail');

    rail.addEventListener('click', function (e) {
      var card = e.target.closest('.soc-step-card');
      if (!card) return;
      var idx = +card.dataset.idx;

      if (e.target.closest('.soc-step-del')) {
        if (SEQ.steps.length <= 1) { toast('A sequence needs at least one step.', 'warning'); return; }
        SEQ.pendingDelete = idx;
        $('socDelMsg').textContent = 'Step ' + (idx + 1) + ' and its ' +
          SEQ.steps[idx].variants.length + ' variation(s) will be removed.';
        $('socDelModal').classList.add('open');
        return;
      }
      if (e.target.closest('.soc-var-add')) {
        var st = SEQ.steps[idx];
        if (st.variants.length >= MAX_VARIANTS) return;
        commitEditor();
        var src = st.variants[SEQ.sel.step === idx ? SEQ.sel.variant : 0];
        var nv = blankVariant(LABELS[st.variants.length]);
        nv.subject = src.subject;                    // duplicate for easy A/B subject tests
        nv.html    = src.html;
        nv.name    = (src.name || 'Variation') + ' (copy)';
        st.variants.push(nv);
        SEQ.sel = { step: idx, variant: st.variants.length - 1 };
        renderRail(); loadEditor(); markDirty();
        return;
      }
      if (e.target.closest('.soc-var-del')) {
        var s2 = SEQ.steps[idx];
        if (s2.variants.length <= 1) return;
        commitEditor();
        var vi = (SEQ.sel.step === idx) ? SEQ.sel.variant : 0;
        s2.variants.splice(vi, 1);
        s2.variants.forEach(function (v, k) { v.label = LABELS[k]; });
        SEQ.sel = { step: idx, variant: Math.max(0, vi - 1) };
        renderRail(); loadEditor(); markDirty();
        return;
      }
      var chip = e.target.closest('.soc-var-chip');
      if (chip) {
        commitEditor();
        SEQ.sel = { step: idx, variant: +chip.dataset.v };
        renderRail(); loadEditor();
        return;
      }
      if (e.target.closest('.soc-var-name') || e.target.closest('.soc-wait-days')) return;

      if (idx !== SEQ.sel.step) {
        commitEditor();
        SEQ.sel = { step: idx, variant: 0 };
        renderRail(); loadEditor();
      }
    });

    rail.addEventListener('input', function (e) {
      var card = e.target.closest('.soc-step-card');
      if (!card) return;
      var idx = +card.dataset.idx;
      if (e.target.classList.contains('soc-var-name')) {
        var vi = (SEQ.sel.step === idx) ? SEQ.sel.variant : 0;
        SEQ.steps[idx].variants[vi].name = e.target.value;
        markDirty();
      }
      if (e.target.classList.contains('soc-wait-days')) {
        var target = +e.target.dataset.waitfor;
        var val = Math.max(0, Math.min(90, parseInt(e.target.value, 10) || 0));
        if (SEQ.steps[target]) { SEQ.steps[target].waitDays = val; markDirty(); }
      }
    });
  }

  /* ── init ────────────────────────────────────────────────────────────── */
  function hydrate() {
    var node = $('socEditingData');
    if (!node) {
      SEQ.steps = [{ cid: uid('s'), id: null, waitDays: 0, variants: [blankVariant('A')] }];
      SUBSEQ.list = [];   // opt-in — a new campaign starts with no subsequences
      return;
    }
    var d = JSON.parse(node.textContent);
    SEQ.campaignId = d.id;
    $('socName').value = d.name || '';
    if ($('socSenderName')) $('socSenderName').value = d.sender_name || '';
    if ($('socReplyTo'))    $('socReplyTo').value    = d.reply_to || '';
    function check(listId, type, ids) {
      var list = $(listId);
      if (!list) return;
      ids.forEach(function (id) {
        var o = list.querySelector('.soc-rd-option[data-type="' + type + '"][data-id="' + id + '"]');
        if (o) o.querySelector('.soc-rd-chk').checked = true;
      });
    }
    check('socRdList', 'list', d.list_ids);
    check('socRdList', 'segment', d.segment_ids);
    check('socRdExcList', 'list', d.exclude_list_ids);
    check('socRdExcList', 'segment', d.exclude_segment_ids);
    check('socSenderList', 'account', d.email_account_ids);
    if (d.exclude_list_ids.length || d.exclude_segment_ids.length) {
      $('socExclSection').hidden = false;
      $('socExclToggle').style.display = 'none';
    }

    SEQ.steps = (d.sequence || []).map(function (s, i) {
      return {
        cid: uid('s'), id: s.id, waitDays: s.wait_days,
        variants: (s.variants || []).map(function (v) {
          return { cid: uid('v'), id: v.id, label: v.label, name: v.name,
                   subject: v.subject, preheader: v.preheader, html: v.html_body };
        })
      };
    });
    if (!SEQ.steps.length) {
      SEQ.steps = [{ cid: uid('s'), id: null, waitDays: 0, variants: [blankVariant('A')] }];
    }
    SEQ.steps.forEach(function (s) {
      if (!s.variants.length) s.variants.push(blankVariant('A'));
    });

    SUBSEQ.list = (d.subsequences || []).map(function (sub) {
      return {
        cid: uid('sub'), id: sub.id, name: sub.name || '',
        triggerDays: sub.trigger_days || 3, isActive: sub.is_active !== false,
        steps: (sub.steps || []).map(function (s) {
          return {
            cid: uid('s'), id: s.id, waitDays: s.wait_days,
            variants: (s.variants || []).map(function (v) {
              return { cid: uid('v'), id: v.id, label: v.label, name: v.name,
                       subject: v.subject, preheader: v.preheader, html: v.html_body };
            })
          };
        })
      };
    });
    SUBSEQ.list.forEach(function (sub) {
      if (!sub.steps.length) sub.steps = [{ cid: uid('s'), id: null, waitDays: 0, variants: [blankVariant('A')] }];
      sub.steps.forEach(function (s) { if (!s.variants.length) s.variants.push(blankVariant('A')); });
    });
    SUBSEQ.expanded = -1;
    SUBSEQ.sel = { step: 0, variant: 0 };

    if (d.schedule_timezone) {
      $('socCampaignTz').value = d.schedule_timezone;
      $('socTzLabelText').textContent = d.schedule_timezone;
    }
    if (d.send_option === 'schedule') {
      document.querySelector('input[name="socSendOption"][value="schedule"]').checked = true;
    }
    if (d.schedule_at) {
      WTITZ.restoreScheduleFromUTC(d.schedule_at, d.schedule_timezone, TZCFG);
    }
    setStatus('Saved', false);
  }

  /* Each phase is isolated: one failing area must not silently kill the rest of
     the page. Previously a Quill/CDN failure aborted init() before the recipient
     comboboxes were ever wired, which left them dead with no visible cause. */
  function phase(name, fn) {
    try { fn(); return true; }
    catch (err) {
      if (window.console) console.error('[SOCampaignPage] "' + name + '" failed:', err);
      failures.push(name);
      return false;
    }
  }
  var failures = [];

  function init(cfg) {
    CFG = cfg;
    failures = [];

    phase('tags', function () {
      var tagNode = $('socTagsData');
      TAGS = tagNode ? JSON.parse(tagNode.textContent) : [];
    });

    /* 1. Sequence state first — everything else reads from it. */
    phase('state', hydrate);
    phase('rail', renderRail);
    phase('rail-events', function () { wireRail(); wireDrag(); });
    phase('subsequence', function () {
      renderSubseqList();
      wireSubseqList();
      wireSubRail();
      wireSubDrag();
      initSubQuill();
      wireSubEditor();
      wireSubMergeMenu();
    });

    /* 2. Form controls BEFORE the editor, so an editor failure cannot kill them. */
    phase('recipients', wireCombos);
    phase('schedule', function () {
      if (typeof WTITZ === 'undefined') throw new Error('wti_timezones.js did not load');
      WTITZ.populateScheduleControls(TZCFG);
      WTITZ.initTzDropdown(TZCFG);
      if (!$('socEditingData')) {
        var tz = WTITZ.detectTz('Asia/Kolkata');
        $('socCampaignTz').value = tz;
        $('socTzLabelText').textContent = tz;
      }
      var ed = $('socEditingData');
      if (ed) {
        var d = JSON.parse(ed.textContent);
        if (d.schedule_at) WTITZ.restoreScheduleFromUTC(d.schedule_at, d.schedule_timezone, TZCFG);
      }
    });
    phase('send-window', function () { initSendWindowControls(); });
    phase('form', wireForm);

    /* 3. Editor last. */
    phase('editor', function () { initQuill(); buildPopovers(); loadEditor(); wireEditor(); });

    if (failures.length && window.console) {
      console.warn('[SOCampaignPage] degraded — failed sections: ' + failures.join(', '));
    }
  }

  function wireCombos() {
    includeCombo = makeCombo(
      { root: 'socRdRoot', trigger: 'socRdTrigger', panel: 'socRdPanel',
        tags: 'socRdTags', search: 'socRdSearch', list: 'socRdList' },
      'Select lists or segments',
      function () { syncExclude(); refreshEstimate(); });
    excludeCombo = makeCombo(
      { root: 'socRdExcRoot', trigger: 'socRdExcTrigger', panel: 'socRdExcPanel',
        tags: 'socRdExcTags', search: 'socRdExcSearch', list: 'socRdExcList' },
      'Select lists or segments to exclude',
      function () { refreshEstimate(); });
    senderCombo = makeCombo(
      { root: 'socSenderRoot', trigger: 'socSenderTrigger', panel: 'socSenderPanel',
        tags: 'socSenderTags', search: 'socSenderSearch', list: 'socSenderList' },
      'Select one or more sending accounts',
      function () { updateCapacityReadout(); });
    syncExclude();
    refreshEstimate();
  }

  /* Campaign Test Send toggle — same reveal-and-hide-the-button pattern as
     the Exclude toggle (socExclToggle). Shared by the toggle button itself
     and socToolTest (the paper-plane icon in the tool row does the same
     "open the test-send section" work rather than scrolling to a section
     that's still hidden behind the collapsed toggle). */
  function openTestSend() {
    $('socTestSendSection').hidden = false;
    $('socTestSendToggle').style.display = 'none';
  }
  function closeTestSend() {
    $('socTestSendSection').hidden = true;
    $('socTestSendToggle').style.display = '';
  }

  function wireEditor() {
    wireTestTargetCombo();

    $('socTestSendToggle').addEventListener('click', openTestSend);
    $('socTestSendClose').addEventListener('click', closeTestSend);

    /* editor fields */
    $('socSubject').addEventListener('input', function () {
      var v = curVariant(); if (v) { v.subject = this.value; }
      renderTestTargets(); markDirty(); scheduleScore();
    });
    $('socPreheader').addEventListener('input', function () {
      var v = curVariant(); if (v) { v.preheader = this.value; }
      markDirty();
    });
    $('socPreheaderToggle').addEventListener('click', function () {
      var ph = $('socPreheader');
      ph.hidden = !ph.hidden;
      this.textContent = ph.hidden ? 'Add preheader' : 'Hide preheader';
      if (!ph.hidden) ph.focus();
    });
    $('socSourceArea').addEventListener('input', function () { markDirty(); updateCharCount(); });

    /* toolbar extras */
    $('socTbUndo').addEventListener('click', function () { quill.history.undo(); });
    $('socTbRedo').addEventListener('click', function () { quill.history.redo(); });
    $('socTbSource').addEventListener('click', function () { toggleSource(); });
    $('socTbEmoji').addEventListener('click', function (e) { e.stopPropagation(); togglePop($('socEmojiPop'), this); });
    $('socTbTable').addEventListener('click', function (e) { e.stopPropagation(); togglePop($('socTablePop'), this); });
    $('socToolMergeTags').addEventListener('click', function (e) { e.stopPropagation(); togglePop($('socTagPop'), this); });
    document.addEventListener('click', function (e) {
      if (!e.target.closest('.soc-pop')) closePops();
    });

    $('socScoreDetails').addEventListener('click', showScoreModal);
    $('socScoreClose').addEventListener('click', function () { $('socScoreModal').classList.remove('open'); });
    $('socToolTest').addEventListener('click', function () {
      setTestTarget(SEQ.sel.step + ':' + SEQ.sel.variant);
      openTestSend();
      $('socTestRecipients').scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    $('socToolAb').addEventListener('click', function () {
      var btn = $('socStepRail').querySelector('.soc-step-card[data-idx="' + SEQ.sel.step + '"] .soc-var-add');
      if (btn && !btn.disabled) btn.click();
      else toast('This step already has the maximum ' + MAX_VARIANTS + ' variations.', 'warning');
    });

  }

  function wireForm() {
    /* steps */
    $('socAddStep').addEventListener('click', function () {
      if (SEQ.steps.length >= MAX_STEPS) return;
      commitEditor();
      SEQ.steps.push({ cid: uid('s'), id: null, waitDays: 3, variants: [blankVariant('A')] });
      SEQ.sel = { step: SEQ.steps.length - 1, variant: 0 };
      renderRail(); loadEditor(); markDirty();
    });
    $('socDelCancel').addEventListener('click', function () {
      $('socDelModal').classList.remove('open'); SEQ.pendingDelete = null;
    });
    $('socDelConfirm').addEventListener('click', function () {
      if (SEQ.pendingDelete === null) return;
      SEQ.steps.splice(SEQ.pendingDelete, 1);
      SEQ.sel = { step: Math.max(0, Math.min(SEQ.sel.step, SEQ.steps.length - 1)), variant: 0 };
      SEQ.pendingDelete = null;
      $('socDelModal').classList.remove('open');
      renderRail(); loadEditor(); markDirty();
    });

    /* exclude toggle */
    $('socExclToggle').addEventListener('click', function () {
      $('socExclSection').hidden = false;
      this.style.display = 'none';
    });
    $('socExclRemove').addEventListener('click', function () {
      $('socRdExcList').querySelectorAll('.soc-rd-chk:checked').forEach(function (c) { c.checked = false; });
      excludeCombo.summary();
      $('socExclSection').hidden = true;
      $('socExclToggle').style.display = '';
      refreshEstimate();
    });

    /* sender + name */
    $('socName').addEventListener('input', markDirty);
    if ($('socSenderList')) {
      // Auto-fill Sender Name / Reply-To from the first checked account, same
      // as the old single-select did — only when those fields are still empty,
      // so it never clobbers something the user already typed.
      $('socSenderList').addEventListener('change', function (e) {
        if (!e.target.classList.contains('soc-rd-chk') || !e.target.checked) return;
        var o = e.target.closest('.soc-rd-option');
        if ($('socSenderName') && !$('socSenderName').value) {
          $('socSenderName').value = o.dataset.displayName || '';
        }
        if ($('socReplyTo') && !$('socReplyTo').value) {
          $('socReplyTo').value = o.dataset.label || '';
        }
      });
      if ($('socSenderName')) $('socSenderName').addEventListener('input', markDirty);
      if ($('socReplyTo'))    $('socReplyTo').addEventListener('input', markDirty);
    }

    /* test send */
    addTestRow('');
    $('socTestAdd').addEventListener('click', function () { addTestRow(''); });
    $('socTestSend').addEventListener('click', sendTest);

    /* send option */
    document.querySelectorAll('input[name="socSendOption"]').forEach(function (r) {
      r.addEventListener('change', function () { updateSendOptionUI(); markDirty(); });
    });
    updateSendOptionUI();

    /* sending days & hours — selects are already populated/restored by
       initSendWindowControls() in the 'send-window' phase; this just wires
       the interactive bits. */
    if ($('socWindowToggle')) {
      $('socWindowToggle').addEventListener('change', function () {
        $('socWindowGroup').hidden = !this.checked;
        markDirty();
      });
    }
    document.querySelectorAll('#socDayChips .soc-day-chip').forEach(function (chip) {
      chip.addEventListener('click', function () {
        chip.classList.toggle('active');
        markDirty();
      });
    });

    /* actions */
    $('socSaveAll').addEventListener('click', function () { submit('save_draft', this); });
    $('socSaveDraftBtn').addEventListener('click', function () { submit('save_draft', this); });
    $('socSendBtn').addEventListener('click', function () {
      var sched = document.querySelector('input[name="socSendOption"]:checked').value === 'schedule';
      var msg = sched
        ? 'Are you sure you want to schedule this campaign? It will start sending automatically at the configured time.'
        : 'Are you sure you want to launch this campaign? It will start sending immediately.';
      if (!window.confirm(msg)) return;
      submit(sched ? 'schedule' : 'send_now', this);
    });

    window.addEventListener('beforeunload', function (e) {
      if (SEQ.dirty) { e.preventDefault(); e.returnValue = ''; }
    });
  }

  return { init: init, renderReviewSummary: renderReviewSummary };
})();
