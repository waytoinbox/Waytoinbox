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
  var MAX_CONDITIONS = 20;
  var MAX_GROUPS = 10;   // V4.0 -- mirrors SEQ_MAX_GROUPS in views/so_sender.py
  var GROUP_TRIGGER_TYPES = ['clicked', 'opened', 'replied'];   // no_event_after_days is never a valid group member

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

  // Branch conditions — flat rows (no nested steps), so unlike SUBSEQ there's
  // no expanded/rail/editor split, just a plain list. Step references
  // (sourceCid/yesCid/noCid) are SEQ.steps client ids ('' = not chosen /
  // no target) — never a raw numeric step id, matching the server's own
  // client-id-only resolution contract (see _validate_conditions).
  var COND = { list: [] };

  // V4.0 -- AND/OR condition groups. A group owns its own source step, wait
  // days, and YES/NO targets (mirroring SOConditionGroup); a condition
  // joins a group via its own groupCid ('' = standalone, unchanged from
  // before V4.0). Same plain-list shape as COND -- no expand/rail/editor
  // split here either.
  var GROUP = { list: [] };

  var quill = null, sourceMode = false, autosaveTimer = null, scoreTimer = null, estimateTimer = null;
  var subQuill = null, subSourceMode = false, subScoreTimer = null;
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
    // Default weight for a NEWLY created variant only (a fresh step's first
    // variant, or one produced by the "+" add-variation button, both main
    // sequence and subsequence — every such path shares this one
    // constructor). 10, not 1, so adding a 2nd variant lands on 10+10
    // rather than 1+1 — easier to nudge toward 100 by typing fewer digits.
    // Existing/hydrated variants never pass through here — see hydrate()'s
    // own (v.weight != null ? v.weight : 1) fallback, untouched.
    return { cid: uid('v'), id: null, label: label, name: 'Variation ' + label,
             subject: '', preheader: '', html: '', weight: 10 };
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

  /* V4.x variation-weight fix — a step with only one variant has nothing to
     split traffic against, so it's exempt from the 100% check (mirrors the
     same exemption in views/so_sender.py::_validate_step_list). Shared by
     both the main sequence and every subsequence's own steps — identical
     rule, same as their server-side validation. */
  function stepWeightTotal(step) {
    var total = step.variants.reduce(function (s, v) { return s + (v.weight || 0); }, 0);
    return { total: total, valid: step.variants.length <= 1 || total === 100 };
  }

  /* Global gate for Next/Save — true only if every step, in the main
     sequence AND every subsequence (including ones not currently expanded),
     has a valid weight total. Backend stays authoritative (this only blocks
     the buttons; the actual save/send call is still validated server-side),
     but this catches the common case before a round trip. */
  function allWeightsValid() {
    return SEQ.steps.every(function (s) { return stepWeightTotal(s).valid; }) &&
      SUBSEQ.list.every(function (sub) { return sub.steps.every(function (s) { return stepWeightTotal(s).valid; }); });
  }

  function updateWeightGate() {
    // V4.x variation-weight fix, UI-consistency follow-up — the backend's
    // 100%-sum check is strict-only (schedule/send_now — see views/
    // so_sender.py::_validate_step_list), so an invalid intermediate total
    // must not block Next or Save Draft either: both are non-strict, and
    // autosave already accepts this exact state (markDirty()'s timer POSTs
    // to so_sequence_autosave regardless of any button's disabled state).
    // Schedule/Send Now were never gated by this function — they rely
    // entirely on the backend's own strict rejection, unchanged here.
    // Left as a no-op call site (not removed) rather than deleting this
    // function and its five callers, so the live weight-total badge itself
    // — rendered/updated separately in renderRail()/renderSubRail() and the
    // weight-input handlers — is untouched.
  }

  function blankCondition() {
    return { cid: uid('c'), id: null, triggerType: 'no_event_after_days',
             sourceCid: '', waitDays: 1, threshold: null, yesCid: '', noCid: '', isActive: true,
             groupCid: '' };
  }
  // Looks up a step's CURRENT client id by its persisted db id — used only
  // while hydrating a saved condition, since the server hands back real
  // step ids (never client ids, which are regenerated fresh every page
  // load — see _serialize_conditions).
  function stepCidById(id) {
    if (id == null) return '';
    var found = SEQ.steps.filter(function (s) { return s.id === id; })[0];
    return found ? found.cid : '';
  }

  // V4.0 -- same idea as stepCidById, for a condition's saved group_id
  // (see _serialize_conditions' new 'group_id' key). Must only be called
  // AFTER GROUP.list has been hydrated (see hydrate() below).
  function blankGroup() {
    return { cid: uid('g'), id: null, logic: 'and',
             sourceCid: '', waitDays: 1, yesCid: '', noCid: '', isActive: true };
  }
  function groupCidById(id) {
    if (id == null) return '';
    var found = GROUP.list.filter(function (g) { return g.id === id; })[0];
    return found ? found.cid : '';
  }

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
      email_account_weights: accountWeights(),
      sender_name: ($('socSenderName') ? $('socSenderName').value.trim() : ''),
      reply_to:    ($('socReplyTo') ? $('socReplyTo').value.trim() : ''),
      schedule_date: $('socScheduleDate') ? $('socScheduleDate').value : '',
      schedule_time: (function () {
        try { return WTITZ.get24hrTime(TZCFG); } catch (e) { return ''; }
      })(),
      schedule_timezone: $('socCampaignTz') ? $('socCampaignTz').value : 'Asia/Kolkata',
      // Default true (checkbox starts checked in markup) so a brand-new
      // campaign — where hydrate() never runs to set an explicit value —
      // still submits the safe "tracking on" default rather than an
      // accidental false if the element were ever missing.
      tracking_enabled: $('socTrackingToggle') ? !!$('socTrackingToggle').checked : true,
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
          wait_hours: i === 0 ? 0 : (s.waitHours || 0),
          variants: s.variants.map(function (v) {
            return { client_id: v.cid, id: v.id, label: v.label, name: v.name,
                     subject: v.subject, preheader: v.preheader,
                     html_body: v.html, weight: (v.weight != null ? v.weight : 1) };
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
                         html_body: v.html, weight: (v.weight != null ? v.weight : 1) };
              })
            };
          })
        };
      }),
      conditions: COND.list.map(function (c) {
        return {
          client_id: c.cid, id: c.id, trigger_type: c.triggerType,
          source_step_client_id: c.sourceCid, wait_days: c.waitDays,
          event_count_threshold: c.threshold,
          yes_target_step_client_id: c.yesCid, no_target_step_client_id: c.noCid,
          is_active: c.isActive,
          group_client_id: c.groupCid,
        };
      }),
      condition_groups: GROUP.list.map(function (g) {
        return {
          client_id: g.cid, id: g.id, logic: g.logic,
          source_step_client_id: g.sourceCid, wait_days: g.waitDays,
          yes_target_step_client_id: g.yesCid, no_target_step_client_id: g.noCid,
          is_active: g.isActive,
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
          (SEQ.steps[i + 1].waitDays || 0) + '" data-waitfor="' + (i + 1) + '"/> Day(s) <input type="number" ' +
          'class="soc-wait-hours" min="0" max="23" value="' + (SEQ.steps[i + 1].waitHours || 0) +
          '" data-waitfor="' + (i + 1) + '"/> Hour(s), then</div>'
        : '';

      var activeVariant = step.variants[i === SEQ.sel.step ? SEQ.sel.variant : 0];
      var wTotal = stepWeightTotal(step);

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
          esc(activeVariant.name) + '"/>' +
        (step.variants.length > 1
          ? '<div class="soc-var-weight-row">Weight <input type="number" class="soc-var-weight" min="0" max="100" step="10" ' +
            'title="A/B split weight — the percentage of this step\'s sends that go to this variation; every ' +
            'variation in a step must add up to exactly 100." value="' +
            (activeVariant.weight != null ? activeVariant.weight : 1) + '"/>' +
            '<span class="soc-var-weight-total" style="margin-left:8px;font-size:12px;color:' +
            (wTotal.valid ? '#2e7d32' : '#c62828') + '">Total: ' + wTotal.total + '% ' + (wTotal.valid ? '✓' : '✗') +
            '</span></div>'
          : '') +
        waitRow;

      rail.appendChild(card);
    });

    $('socAddStep').disabled = SEQ.steps.length >= MAX_STEPS;
    renderTestTargets();
    updateWeightGate();
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
    renderEmailPreview();
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
    // renderSubRail() (above) already calls this when a subsequence is
    // expanded; called again here unconditionally so add/delete/collapse
    // still re-evaluate the gate when nothing ends up expanded.
    updateWeightGate();
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
        (function () {
          if (step.variants.length <= 1) return '';
          var activeVariant = step.variants[i === SUBSEQ.sel.step ? SUBSEQ.sel.variant : 0];
          var wTotal = stepWeightTotal(step);
          return '<div class="soc-var-weight-row">Weight <input type="number" class="soc-var-weight" min="0" max="100" step="10" ' +
            'title="A/B split weight — the percentage of this step\'s sends that go to this variation; every ' +
            'variation in a step must add up to exactly 100." value="' +
            (activeVariant.weight != null ? activeVariant.weight : 1) + '"/>' +
            '<span class="soc-var-weight-total" style="margin-left:8px;font-size:12px;color:' +
            (wTotal.valid ? '#2e7d32' : '#c62828') + '">Total: ' + wTotal.total + '% ' + (wTotal.valid ? '✓' : '✗') +
            '</span></div>';
        })() +
        waitRow;

      rail.appendChild(card);
    });

    $('socSubAddStep').disabled = sub.steps.length >= SUBSEQ_MAX_STEPS;
    updateWeightGate();
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
      if (e.target.closest('.soc-var-name') || e.target.closest('.soc-wait-days') ||
          e.target.closest('.soc-var-weight')) return;

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
      if (e.target.classList.contains('soc-var-weight')) {
        var vi2 = (SUBSEQ.sel.step === idx) ? SUBSEQ.sel.variant : 0;
        var w = Math.max(0, Math.min(1000, parseInt(e.target.value, 10) || 0));
        sub.steps[idx].variants[vi2].weight = w;
        var wTotal2 = stepWeightTotal(sub.steps[idx]);
        var badge = card.querySelector('.soc-var-weight-total');
        if (badge) {
          badge.textContent = 'Total: ' + wTotal2.total + '% ' + (wTotal2.valid ? '✓' : '✗');
          badge.style.color = wTotal2.valid ? '#2e7d32' : '#c62828';
        }
        updateWeightGate();
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
    v.preheader = $('socSubPreheader').value;
    if (subSourceMode)   v.html = $('socSubSourceArea').value;
    else if (subQuill)   v.html = emailSafeHtml(subQuill.root.innerHTML);
    // no editor available: leave v.html as-is rather than wiping the body
  }

  function loadSubEditor() {
    var step = curSubStep(), v = curSubVariant();
    if (!step || !v) return;
    $('socSubEditorBadge').textContent = 'Step ' + (SUBSEQ.sel.step + 1) + v.label;
    $('socSubSubject').value = v.subject || '';
    $('socSubPreheader').value = v.preheader || '';
    $('socSubPreheader').hidden = !v.preheader;
    $('socSubPreheaderToggle').textContent = v.preheader ? 'Hide preheader' : 'Add preheader';

    if (subSourceMode) toggleSubSource(false);
    if (subQuill) {
      subQuill.setContents(subQuill.clipboard.convert({ html: v.html || '' }), 'silent');
      subQuill.history.clear();               // undo must not cross a variation boundary
    } else if ($('socSubSourceArea')) {
      $('socSubSourceArea').value = v.html || '';   // editor-unavailable fallback
    }
    updateSubCharCount();
    subScheduleScore();
  }

  function updateSubCharCount() {
    var n;
    if (subSourceMode) n = $('socSubSourceArea').value.length;
    else if (subQuill) n = subQuill.getText().trim().length;
    else              n = 0;
    $('socSubCharCount').textContent = 'Characters : ' + n;
  }

  function toggleSubSource(force) {
    var want = (typeof force === 'boolean') ? force : !subSourceMode;
    if (want === subSourceMode || !subQuill) return;
    var ta = $('socSubSourceArea'), ed = $('socSubQuillEditor'), note = $('socSubSourceNote');
    if (want) {
      ta.value = emailSafeHtml(subQuill.root.innerHTML);
      ed.style.display = 'none'; ta.style.display = 'block'; note.style.display = 'block';
    } else {
      subQuill.setContents(subQuill.clipboard.convert({ html: ta.value }), 'silent');
      ed.style.display = ''; ta.style.display = 'none'; note.style.display = 'none';
    }
    subSourceMode = want;
    $('socSubTbSource').classList.toggle('active', want);
    updateSubCharCount();
  }

  /* Same degrade-to-plain-HTML-textarea fallback as editorUnavailable()
     above, for when Quill itself fails to load (script blocked/offline) --
     subSourceMode stays permanently true and subQuill stays null, so every
     other sub-editor function's existing `if (subQuill)`/`if (subSourceMode)`
     branches already do the right thing with no further changes needed. */
  function subEditorUnavailable(reason) {
    var ed = $('socSubQuillEditor'), ta = $('socSubSourceArea'), tb = $('socSubQuillToolbar');
    if (tb) tb.style.display = 'none';
    if (ed) ed.style.display = 'none';
    if (ta) { ta.style.display = 'block'; }
    var note = $('socSubSourceNote');
    if (note) {
      note.style.display = 'block';
      note.innerHTML = '<i class="fas fa-exclamation-triangle"></i> Rich-text editor could not load (' +
                       esc(reason) + '). You can still write the email as HTML here, and every other ' +
                       'part of this page works normally.';
    }
    subSourceMode = true;
    subQuill = null;
    if (ta) ta.addEventListener('input', function () { markDirty(); updateSubCharCount(); });
  }

  function subStepFormat(formatName, steps, dir, displayEl) {
    if (!subQuill) return;
    var range = subQuill.getSelection(true);
    if (!range) return;
    var current = subQuill.getFormat(range)[formatName] || steps[0];
    var idx = steps.indexOf(current);
    if (idx === -1) idx = 0;
    idx = dir === 'up' ? Math.min(steps.length - 1, idx + 1) : Math.max(0, idx - 1);
    subQuill.format(formatName, steps[idx]);
    if (displayEl) displayEl.textContent = steps[idx].replace('px', '');
    subQuill.focus();
  }

  function updateSubStepperDisplays() {
    if (!subQuill) return;
    var range = subQuill.getSelection();
    var fmt = range ? subQuill.getFormat(range) : {};
    $('socSubSizeValue').textContent = (fmt.size || '14px').replace('px', '');
    $('socSubLineHeightValue').textContent = fmt.lineheight || '1';
  }

  function subImageHandler() {
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
          var abs = new URL(d.url, window.location.origin).href;
          var range = subQuill.getSelection(true);
          subQuill.insertEmbed(range ? range.index : 0, 'image', abs, 'user');
          markDirty();
        })
        .catch(function () { toast('Upload failed.', 'error'); });
    };
    input.click();
  }

  function initSubQuill() {
    if (typeof Quill === 'undefined') { subEditorUnavailable('script blocked or offline'); return; }
    // Quill.register is idempotent for the same value -- safe to call again
    // here even though initQuill() (main editor) already registers these
    // same formats; this phase can run independently of that one and must
    // not assume it has run first.
    var Size = Quill.import('attributors/style/size');
    Size.whitelist = SEQ_SIZE_STEPS;
    Quill.register(Size, true);
    Quill.register(Quill.import('attributors/style/color'), true);
    Quill.register(Quill.import('attributors/style/align'), true);
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

    subQuill = new Quill('#socSubQuillEditor', {
      theme: 'snow',
      modules: {
        table: true,
        history: { delay: 800, maxStack: 200, userOnly: true },
        toolbar: { container: '#socSubQuillToolbar', handlers: { image: subImageHandler } }
      },
      placeholder: 'Write the follow-up email…',
    });

    updateSubStepperDisplays();
    subQuill.on('selection-change', updateSubStepperDisplays);
    subQuill.on('text-change', function (_d, _o, source) {
      if (source === 'user') markDirty();
      updateSubCharCount();
      subScheduleScore();
      updateSubStepperDisplays();
    });

    $('socSubSizeUp').addEventListener('click', function () { subStepFormat('size', SEQ_SIZE_STEPS, 'up', $('socSubSizeValue')); });
    $('socSubSizeDown').addEventListener('click', function () { subStepFormat('size', SEQ_SIZE_STEPS, 'down', $('socSubSizeValue')); });
    $('socSubLineHeightUp').addEventListener('click', function () { subStepFormat('lineheight', SEQ_LINEHEIGHT_STEPS, 'up', $('socSubLineHeightValue')); });
    $('socSubLineHeightDown').addEventListener('click', function () { subStepFormat('lineheight', SEQ_LINEHEIGHT_STEPS, 'down', $('socSubLineHeightValue')); });

    // Same two position fixes as initQuill() above (link tooltip landing
    // oddly placed; picker dropdown -- the colour select -- getting clipped
    // by the toolbar's own overflow-x:auto/overflow-y:hidden scroll
    // container, since .soc-seq-toolbar's CSS applies here too now that
    // socSubQuillToolbar carries that class). Duplicated rather than
    // factored out, matching every other main/sub editor pair in this
    // file (commitEditor/commitSubEditor, loadEditor/loadSubEditor, etc.)
    // -- see so_sender.py's own "structural mirror" convention.
    var subTooltipEl = document.querySelector('#socSubQuillEditor .ql-tooltip');
    if (subTooltipEl && typeof MutationObserver !== 'undefined') {
      new MutationObserver(function () {
        if (subTooltipEl.classList.contains('ql-hidden')) return;
        var editorEl = document.querySelector('#socSubQuillEditor');
        var eRect = editorEl ? editorEl.getBoundingClientRect() : { left: 0, width: window.innerWidth, top: 0 };
        subTooltipEl.style.position = 'fixed';
        subTooltipEl.style.left = (eRect.left + eRect.width / 2) + 'px';
        subTooltipEl.style.top = (eRect.top + 40) + 'px';
        subTooltipEl.style.transform = 'translateX(-50%)';
        subTooltipEl.style.margin = '0';
      }).observe(subTooltipEl, { attributes: true, attributeFilter: ['class'] });
    }
    if (typeof MutationObserver !== 'undefined') {
      document.querySelectorAll('#socSubQuillToolbar .ql-picker').forEach(function (picker) {
        var options = picker.querySelector('.ql-picker-options');
        if (!options) return;
        new MutationObserver(function () {
          if (!picker.classList.contains('ql-expanded')) return;
          var r = picker.getBoundingClientRect();
          options.style.position = 'fixed';
          options.style.top = (r.bottom + 4) + 'px';
          options.style.left = r.left + 'px';
          options.style.minWidth = '0';
        }).observe(picker, { attributes: true, attributeFilter: ['class'] });
      });
    }

    setupImageResize({
      getQuill: function () { return subQuill; },
      editorSelector: '#socSubQuillEditor',
      popId: 'socSubImgResizePop', widthId: 'socSubImgWidth', heightId: 'socSubImgHeight',
      lockId: 'socSubImgLockRatio', resetId: 'socSubImgResetSize',
    });
  }

  function wireSubEditor() {
    $('socSubSubject').addEventListener('input', function () {
      var v = curSubVariant(); if (v) v.subject = this.value;
      markDirty();
    });
    $('socSubPreheader').addEventListener('input', function () {
      var v = curSubVariant(); if (v) { v.preheader = this.value; }
      markDirty();
    });
    $('socSubPreheaderToggle').addEventListener('click', function () {
      var ph = $('socSubPreheader');
      ph.hidden = !ph.hidden;
      this.textContent = ph.hidden ? 'Add preheader' : 'Hide preheader';
      if (!ph.hidden) ph.focus();
    });
    $('socSubSourceArea').addEventListener('input', function () { markDirty(); updateSubCharCount(); });

    $('socSubTbUndo').addEventListener('click', function () { subQuill.history.undo(); });
    $('socSubTbRedo').addEventListener('click', function () { subQuill.history.redo(); });
    $('socSubTbSource').addEventListener('click', function () { toggleSubSource(); });
    $('socSubTbEmoji').addEventListener('click', function (e) { e.stopPropagation(); toggleSubPop($('socSubEmojiPop'), this); });
    $('socSubTbTable').addEventListener('click', function (e) { e.stopPropagation(); toggleSubPop($('socSubTablePop'), this); });
    document.addEventListener('click', function (e) {
      if (!e.target.closest('.soc-pop')) closeSubPops();
    });

    $('socSubScoreDetails').addEventListener('click', subShowScoreModal);
    $('socSubToolAb').addEventListener('click', function () {
      var btn = $('socSubStepRail').querySelector('.soc-step-card[data-idx="' + SUBSEQ.sel.step + '"] .soc-var-add');
      if (btn && !btn.disabled) btn.click();
      else toast('This step already has the maximum ' + SUBSEQ_MAX_VARIANTS + ' variations.', 'warning');
    });
    // submit() saves the WHOLE campaign payload (main sequence, every
    // subsequence, conditions, groups, settings) via collectPayload() --
    // exactly what socSaveAll already does in the main editor, so this is
    // the same shortcut, not a new/separate save path.
    $('socSubSaveAll').addEventListener('click', function () { submit('save_draft', this); });
  }

  function wireSubMergeMenu() {
    var menu = $('socSubMergeMenu');
    var mergeBtn = $('socSubMergeBtn');
    menu.innerHTML = TAGS.map(function (t) {
      return '<button type="button" data-tag="' + esc(t.value) + '">' + esc(t.label) + '</button>';
    }).join('');
    mergeBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
      if (!menu.hidden) {
        // Same bug class as the colour-picker fix in initQuill()/
        // initSubQuill() -- this menu is position:absolute inside
        // .soc-editor-bar, which sets overflow-x:auto; CSS forces
        // overflow-y to also clip once overflow-x isn't visible, so
        // almost this entire ~204px-tall dropdown was silently cut off
        // inside the ~51px-tall bar (only a sliver of the first row
        // survived, which is why an earlier automated check clicking
        // that first item alone missed this). Lifted to fixed viewport
        // coordinates instead. Unlike the picker fix, no min-width reset
        // is needed here -- .soc-sub-merge-menu's min-width is a fixed
        // 160px, not a percentage, so it isn't affected by the
        // containing-block change from switching to position:fixed.
        var r = mergeBtn.getBoundingClientRect();
        menu.style.position = 'fixed';
        // Opens to the LEFT of the button (menu's right edge sits just
        // left of the button's left edge), top-aligned with the button --
        // not below it. Clamp against the viewport's left edge (same
        // 8px-margin style positionPop() below uses for the other
        // popovers) so a button sitting close to the left edge doesn't
        // push the menu off-screen.
        menu.style.top = r.top + 'px';
        menu.style.left = Math.max(8, r.left - menu.offsetWidth - 4) + 'px';
      }
    });
    // position:fixed (needed to escape .soc-editor-bar's clipping, see
    // above) does not track page scroll the way the OTHER popovers'
    // position:absolute does (.soc-pop, positioned relative to <body>,
    // outside any clipping ancestor, so window.scrollX/Y-aware absolute
    // positioning naturally scrolls with the page). Left open during a
    // scroll, this menu would stay glued to its last screen position while
    // the actual merge button scrolls away underneath it. Closing on any
    // scroll (capture:true so this fires even for scrolling inside a
    // nested container, since `scroll` doesn't bubble) is the same
    // standard behavior virtually every dropdown/select uses, and avoids
    // the complexity of continuously repositioning while open.
    document.addEventListener('scroll', function () { menu.hidden = true; }, true);
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

  /* ── subsequence popovers ───────────────────────────────────────────────
     Own close/toggle rather than reusing closePops()/togglePop() directly --
     togglePop()'s body unconditionally calls the MAIN editor's closePops()
     (closing socEmojiPop/socTagPop/socTablePop), which would leave a
     currently-open sub popover untouched, letting two sub popovers end up
     open at once. positionPop() itself is generic (pop/anchor are already
     parameters, no main-editor coupling) and is reused as-is. */
  function closeSubPops() {
    ['socSubEmojiPop', 'socSubTablePop'].forEach(function (id) { $(id).classList.remove('open'); });
  }
  function toggleSubPop(pop, anchor) {
    var wasOpen = pop.classList.contains('open');
    closeSubPops();
    if (!wasOpen) { positionPop(pop, anchor); pop.classList.add('open'); }
  }

  function subInsertAtCursor(text) {
    if (!subQuill && !subSourceMode) return;
    if (subSourceMode) {
      var ta = $('socSubSourceArea'), p = ta.selectionStart || 0;
      ta.value = ta.value.slice(0, p) + text + ta.value.slice(ta.selectionEnd || p);
      ta.focus();
    } else {
      var range = subQuill.getSelection(true);
      subQuill.insertText(range ? range.index : 0, text, 'user');
    }
    markDirty();
    updateSubCharCount();
  }

  function buildSubPopovers() {
    $('socSubEmojiGrid').innerHTML = EMOJI.map(function (e) {
      return '<button type="button">' + e + '</button>';
    }).join('');
    $('socSubEmojiGrid').addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (b) { subInsertAtCursor(b.textContent); closeSubPops(); }
    });

    var grid = $('socSubTableGrid'), html = '';
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
      $('socSubTableLbl').textContent = R + ' × ' + C;
    });
    grid.addEventListener('click', function (e) {
      var cell = e.target.closest('span');
      if (!cell) return;
      subInsertTable(+cell.dataset.r, +cell.dataset.c);
      closeSubPops();
    });
  }

  function subInsertTable(rows, cols) {
    var mod = subQuill.getModule('table');
    if (mod && typeof mod.insertTable === 'function') {
      subQuill.focus();
      mod.insertTable(rows, cols);
      markDirty();
      return;
    }
    // Quill build without the table module — fall back to the source view
    toast('Tables are inserted through the HTML view in this editor build.', 'warning');
    toggleSubSource(true);
  }

  /* ── subsequence content score ───────────────────────────────────────── */
  var subLastScore = null;
  function subScheduleScore() {
    clearTimeout(subScoreTimer);
    subScoreTimer = setTimeout(subRunScore, 1500);
  }
  function subRunScore(openModalWhenDone) {
    var v = curSubVariant();
    if (!v) return;
    commitSubEditor();
    fetch(CFG.scoreUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CFG.csrf },
      body: JSON.stringify({ subject: v.subject, html_body: v.html })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status !== 'ok') return;
        subLastScore = d;
        var pill = $('socSubScorePill');
        pill.textContent = d.label;
        pill.className = 'soc-score-pill ' + d.label.toLowerCase();
        // Same first-click-opens-once-ready fix as the main editor's
        // runScore()/showScoreModal() -- see that pair's own comment.
        if (openModalWhenDone) subShowScoreModal();
      })
      .catch(function () { /* score is advisory */ });
  }
  function subShowScoreModal() {
    if (!subLastScore) { subRunScore(true); return; }
    // Reuses the SAME modal DOM as the main editor's showScoreModal() --
    // safe because only one wizard step (Sequence vs Subsequence) is ever
    // visible/interactive at a time, so the two can never need the modal
    // simultaneously.
    $('socScoreSummary').textContent =
      'Score ' + subLastScore.score + ' — ' + subLastScore.label + '. Lower is better.';
    $('socScoreReasons').innerHTML = subLastScore.reasons.map(function (r) {
      var ico = r.severity === 'warn' ? 'fa-exclamation-triangle warn' : 'fa-info-circle info';
      return '<div class="soc-reason"><i class="fas ' + ico + '"></i><span>' + esc(r.text) + '</span></div>';
    }).join('');
    $('socScoreModal').classList.add('open');
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
    if (html.indexOf('<table') === -1 && html.indexOf('<img') === -1) return html;
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
    // A resized image carries plain width/height attributes (set via
    // quill.formatText -- see setupImageResize above) rather than inline
    // CSS, since those are what Quill's built-in image blot actually
    // recognises as formats and preserves across the editor's own
    // clipboard.convert() round-trip. The width attribute alone is enough
    // for old clients that ignore CSS (classic Outlook), but responsive
    // clients need the matching inline style too -- added here, once, at
    // save time, exactly like the <table> attributes above, rather than
    // trying to keep a live style in sync inside the editor itself.
    box.querySelectorAll('img[width]').forEach(function (img) {
      if (!img.style.width) {
        img.style.width = img.getAttribute('width') + 'px';
        img.style.height = 'auto';
        img.style.maxWidth = '100%';
      }
    });
    // Alignment (quill.formatLine's {align} -- see setupImageResize above)
    // is stored as text-align on the image's containing <p>, which only
    // affects an INLINE child -- several real webmail clients (Gmail's own
    // reset among them) apply their own `img { display: block }` the same
    // way this app's tokens-reset-utilities.css does, which would silently
    // defeat text-align alone for some recipients exactly like it did in
    // this editor before that CSS fix. Belt-and-suspenders: also apply the
    // equivalent margin-based centering directly on the image, which works
    // correctly for a block-level image regardless of what the recipient's
    // client does with display -- redundant with the parent's text-align
    // (harmless either way), not a replacement for it.
    box.querySelectorAll('p[style*="text-align"] > img').forEach(function (img) {
      var align = img.parentElement.style.textAlign;
      img.style.display = 'block';
      if (align === 'center')      img.style.margin = '0 auto';
      else if (align === 'right')  img.style.margin = '0 0 0 auto';
      else                         img.style.margin = '0';
    });
    // soc-img-selected is a pure editor-UI marker (the resize outline) —
    // it lands in quill.root.innerHTML whenever a save happens while an
    // image is still selected, and must never leak into saved/sent HTML.
    box.querySelectorAll('img.soc-img-selected').forEach(function (img) {
      img.classList.remove('soc-img-selected');
      if (!img.getAttribute('class')) img.removeAttribute('class');
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
  function runScore(openModalWhenDone) {
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
        // Forensic-audit fix — showScoreModal()'s own first-click case (no
        // score yet) triggers this fetch and used to just return, leaving
        // the modal never opened once the score arrived. Re-entering
        // showScoreModal() now that lastScore is set takes its normal
        // already-scored branch below, opening it. Untouched for the
        // ordinary scheduleScore()->runScore() background-refresh path
        // (openModalWhenDone is undefined there), which must keep only
        // updating the pill, never popping the modal unprompted.
        if (openModalWhenDone) showScoreModal();
      })
      .catch(function () { /* score is advisory */ });
  }
  function showScoreModal() {
    if (!lastScore) { runScore(true); return; }
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

  /* {account_id: weight} for every currently-checked sender account, read
     straight from each option's data-weight attribute (the single source
     of truth — see renderSenderWeights()). Missing/blank defaults to 1,
     same default the model field itself uses. */
  function accountWeights() {
    var out = {};
    var list = $('socSenderList');
    if (!list) return out;
    list.querySelectorAll('.soc-rd-option[data-type="account"]').forEach(function (o) {
      var chk = o.querySelector('.soc-rd-chk');
      if (chk && chk.checked) {
        var w = o.dataset.weight != null && o.dataset.weight !== '' ? parseInt(o.dataset.weight, 10) : 1;
        out[o.dataset.id] = isNaN(w) ? 1 : w;
      }
    });
    return out;
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
    var variantCount = SEQ.steps.reduce(function (sum, s) { return sum + s.variants.length; }, 0);

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
    var trackingOn = $('socTrackingToggle') ? !!$('socTrackingToggle').checked : true;
    var trackingText = trackingOn ? 'Enabled (opens & clicks tracked)' : 'Disabled for this campaign';

    function row(label, value) {
      return '<div class="soc-review-row"><span class="soc-review-label">' + esc(label) +
             '</span><span class="soc-review-value">' + esc(value) + '</span></div>';
    }

    el.innerHTML =
      row('Audience', prospectCount + ' prospect' + (prospectCount === 1 ? '' : 's') +
          ' across ' + listCount + ' list/segment' + (listCount === 1 ? '' : 's')) +
      row('Sequence', stepCount + ' step' + (stepCount === 1 ? '' : 's') +
          ' · ' + variantCount + ' variation' + (variantCount === 1 ? '' : 's')) +
      row('Subsequence', subLabel) +
      row('Sender Accounts', accIds.length + ' account' + (accIds.length === 1 ? '' : 's') +
          ' · ' + combined.toLocaleString() + '/day combined') +
      row('Send Option', sendOpt) +
      row('Schedule', scheduleText) +
      row('Timezone', tz) +
      row('Sending Window', windowText) +
      row('Tracking', trackingText);
  }

  /* ── Email Preview (Step 5) ────────────────────────────────────────────
     Driven by the SAME socTestTarget selection Campaign Test Send already
     uses (see setTestTarget()'s own call to this at the end) -- reuses
     existing state rather than adding a second, independent selector. */
  function renderEmailPreview() {
    var el = $('socEmailPreview');
    if (!el) return;
    var val = $('socTestTarget') ? $('socTestTarget').value : '';
    var parts = val ? val.split(':') : [];
    var step = parts.length === 2 ? SEQ.steps[parseInt(parts[0], 10)] : null;
    var v = step ? step.variants[parseInt(parts[1], 10)] : null;
    if (!v) {
      el.innerHTML = '<p class="soc-hint soc-hint-tight-sm">Select a step &amp; variation above to preview it.</p>';
      return;
    }
    var fromName = ($('socSenderName') && $('socSenderName').value) || '';
    var fromEmail = ($('socReplyTo') && $('socReplyTo').value) || '';
    var fromLine = fromName ? (fromName + (fromEmail ? ' <' + fromEmail + '>' : '')) : (fromEmail || '—');

    el.innerHTML =
      '<div class="soc-review-summary">' +
        '<div class="soc-review-row"><span class="soc-review-label">From</span>' +
          '<span class="soc-review-value">' + esc(fromLine) + '</span></div>' +
        '<div class="soc-review-row"><span class="soc-review-label">Subject</span>' +
          '<span class="soc-review-value">' + esc(v.subject || '(empty)') + '</span></div>' +
        '<div class="soc-review-row"><span class="soc-review-label">Preheader</span>' +
          '<span class="soc-review-value">' + esc(v.preheader || '(none)') + '</span></div>' +
      '</div>' +
      '<div class="soc-email-preview-body">' + (v.html && v.html.trim() ? v.html : '<p class="soc-hint">(empty body)</p>') + '</div>';
  }

  /* ── Launch readiness (Step 6) ──────────────────────────────────────────
     Deliberately minimal -- no repeated audience/sender/capacity/timezone/
     window/tracking/sequence detail, all of which already live on Review
     (Step 5, renderReviewSummary above). This is only a go/no-go checklist
     plus the launch-timing message. All checks are advisory client-side
     hints computed from state already in memory (nothing new fetched) --
     the backend's own strict validation stays the actual authority at
     submit time, unchanged. */
  function renderLaunchReadiness(targetId) {
    var el = $(targetId);
    if (!el) return;

    var prospectCount = lastEstimateCount;
    var accIds = accountIds();
    var combined = 0;
    var senderList = $('socSenderList');
    if (senderList) {
      senderList.querySelectorAll('.soc-rd-chk:checked').forEach(function (c) {
        combined += parseInt(c.closest('.soc-rd-option').dataset.dailyLimit, 10) || 0;
      });
    }
    var sequenceReady = SEQ.steps.length > 0 && SEQ.steps.every(function (s) {
      return s.variants.length > 0 && s.variants.every(function (v) {
        return (v.subject || '').trim() && (v.html || '').trim();
      });
    });

    var checks = [
      { label: 'Audience ready', ok: prospectCount > 0,
        warn: 'No prospects selected — go back to Audience and choose at least one list or segment.' },
      { label: 'Sequence configured', ok: sequenceReady,
        warn: 'Every step needs at least one variation with a subject line and a body — go back to Sequence and finish it.' },
      { label: 'Sender account connected', ok: accIds.length > 0,
        warn: 'No sender account selected — go back to Settings and choose at least one sending account.' },
      { label: 'Sending capacity available', ok: combined > 0,
        warn: 'The selected sender account(s) have no daily sending capacity — check their limits in Settings.' },
      { label: 'Tracking configured', ok: true, warn: '' }
    ];

    var checklistHtml = checks.map(function (c) {
      return '<div class="soc-reason"><i class="fas ' + (c.ok ? 'fa-check-circle' : 'fa-exclamation-triangle warn') + '"></i>' +
             '<span>' + esc(c.label) + '</span></div>';
    }).join('');

    var failing = checks.filter(function (c) { return !c.ok; });
    var warningsHtml = failing.length
      ? '<div class="soc-launch-warning"><strong><i class="fas fa-exclamation-triangle"></i> Action required</strong>' +
        failing.map(function (c) { return '<p>' + esc(c.warn) + '</p>'; }).join('') + '</div>'
      : '';

    var sendOptEl = document.querySelector('input[name="socSendOption"]:checked');
    var isSchedule = !!(sendOptEl && sendOptEl.value === 'schedule');
    var message;
    if (isSchedule) {
      var d = $('socScheduleDate') ? $('socScheduleDate').value : '';
      var timeStr = '';
      try { timeStr = WTITZ.get24hrTime(TZCFG); } catch (e) { /* not restored yet */ }
      var tz = $('socCampaignTz') ? $('socCampaignTz').value : 'Asia/Kolkata';
      message = d
        ? 'Campaign will start on ' + esc(d) + (timeStr ? ' ' + esc(timeStr) : '') + ' (' + esc(tz) + ').'
        : 'Pick a date and time on the Settings step before this campaign can be scheduled.';
    } else {
      message = 'Campaign will start sending immediately according to the configured sending limits.';
    }

    el.innerHTML =
      '<div class="soc-launch-checklist">' + checklistHtml + '</div>' +
      warningsHtml +
      '<p class="soc-launch-message">' + message + '</p>';
  }

  /* ── Sender rotation weights ─────────────────────────────────────────── */
  /* Weight lives directly on each account's .soc-rd-option element as a
     data-weight attribute — same place data-daily-limit already lives —
     rather than a separate JS state object, so it can't drift out of sync
     with which accounts are actually checked. Only checked accounts ever
     get a visible row; unchecking one just hides it (its data-weight is
     preserved on the element in case it's re-checked in the same session). */
  function renderSenderWeights() {
    var wrap = $('socSenderWeights');
    var list = $('socSenderList');
    if (!wrap || !list) return;
    var checked = Array.prototype.filter.call(
      list.querySelectorAll('.soc-rd-option[data-type="account"]'),
      function (o) { var chk = o.querySelector('.soc-rd-chk'); return chk && chk.checked; }
    );
    if (!checked.length) { wrap.hidden = true; wrap.innerHTML = ''; return; }
    wrap.hidden = false;
    wrap.innerHTML =
      '<p class="soc-hint soc-sender-weight-hint">Higher weight = more prospects assigned to this account</p>' +
      checked.map(function (o) {
        var w = o.dataset.weight != null && o.dataset.weight !== '' ? o.dataset.weight : '1';
        return '<div class="soc-sender-weight-row" data-account-id="' + esc(o.dataset.id) + '">' +
          '<span class="soc-sender-weight-email">' + esc(o.dataset.label) + '</span>' +
          '<span class="soc-sender-weight-field"><label>Weight</label>' +
          '<input type="number" class="soc-sender-weight-input" min="0" max="1000" step="10" value="' + esc(w) + '"/></span>' +
        '</div>';
      }).join('');
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
                send_window: 'err-socWindow', subsequences: 'err-socSubsequences',
                conditions: 'err-socConditions' };
    // Which wizard step each field lives on, so an error on a hidden panel is
    // actually visible rather than silently failing to scroll into view.
    // Steps: 1 Audience, 2 Sequence, 3 Subsequence (+ Branch Conditions),
    // 4 Settings, 5 Review & Test, 6 Launch.
    var step = { name: 1, recipients: 1, account: 4, sequence: 2, schedule: 4, send_window: 4,
                 subsequences: 3, conditions: 3 };
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

    // Colour picker dropdown (.ql-picker-options, e.g. #ql-picker-options-0
    // for the text-colour select) — the toolbar scrolls horizontally on
    // narrow viewports (.soc-seq-toolbar's overflow-x:auto in
    // so_campaign.css), and overflow-x/overflow-y can't independently be
    // auto/visible on the same box — CSS forces overflow-y to also clip
    // once overflow-x isn't visible — so the dropdown, which needs to
    // render BELOW the toolbar, was being silently clipped to invisible
    // even though it was fully present and expanded in the DOM. Same
    // fixed-viewport-position technique as the tooltip fix above, applied
    // to every Quill picker in this toolbar (currently just colour) rather
    // than removing the toolbar's own horizontal scroll.
    if (typeof MutationObserver !== 'undefined') {
      document.querySelectorAll('#socQuillToolbar .ql-picker').forEach(function (picker) {
        var options = picker.querySelector('.ql-picker-options');
        if (!options) return;
        new MutationObserver(function () {
          if (!picker.classList.contains('ql-expanded')) return;
          var r = picker.getBoundingClientRect();
          options.style.position = 'fixed';
          options.style.top = (r.bottom + 4) + 'px';
          options.style.left = r.left + 'px';
          // Quill's own CSS sets .ql-picker-options { min-width: 100% },
          // which resolves against the CONTAINING BLOCK -- the tiny
          // ~28px picker span under position:absolute (its original
          // positioning), but the whole viewport once switched to
          // position:fixed above. min-width always wins over a smaller
          // explicit width, so without this the dropdown would stretch
          // to the full viewport width instead of Quill's intended
          // ~152px. Inline style beats the stylesheet rule regardless of
          // selector specificity, restoring the original sizing.
          options.style.minWidth = '0';
        }).observe(picker, { attributes: true, attributeFilter: ['class'] });
      });
    }

    setupImageResize({
      getQuill: function () { return quill; },
      editorSelector: '#socQuillEditor',
      popId: 'socImgResizePop', widthId: 'socImgWidth', heightId: 'socImgHeight',
      lockId: 'socImgLockRatio', resetId: 'socImgResetSize',
    });
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

  /* ── image resize (shared by both editors — a generic operation on
     whichever quill instance/popover it's given, same sharing rationale as
     positionPop/closePops/EMOJI below, unlike the editor-specific business
     logic elsewhere in this file which is deliberately duplicated) ──────
     Quill 2.0.3's built-in image blot already recognises width/height as
     real attribute-backed formats (confirmed against the vendored
     quill.min.js: const C=["alt","height","width"]), so resizing goes
     through quill.formatText() like any other format — no custom blot
     needed. That's also what makes it survive the clipboard.convert()
     round-trip loadEditor()/toggleSource() already do on every step
     switch: format()-applied attributes are what that round-trip actually
     preserves, whereas a plain el.style mutation on the live DOM node
     would just be silently dropped the next time the editor re-parses
     saved HTML back into a Delta. */
  function setupImageResize(cfg) {
    var pop         = $(cfg.popId);
    var widthInput  = $(cfg.widthId);
    var heightInput = $(cfg.heightId);
    var lockInput   = $(cfg.lockId);
    var resetBtn    = $(cfg.resetId);
    var alignBtns   = pop.querySelectorAll('.soc-img-align-btn');
    var editorEl    = document.querySelector(cfg.editorSelector);
    if (!editorEl) return;

    // Corner-pin the popover to the editor container itself rather than
    // the image (see the CSS comment on .soc-img-resize-pop) -- moves it
    // out from wherever the template originally placed it, once, so plain
    // top/right in CSS is all that's needed from here on.
    if (pop.parentElement !== editorEl) editorEl.appendChild(pop);

    var selected = null;   // the currently-selected <img> DOM node
    var handle   = null;   // drag-to-resize handle, appended to <body>
    var ratio    = 1;      // naturalWidth / naturalHeight of `selected`
    var dragging = false;

    function currentSize(img) {
      var w = parseInt(img.getAttribute('width'), 10);
      var h = parseInt(img.getAttribute('height'), 10);
      if (!w || !h) {
        var r = img.getBoundingClientRect();
        w = w || Math.round(r.width);
        h = h || Math.round(r.height);
      }
      return { w: w, h: h };
    }

    function positionHandle() {
      if (!handle || !selected) return;
      var r = selected.getBoundingClientRect();
      handle.style.left = (r.right + window.scrollX - 6) + 'px';
      handle.style.top  = (r.bottom + window.scrollY - 6) + 'px';
    }

    function syncAlignButtons() {
      var quill = cfg.getQuill();
      var current = '';
      if (quill && selected) {
        var blot = Quill.find(selected);
        if (blot) {
          var fmt = quill.getFormat(quill.getIndex(blot), 1);
          current = fmt.align || '';
        }
      }
      alignBtns.forEach(function (btn) {
        btn.classList.toggle('active', btn.dataset.align === current);
      });
    }

    function select(img) {
      if (selected === img) return;
      clearSelection();
      // Mutually exclusive with the toolbar's own emoji/table/tag popovers
      // -- both editors share the exact same closePops()/closeSubPops()
      // used by their own toolbar buttons.
      if (typeof closePops === 'function') closePops();
      if (typeof closeSubPops === 'function') closeSubPops();
      selected = img;
      selected.classList.add('soc-img-selected');
      ratio = (img.naturalWidth && img.naturalHeight) ? (img.naturalWidth / img.naturalHeight) : 1;

      var size = currentSize(img);
      widthInput.value  = size.w;
      heightInput.value = size.h;
      syncAlignButtons();

      if (!handle) {
        handle = document.createElement('div');
        handle.className = 'soc-img-resize-handle';
        document.body.appendChild(handle);
        handle.addEventListener('mousedown', onHandleMouseDown);
      }
      handle.style.display = '';
      positionHandle();
      pop.classList.add('open');
      window.addEventListener('scroll', positionHandle, true);
      window.addEventListener('resize', positionHandle);
    }

    function clearSelection() {
      if (selected) selected.classList.remove('soc-img-selected');
      selected = null;
      pop.classList.remove('open');
      if (handle) handle.style.display = 'none';
      window.removeEventListener('scroll', positionHandle, true);
      window.removeEventListener('resize', positionHandle);
    }

    function applySize(w, h) {
      var quill = cfg.getQuill();
      if (!quill || !selected || !w || !h) return;
      var blot = Quill.find(selected);
      if (!blot) return;
      var idx = quill.getIndex(blot);
      quill.formatText(idx, 1, { width: String(w), height: String(h) }, 'user');
      // formatText() re-renders the blot -- the DOM node is replaced, so
      // the resize handle/outline must move to the new node at the same spot.
      var leaf = quill.getLeaf(idx);
      var newNode = leaf && leaf[0] && leaf[0].domNode;
      if (newNode && newNode.tagName === 'IMG') {
        selected.classList.remove('soc-img-selected');
        selected = newNode;
        selected.classList.add('soc-img-selected');
      }
      positionHandle();
      markDirty();
    }

    function applyAlign(value) {
      var quill = cfg.getQuill();
      if (!quill || !selected) return;
      var blot = Quill.find(selected);
      if (!blot) return;
      var idx = quill.getIndex(blot);
      // Block-level format (Quill.import('attributors/style/align'), already
      // registered in initQuill/initSubQuill for the toolbar's own text-align
      // button) applied to the image's own line -- an email-safe technique
      // since it becomes a plain text-align inline style on the containing
      // <p>, which every mail client honours, rather than floating the
      // image itself (unreliable across clients, and would need its own
      // clearfix handling that plain text-align never does).
      quill.formatLine(idx, 1, { align: value || false }, 'user');
      syncAlignButtons();
      markDirty();
    }

    function onHandleMouseDown(e) {
      e.preventDefault();
      dragging = true;
      var startX = e.clientX;
      var start = currentSize(selected);
      function onMove(me) {
        if (!dragging) return;
        var newW = Math.max(20, start.w + (me.clientX - startX));
        var newH = lockInput.checked ? Math.round(newW / ratio) : start.h;
        widthInput.value = newW;
        heightInput.value = newH;
        applySize(newW, newH);
      }
      function onUp() {
        dragging = false;
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    }

    editorEl.addEventListener('click', function (e) {
      if (e.target.closest('.soc-img-resize-pop')) return;
      var img = e.target.closest ? e.target.closest('img') : null;
      if (img && editorEl.contains(img)) { select(img); }
      else if (selected) { clearSelection(); }
    });

    widthInput.addEventListener('input', function () {
      var w = parseInt(widthInput.value, 10);
      if (!w) return;
      var h = lockInput.checked ? Math.round(w / ratio) : parseInt(heightInput.value, 10);
      if (lockInput.checked) heightInput.value = h;
      applySize(w, h);
    });
    heightInput.addEventListener('input', function () {
      var h = parseInt(heightInput.value, 10);
      if (!h) return;
      var w = lockInput.checked ? Math.round(h * ratio) : parseInt(widthInput.value, 10);
      if (lockInput.checked) widthInput.value = w;
      applySize(w, h);
    });
    alignBtns.forEach(function (btn) {
      btn.addEventListener('click', function () { applyAlign(btn.dataset.align); });
    });
    resetBtn.addEventListener('click', function () {
      var quill = cfg.getQuill();
      if (!quill || !selected) return;
      var blot = Quill.find(selected);
      if (!blot) return;
      var idx = quill.getIndex(blot);
      quill.formatText(idx, 1, { width: false, height: false }, 'user');
      clearSelection();
      markDirty();
    });

    document.addEventListener('click', function (e) {
      if (selected && !e.target.closest('img') && !e.target.closest('.soc-img-resize-pop')) clearSelection();
    });
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
      if (e.target.closest('.soc-var-name') || e.target.closest('.soc-wait-days') ||
          e.target.closest('.soc-var-weight') || e.target.closest('.soc-wait-hours')) return;

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
      if (e.target.classList.contains('soc-var-weight')) {
        var vi2 = (SEQ.sel.step === idx) ? SEQ.sel.variant : 0;
        var w = Math.max(0, Math.min(1000, parseInt(e.target.value, 10) || 0));
        SEQ.steps[idx].variants[vi2].weight = w;
        // Live total badge, updated in place (no re-render) so the input
        // being typed into never loses focus.
        var wTotal2 = stepWeightTotal(SEQ.steps[idx]);
        var badge = card.querySelector('.soc-var-weight-total');
        if (badge) {
          badge.textContent = 'Total: ' + wTotal2.total + '% ' + (wTotal2.valid ? '✓' : '✗');
          badge.style.color = wTotal2.valid ? '#2e7d32' : '#c62828';
        }
        updateWeightGate();
        markDirty();
      }
      if (e.target.classList.contains('soc-wait-days')) {
        var target = +e.target.dataset.waitfor;
        var val = Math.max(0, Math.min(90, parseInt(e.target.value, 10) || 0));
        if (SEQ.steps[target]) { SEQ.steps[target].waitDays = val; markDirty(); }
      }
      if (e.target.classList.contains('soc-wait-hours')) {
        var target2 = +e.target.dataset.waitfor;
        var val2 = Math.max(0, Math.min(23, parseInt(e.target.value, 10) || 0));
        if (SEQ.steps[target2]) { SEQ.steps[target2].waitHours = val2; markDirty(); }
      }
    });
  }

  /* ── branch conditions (Step 3, alongside Subsequences) ─────────────────
     Flat list of inline-editable cards -- each condition is one row of
     settings with no nested content of its own, so unlike Subsequences this
     never needs a separate expand/rail/editor. Source/YES/NO step dropdowns
     are built ONLY from SEQ.steps (the main sequence) -- subsequence steps
     are never offered, matching the server's own main-sequence-only
     resolution (see _validate_conditions' valid_step_client_ids). */

  var TRIGGER_LABELS = {
    no_event_after_days: 'No reply after N days', clicked: 'Clicked', opened: 'Opened', replied: 'Replied',
  };

  function stepOptionsHtml(selectedCid, includeNone) {
    var html = includeNone ? '<option value="">— None —</option>' : '<option value="">Choose a step…</option>';
    SEQ.steps.forEach(function (s, i) {
      html += '<option value="' + s.cid + '"' + (s.cid === selectedCid ? ' selected' : '') +
              '>Step ' + (i + 1) + '</option>';
    });
    return html;
  }

  // V4.0 -- '— Standalone —' plus every declared group, labeled by its
  // position + logic (groups have no name field of their own, matching
  // conditions/steps, which are likewise never named by the user).
  function groupOptionsHtml(selectedCid) {
    var html = '<option value="">— Standalone —</option>';
    GROUP.list.forEach(function (g, i) {
      html += '<option value="' + g.cid + '"' + (g.cid === selectedCid ? ' selected' : '') +
              '>Group ' + (i + 1) + ' (' + (g.logic === 'and' ? 'AND' : 'OR') + ')</option>';
    });
    return html;
  }

  function renderCondList() {
    var wrap = $('socCondList');
    if (!wrap) return;
    if (!COND.list.length) {
      wrap.innerHTML =
        '<div class="so-empty" style="padding:20px 0;border:none;">' +
          '<i class="fas fa-code-branch" style="font-size:22px;color:var(--ink-3);"></i>' +
          '<p style="margin-top:8px;">No branch conditions yet — add one to route prospects based on a reply, click, or open.</p>' +
        '</div>';
    } else {
      wrap.innerHTML = COND.list.map(function (c, i) {
        var grouped = !!c.groupCid;
        return (
          '<div class="soc-cond-card' + (c.isActive ? '' : ' inactive') + '" data-idx="' + i + '">' +
            '<div class="soc-cond-row">' +
              '<label class="soc-cond-field">Trigger' +
                '<select class="soc-cond-trigger">' +
                  Object.keys(TRIGGER_LABELS).filter(function (t) {
                    // A grouped condition can never use 'no_event_after_days'
                    // (see GROUP_TRIGGER_TYPES) -- omitted rather than shown-
                    // then-rejected, so the UI can't express an invalid
                    // combination in the first place.
                    return !grouped || GROUP_TRIGGER_TYPES.indexOf(t) !== -1;
                  }).map(function (t) {
                    return '<option value="' + t + '"' + (c.triggerType === t ? ' selected' : '') + '>' +
                           TRIGGER_LABELS[t] + '</option>';
                  }).join('') +
                '</select>' +
              '</label>' +
              // Source step / wait days live on the GROUP once a condition
              // is grouped (see SOSequenceCondition.group's model comment) --
              // hidden here rather than shown-and-ignored, and kept in sync
              // with the group's own value invisibly (syncAllGroupMemberSourceSteps).
              (grouped ? '' :
              '<label class="soc-cond-field">Source step' +
                '<select class="soc-cond-source">' + stepOptionsHtml(c.sourceCid, false) + '</select>' +
              '</label>' +
              '<label class="soc-cond-field soc-cond-field-sm">Wait days' +
                '<input type="number" class="soc-cond-wait" min="0" max="90" value="' + c.waitDays + '"/>' +
              '</label>') +
              // 'replied' never uses event_count_threshold (one genuine
              // reply is always sufficient — see _eval_replied) — hidden
              // rather than shown-disabled so the field doesn't invite a
              // value that would just be silently ignored server-side.
              // Each group member keeps its OWN threshold (V4.0 approved
              // scope), so this stays visible/editable when grouped too.
              (c.triggerType === 'replied' ? '' :
              '<label class="soc-cond-field soc-cond-field-sm">Min. count (optional)' +
                '<input type="number" class="soc-cond-threshold" min="0" placeholder="any" value="' +
                (c.threshold == null ? '' : c.threshold) + '"/>' +
              '</label>') +
              '<label class="soc-cond-field">Group (optional)' +
                '<select class="soc-cond-group">' + groupOptionsHtml(c.groupCid) + '</select>' +
              '</label>' +
            '</div>' +
            '<div class="soc-cond-row">' +
              (grouped ?
                '<p class="soc-cond-group-note" style="margin:0;flex:1;color:var(--ink-3);font-size:12px;">' +
                  'Source step, wait days, and YES/NO targets are configured on the group above (Condition Groups).' +
                '</p>'
              :
                '<label class="soc-cond-field">YES →' +
                  '<select class="soc-cond-yes">' + stepOptionsHtml(c.yesCid, true) + '</select>' +
                '</label>' +
                '<label class="soc-cond-field">NO →' +
                  '<select class="soc-cond-no">' + stepOptionsHtml(c.noCid, true) + '</select>' +
                '</label>'
              ) +
              '<label class="soc-cond-active-lbl"><input type="checkbox" class="soc-cond-active"' +
                (c.isActive ? ' checked' : '') + '/> Active</label>' +
              '<button type="button" class="soc-cond-del" title="Delete condition"><i class="fas fa-trash"></i></button>' +
            '</div>' +
          '</div>'
        );
      }).join('');
    }
    $('socAddCondition').disabled = COND.list.length >= MAX_CONDITIONS;
  }

  function clearConditionRefsToStep(stepCid) {
    if (!stepCid) return;
    var touched = false;
    COND.list.forEach(function (c) {
      if (c.sourceCid === stepCid) { c.sourceCid = ''; touched = true; }
      if (c.yesCid === stepCid)    { c.yesCid = '';    touched = true; }
      if (c.noCid === stepCid)     { c.noCid = '';     touched = true; }
    });
    // V4.0 -- a group's own source/YES/NO step references need the same
    // deleted-step cleanup as a standalone condition's.
    var groupTouched = false;
    GROUP.list.forEach(function (g) {
      if (g.sourceCid === stepCid) { g.sourceCid = ''; groupTouched = true; }
      if (g.yesCid === stepCid)    { g.yesCid = '';    groupTouched = true; }
      if (g.noCid === stepCid)     { g.noCid = '';     groupTouched = true; }
    });
    if (touched || groupTouched) {
      toast('A branch condition referenced the deleted step and was cleared — please review it.', 'warning');
      renderCondList();
      renderGroupList();
    }
  }

  // V4.0 -- keeps every grouped condition's OWN source_step_client_id in
  // sync with its group's, so the "every member must share the group's
  // source step" rule (enforced server-side in _validate_condition_groups)
  // can never actually be violated through this UI -- the member's own
  // source-step field is hidden once grouped (see renderCondList) and its
  // value is instead driven entirely by the group's, invisibly.
  function syncAllGroupMemberSourceSteps() {
    var byCid = {};
    GROUP.list.forEach(function (g) { byCid[g.cid] = g.sourceCid; });
    COND.list.forEach(function (c) {
      if (c.groupCid && byCid.hasOwnProperty(c.groupCid)) c.sourceCid = byCid[c.groupCid];
    });
  }

  // V4.0 -- SET_NULL semantics client-side: deleting a group detaches its
  // member conditions (they revert to standalone) rather than deleting
  // them, mirroring SOSequenceCondition.group's on_delete=SET_NULL.
  function deleteGroup(idx) {
    var g = GROUP.list[idx];
    if (!g) return;
    COND.list.forEach(function (c) { if (c.groupCid === g.cid) c.groupCid = ''; });
    GROUP.list.splice(idx, 1);
    renderGroupList();
    renderCondList();
    markDirty();
  }

  function wireCondList() {
    var wrap = $('socCondList');

    wrap.addEventListener('click', function (e) {
      var card = e.target.closest('.soc-cond-card');
      if (!card) return;
      var idx = +card.dataset.idx;
      if (e.target.closest('.soc-cond-del')) {
        COND.list.splice(idx, 1);
        renderCondList(); renderGroupList(); markDirty();
      }
    });

    wrap.addEventListener('change', function (e) {
      var card = e.target.closest('.soc-cond-card');
      if (!card) return;
      var c = COND.list[+card.dataset.idx];
      if (!c) return;
      if (e.target.classList.contains('soc-cond-trigger')) {
        c.triggerType = e.target.value;
        renderCondList();   // threshold field visibility depends on triggerType (replied hides it)
        if (c.groupCid) renderGroupList();   // member label (V4.0) includes each member's trigger type
        markDirty();
      }
      if (e.target.classList.contains('soc-cond-source'))  { c.sourceCid = e.target.value; markDirty(); }
      if (e.target.classList.contains('soc-cond-yes'))     { c.yesCid = e.target.value; markDirty(); }
      if (e.target.classList.contains('soc-cond-no'))      { c.noCid = e.target.value; markDirty(); }
      if (e.target.classList.contains('soc-cond-active'))  {
        c.isActive = e.target.checked; renderCondList(); markDirty();
      }
      if (e.target.classList.contains('soc-cond-group')) {
        c.groupCid = e.target.value;
        if (c.groupCid && GROUP_TRIGGER_TYPES.indexOf(c.triggerType) === -1) {
          // 'no_event_after_days' can't be a group member (see
          // GROUP_TRIGGER_TYPES) -- switched to a valid default rather
          // than leaving the payload in a state the server would reject.
          c.triggerType = 'clicked';
          toast('Trigger changed to "Clicked" — groups can\'t use "No reply after N days".', 'warning');
        }
        syncAllGroupMemberSourceSteps();
        renderCondList();
        renderGroupList();   // member count/label shown on the group card
        markDirty();
      }
    });

    wrap.addEventListener('input', function (e) {
      var card = e.target.closest('.soc-cond-card');
      if (!card) return;
      var c = COND.list[+card.dataset.idx];
      if (!c) return;
      if (e.target.classList.contains('soc-cond-wait')) {
        c.waitDays = Math.max(0, Math.min(90, parseInt(e.target.value, 10) || 0));
        markDirty();
      }
      if (e.target.classList.contains('soc-cond-threshold')) {
        var v = e.target.value;
        c.threshold = (v === '' ? null : Math.max(0, parseInt(v, 10) || 0));
        markDirty();
      }
    });

    $('socAddCondition').addEventListener('click', function () {
      if (COND.list.length >= MAX_CONDITIONS) return;
      COND.list.push(blankCondition());
      renderCondList(); markDirty();
    });
  }

  /* ── condition groups (V4.0, Step 3 alongside Branch Conditions) ────────
     Same flat-card shape as COND above. A group owns source step/wait
     days/YES/NO targets; membership is driven entirely by each condition's
     own groupCid (set from the "Group" dropdown in renderCondList), so this
     section only ever displays/edits the group's own fields plus a
     read-only member summary — never a drag-and-drop membership UI, kept
     deliberately as simple as the flat condition list it sits beside. */

  function groupMembersLabel(g) {
    var members = COND.list.filter(function (c) { return c.groupCid === g.cid; });
    if (!members.length) return '';
    return members.map(function (c) { return TRIGGER_LABELS[c.triggerType]; }).join(' + ');
  }

  function renderGroupList() {
    var wrap = $('socGroupList');
    if (!wrap) return;
    if (!GROUP.list.length) {
      wrap.innerHTML =
        '<div class="so-empty" style="padding:20px 0;border:none;">' +
          '<i class="fas fa-object-group" style="font-size:22px;color:var(--ink-3);"></i>' +
          '<p style="margin-top:8px;">No condition groups yet — create one to combine 2+ conditions with AND/OR logic.</p>' +
        '</div>';
    } else {
      wrap.innerHTML = GROUP.list.map(function (g, i) {
        var memberCount = COND.list.filter(function (c) { return c.groupCid === g.cid; }).length;
        var needsMore = memberCount < 2;
        return (
          '<div class="soc-cond-card' + (g.isActive ? '' : ' inactive') + '" data-idx="' + i + '">' +
            '<div class="soc-cond-row">' +
              '<label class="soc-cond-field">Logic' +
                '<select class="soc-group-logic">' +
                  '<option value="and"' + (g.logic === 'and' ? ' selected' : '') + '>AND — every condition must be true</option>' +
                  '<option value="or"' + (g.logic === 'or' ? ' selected' : '') + '>OR — any condition may be true</option>' +
                '</select>' +
              '</label>' +
              '<label class="soc-cond-field">Source step' +
                '<select class="soc-group-source">' + stepOptionsHtml(g.sourceCid, false) + '</select>' +
              '</label>' +
              '<label class="soc-cond-field soc-cond-field-sm">Wait days' +
                '<input type="number" class="soc-group-wait" min="0" max="90" value="' + g.waitDays + '"/>' +
              '</label>' +
            '</div>' +
            '<div class="soc-cond-row">' +
              '<label class="soc-cond-field">YES →' +
                '<select class="soc-group-yes">' + stepOptionsHtml(g.yesCid, true) + '</select>' +
              '</label>' +
              '<label class="soc-cond-field">NO →' +
                '<select class="soc-group-no">' + stepOptionsHtml(g.noCid, true) + '</select>' +
              '</label>' +
              '<label class="soc-cond-active-lbl"><input type="checkbox" class="soc-group-active"' +
                (g.isActive ? ' checked' : '') + '/> Active</label>' +
              '<button type="button" class="soc-group-del" title="Delete group"><i class="fas fa-trash"></i></button>' +
            '</div>' +
            '<p class="soc-cond-group-note" style="margin:2px 0 10px;padding:0 2px;font-size:12px;' +
              (needsMore ? 'color:#ef4444;' : 'color:var(--ink-3);') + '">' +
              (needsMore
                ? memberCount + ' condition(s) assigned — needs at least 2 (assign a condition to this group below in Branch Conditions).'
                : memberCount + ' conditions: ' + esc(groupMembersLabel(g))) +
            '</p>' +
          '</div>'
        );
      }).join('');
    }
    $('socAddGroup').disabled = GROUP.list.length >= MAX_GROUPS;
  }

  function wireGroupList() {
    var wrap = $('socGroupList');

    wrap.addEventListener('click', function (e) {
      var card = e.target.closest('.soc-cond-card');
      if (!card) return;
      var idx = +card.dataset.idx;
      if (e.target.closest('.soc-group-del')) {
        deleteGroup(idx);
      }
    });

    wrap.addEventListener('change', function (e) {
      var card = e.target.closest('.soc-cond-card');
      if (!card) return;
      var g = GROUP.list[+card.dataset.idx];
      if (!g) return;
      if (e.target.classList.contains('soc-group-logic')) {
        g.logic = e.target.value; renderCondList(); markDirty();
      }
      if (e.target.classList.contains('soc-group-source')) {
        g.sourceCid = e.target.value;
        syncAllGroupMemberSourceSteps();
        markDirty();
      }
      if (e.target.classList.contains('soc-group-yes')) { g.yesCid = e.target.value; markDirty(); }
      if (e.target.classList.contains('soc-group-no'))  { g.noCid = e.target.value; markDirty(); }
      if (e.target.classList.contains('soc-group-active')) {
        g.isActive = e.target.checked; renderGroupList(); markDirty();
      }
    });

    wrap.addEventListener('input', function (e) {
      var card = e.target.closest('.soc-cond-card');
      if (!card) return;
      var g = GROUP.list[+card.dataset.idx];
      if (!g) return;
      if (e.target.classList.contains('soc-group-wait')) {
        g.waitDays = Math.max(0, Math.min(90, parseInt(e.target.value, 10) || 0));
        markDirty();
      }
    });

    $('socAddGroup').addEventListener('click', function () {
      if (GROUP.list.length >= MAX_GROUPS) return;
      GROUP.list.push(blankGroup());
      renderGroupList(); renderCondList(); markDirty();
    });
  }

  /* ── init ────────────────────────────────────────────────────────────── */
  function hydrate() {
    var node = $('socEditingData');
    if (!node) {
      SEQ.steps = [{ cid: uid('s'), id: null, waitDays: 0, waitHours: 0, variants: [blankVariant('A')] }];
      SUBSEQ.list = [];   // opt-in — a new campaign starts with no subsequences
      COND.list = [];     // opt-in — a new campaign starts with no branch conditions
      GROUP.list = [];    // opt-in — a new campaign starts with no condition groups (V4.0)
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
    // Weights are keyed by account id in the saved payload (server sends
    // string keys since they came from a JSON object) — set BEFORE
    // wireCombos() runs so senderCombo's initial renderSenderWeights() call
    // reflects the real saved values, not the "1" default.
    var savedWeights = d.email_account_weights || {};
    Object.keys(savedWeights).forEach(function (accId) {
      var opt = $('socSenderList') && document.querySelector(
        '#socSenderList .soc-rd-option[data-type="account"][data-id="' + accId + '"]'
      );
      if (opt) opt.dataset.weight = String(savedWeights[accId]);
    });
    if (d.exclude_list_ids.length || d.exclude_segment_ids.length) {
      $('socExclSection').hidden = false;
      $('socExclToggle').style.display = 'none';
    }

    SEQ.steps = (d.sequence || []).map(function (s, i) {
      return {
        cid: uid('s'), id: s.id, waitDays: s.wait_days, waitHours: s.wait_hours,
        variants: (s.variants || []).map(function (v) {
          return { cid: uid('v'), id: v.id, label: v.label, name: v.name,
                   subject: v.subject, preheader: v.preheader, html: v.html_body,
                   weight: (v.weight != null ? v.weight : 1) };
        })
      };
    });
    if (!SEQ.steps.length) {
      SEQ.steps = [{ cid: uid('s'), id: null, waitDays: 0, waitHours: 0, variants: [blankVariant('A')] }];
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
                       subject: v.subject, preheader: v.preheader, html: v.html_body,
                       weight: (v.weight != null ? v.weight : 1) };
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

    // V4.0 -- groups hydrate BEFORE conditions (groupCidById needs
    // GROUP.list already populated), same real-db-id-then-resolve-to-
    // client-id pattern as steps/conditions.
    GROUP.list = (d.condition_groups || []).map(function (g) {
      return {
        cid: uid('g'), id: g.id, logic: g.logic,
        sourceCid: stepCidById(g.source_step_id), waitDays: g.wait_days,
        yesCid: stepCidById(g.yes_target_step_id), noCid: stepCidById(g.no_target_step_id),
        isActive: g.is_active !== false,
      };
    });

    // Step references arrive as real db ids (see _serialize_conditions) and
    // are converted back to this session's freshly-generated step client
    // ids via stepCidById -- a step that no longer exists (shouldn't happen
    // outside manual DB edits, since CASCADE/SET_NULL already keep this
    // consistent server-side) simply resolves to '' (not chosen).
    COND.list = (d.conditions || []).map(function (c) {
      return {
        cid: uid('c'), id: c.id, triggerType: c.trigger_type,
        sourceCid: stepCidById(c.source_step_id), waitDays: c.wait_days,
        threshold: (c.event_count_threshold == null ? null : c.event_count_threshold),
        yesCid: stepCidById(c.yes_target_step_id), noCid: stepCidById(c.no_target_step_id),
        isActive: c.is_active !== false,
        groupCid: groupCidById(c.group_id),
      };
    });

    if ($('socTrackingToggle')) {
      // d.tracking_enabled is always a real boolean once a campaign has been
      // saved at least once (server always returns the field) — the
      // `!== false` guard only matters for the narrow case of an editing
      // payload that somehow predates this field, where it should fall back
      // to the same safe "on" default a brand-new campaign starts with.
      $('socTrackingToggle').checked = d.tracking_enabled !== false;
    }

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
    /* Split into independently guarded phases -- same crash-isolation
       hardening as the main editor's quill/popovers/load/wire split
       (forensic-audit fix), so a mistake in one (e.g. the newly-added
       toolbar-parity wiring) can't silently take out the others. */
    phase('subsequence-list', function () { renderSubseqList(); wireSubseqList(); });
    phase('subsequence-rail', function () { wireSubRail(); wireSubDrag(); });
    phase('subsequence-quill', initSubQuill);
    phase('subsequence-popovers', buildSubPopovers);
    phase('subsequence-wire', wireSubEditor);
    phase('subsequence-merge', wireSubMergeMenu);
    phase('conditions', function () {
      renderGroupList();   // V4.0 -- rendered first: renderCondList's own Group
      wireGroupList();     // dropdown reads GROUP.list, so it must exist by then.
      renderCondList();
      wireCondList();
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

    /* 3. Editor last. Split into independently guarded phases (forensic-
       audit hardening) -- these four used to share one phase('editor', ...)
       callback, so an exception in an earlier call (e.g. initQuill()) would
       silently abort every later one in the same try/catch, including
       wireEditor() -- which is where socPreheaderToggle/socScoreDetails
       get their listeners. Each now fails independently and reports its
       own named phase to console.warn if it does. */
    phase('quill', initQuill);
    phase('popovers', buildPopovers);
    phase('load', loadEditor);
    phase('wire', wireEditor);

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
      function () { updateCapacityReadout(); renderSenderWeights(); });
    // Initial render — hydrate() has already checked the saved accounts and
    // set their data-weight attributes by this point (phase('state', hydrate)
    // runs before phase('recipients', wireCombos)), so this reflects the
    // real saved state on first paint, not just future changes.
    renderSenderWeights();
    if ($('socSenderWeights')) {
      $('socSenderWeights').addEventListener('input', function (e) {
        var input = e.target.closest('.soc-sender-weight-input');
        if (!input) return;
        var row = input.closest('.soc-sender-weight-row');
        var accId = row && row.dataset.accountId;
        var opt = accId && document.querySelector(
          '#socSenderList .soc-rd-option[data-type="account"][data-id="' + accId + '"]'
        );
        if (opt) {
          var val = Math.max(0, Math.min(1000, parseInt(input.value, 10) || 0));
          opt.dataset.weight = String(val);
          markDirty();
        }
      });
    }
    syncExclude();
    refreshEstimate();
  }

  function wireEditor() {
    wireTestTargetCombo();

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
      SEQ.steps.push({ cid: uid('s'), id: null, waitDays: 3, waitHours: 0, variants: [blankVariant('A')] });
      SEQ.sel = { step: SEQ.steps.length - 1, variant: 0 };
      renderRail(); loadEditor(); markDirty();
    });
    $('socDelCancel').addEventListener('click', function () {
      $('socDelModal').classList.remove('open'); SEQ.pendingDelete = null;
    });
    $('socDelConfirm').addEventListener('click', function () {
      if (SEQ.pendingDelete === null) return;
      var removedCid = SEQ.steps[SEQ.pendingDelete] && SEQ.steps[SEQ.pendingDelete].cid;
      SEQ.steps.splice(SEQ.pendingDelete, 1);
      SEQ.sel = { step: Math.max(0, Math.min(SEQ.sel.step, SEQ.steps.length - 1)), variant: 0 };
      SEQ.pendingDelete = null;
      $('socDelModal').classList.remove('open');
      clearConditionRefsToStep(removedCid);
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
    if ($('socTrackingToggle')) {
      $('socTrackingToggle').addEventListener('change', function () { markDirty(); });
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

  return { init: init, renderReviewSummary: renderReviewSummary, renderLaunchReadiness: renderLaunchReadiness };
})();
