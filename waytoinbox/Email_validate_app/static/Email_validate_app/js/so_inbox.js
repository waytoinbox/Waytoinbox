/*
 * so_inbox.js — Sales Outreach Inbox (conversation workspace).
 */
var SOInboxPage = (function () {
  'use strict';

  var URLS = window.SOI_URLS || {};
  var CSRF = window.SOI_CSRF || '';
  var MOBILE_BREAKPOINT = 860;

  var PERSONALIZATION_TAGS = [
    '{{first_name}}', '{{last_name}}', '{{full_name}}',
    '{{email}}', '{{company}}', '{{phone}}',
  ];

  var CLASSIFICATION_LABELS = {
    interested: 'Interested', meeting: 'Meeting', question: 'Question',
    not_interested: 'Not Interested', out_of_office: 'Out of Office',
    unsubscribe: 'Unsubscribed', wrong_person: 'Wrong Person',
    positive: 'Positive', negative: 'Negative',
  };

  var STATE = {
    folder: 'all', accountIds: [], campaignIds: [], statuses: [], classifications: [],
    tagId: '', q: '', sort: 'recent',
    page: 1, hasNext: false, loading: false,
    rows: [], selected: {},
    activeConversationId: null, activeConversation: null,
    composerMode: null, // null | 'reply' | 'forward'
  };

  // One shared Quill instance/toolbar/editor (#soiComposeEditor) backs
  // Reply, Forward, and Compose — see openComposer(mode, forwardItem).
  var quillCompose = null;
  var pendingAttachments = [];

  /* ── helpers ─────────────────────────────────────────────────────────── */

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function extend(a, b) {
    for (var k in b) { if (Object.prototype.hasOwnProperty.call(b, k)) a[k] = b[k]; }
    return a;
  }
  function toast(msg, kind) {
    if (window.WTI && WTI.toast) WTI.toast(msg, kind || 'success');
  }
  function timeAgo(iso) {
    if (!iso) return '';
    var diffMs = Date.now() - new Date(iso).getTime();
    var mins = Math.floor(diffMs / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    var hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + 'h ago';
    var days = Math.floor(hrs / 24);
    if (days < 7) return days + 'd ago';
    return new Date(iso).toLocaleDateString();
  }
  function formatDateTime(iso) {
    if (!iso) return '';
    return new Date(iso).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    });
  }

  function apiGet(url) {
    return fetch(url).then(function (r) { return r.json(); });
  }
  function apiPostJson(url, data) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify(data),
    }).then(function (r) { return r.json(); });
  }
  function doAction(action, ids, extra, cb) {
    var payload = extend({ action: action, conversation_ids: ids }, extra || {});
    apiPostJson(URLS.action, payload).then(function (d) {
      if (d.status !== 'ok') { toast(d.message || 'Action failed', 'error'); return; }
      if (cb) cb(d);
    }).catch(function () { toast('Action failed', 'error'); });
  }

  /* ── Conversation list ───────────────────────────────────────────────── */

  function buildListUrl(page) {
    var params = ['folder=' + encodeURIComponent(STATE.folder), 'page=' + page, 'sort=' + STATE.sort];
    if (STATE.accountIds.length) params.push('account_ids=' + encodeURIComponent(STATE.accountIds.join(',')));
    if (STATE.campaignIds.length) params.push('campaign_ids=' + encodeURIComponent(STATE.campaignIds.join(',')));
    if (STATE.statuses.length) params.push('statuses=' + encodeURIComponent(STATE.statuses.join(',')));
    if (STATE.classifications.length) params.push('classifications=' + encodeURIComponent(STATE.classifications.join(',')));
    if (STATE.tagId) params.push('tag_id=' + encodeURIComponent(STATE.tagId));
    if (STATE.q) params.push('q=' + encodeURIComponent(STATE.q));
    return URLS.conversations + '?' + params.join('&');
  }

  function loadConversations(opts) {
    opts = opts || {};
    var page = opts.page || 1;
    if (STATE.loading) return;
    STATE.loading = true;
    if (page === 1 && !opts.silent) $('soiConvList').innerHTML = '<div class="soi-list-loading">Loading…</div>';
    apiGet(buildListUrl(page)).then(function (d) {
      STATE.loading = false;
      if (d.status !== 'ok') { if (!opts.silent) toast('Failed to load conversations', 'error'); return; }
      updateFolderCounts(d.counts);
      updateFolderCounts(d.status_counts);
      updateFolderCounts(d.classification_counts);
      if (page === 1) {
        STATE.rows = d.conversations;
        renderConvList(STATE.rows);
      } else {
        STATE.rows = STATE.rows.concat(d.conversations);
        appendConvList(d.conversations);
      }
      STATE.page = d.page;
      STATE.hasNext = d.has_next;
      $('soiLoadMore').hidden = !d.has_next;
      STATE.total = d.total;
      updateBulkBar();
      if (opts.silent) maybeRefreshOpenThread();
    }).catch(function () { STATE.loading = false; if (!opts.silent) toast('Failed to load conversations', 'error'); });
  }

  function updateFolderCounts(counts) {
    Object.keys(counts || {}).forEach(function (folder) {
      var el = document.querySelector('[data-count-for="' + folder + '"]');
      if (!el) return;
      var n = counts[folder] || 0;
      el.textContent = n;
      // CSS can't test a text node's numeric value, so the "count > 0"
      // highlight (e.g. the Unread badge) is driven by this class instead of
      // :not(:empty) — an :empty check is true for "0" too since it's still a
      // text node, which made the badge permanently highlighted.
      el.classList.toggle('soi-count--nonzero', n > 0);
    });
  }

  function convCardHtml(c) {
    var cls = ['soi-conv-card'];
    if (c.is_unread) cls.push('unread');
    if (c.id === STATE.activeConversationId) cls.push('active');
    var badges = '<span class="soi-badge-sm direction-' + c.last_message_direction + '">' +
      (c.last_message_direction === 'inbound' ? 'Needs Reply' : 'Waiting') + '</span>';
    if (c.classification) {
      badges += '<span class="soi-badge-sm cls-' + c.classification + '">' +
        (CLASSIFICATION_LABELS[c.classification] || c.classification) + '</span>';
    }
    (c.tags || []).forEach(function (t) {
      badges += '<span class="soi-badge-sm" style="background:' + t.color + '22;color:' + t.color + ';">' + esc(t.name) + '</span>';
    });
    var companyLine = c.company ? (esc(c.company) + ' · ' + esc(c.email)) : esc(c.email);
    var campaignOrAccount = c.campaign_name ? esc(c.campaign_name) : esc(c.account_email || '');
    var msgCountBadge = c.message_count > 1
      ? '<span class="soi-conv-msgcount"><i class="fas fa-comment"></i> ' + c.message_count + '</span>'
      : '';
    return (
      '<div class="' + cls.join(' ') + '" data-id="' + c.id + '">' +
        '<label class="soi-conv-check"><input type="checkbox" data-select="' + c.id + '"' +
          (STATE.selected[c.id] ? ' checked' : '') + '/></label>' +
        '<div class="soi-conv-body">' +
          '<div class="soi-conv-top">' +
            '<span class="soi-conv-name">' + companyLine + '</span>' +
            '<span class="soi-conv-time">' + timeAgo(c.last_message_at) + '</span>' +
          '</div>' +
          '<div class="soi-conv-subject">' + esc(c.subject || '(no subject)') + '</div>' +
          '<div class="soi-conv-preview">' + esc(c.last_message_preview || '(no messages yet)') + '</div>' +
          '<div class="soi-conv-meta-row">' + badges +
            '<span class="soi-conv-campaign">' + campaignOrAccount + '</span>' + msgCountBadge +
          '</div>' +
        '</div>' +
      '</div>'
    );
  }

  function renderConvList(rows) {
    if (!rows.length) {
      $('soiConvList').innerHTML = '<div class="soi-list-empty">No conversations here.</div>';
      return;
    }
    $('soiConvList').innerHTML = rows.map(convCardHtml).join('');
    wireConvCards();
  }
  function appendConvList(rows) {
    var wrap = document.createElement('div');
    wrap.innerHTML = rows.map(convCardHtml).join('');
    while (wrap.firstChild) $('soiConvList').appendChild(wrap.firstChild);
    wireConvCards();
  }
  function wireConvCards() {
    document.querySelectorAll('.soi-conv-card').forEach(function (card) {
      card.addEventListener('click', function (e) {
        if (e.target.closest('.soi-conv-check')) return;
        openConversation(parseInt(card.dataset.id, 10));
      });
    });
    document.querySelectorAll('[data-select]').forEach(function (cb) {
      cb.addEventListener('change', function () {
        var id = cb.dataset.select;
        if (cb.checked) STATE.selected[id] = true; else delete STATE.selected[id];
        updateBulkBar();
      });
    });
  }
  function updateBulkBar() {
    var ids = Object.keys(STATE.selected);
    $('soiBulkMoreWrap').hidden = ids.length === 0;
    var total = STATE.total || 0;
    $('soiListCount').textContent = ids.length
      ? ids.length + ' selected'
      : total + ' conversation' + (total === 1 ? '' : 's');
    $('soiListCount').classList.toggle('soi-list-count--selecting', ids.length > 0);
  }

  /* ── Filters / folders ───────────────────────────────────────────────── */

  var searchDebounce = null;

  /* ── Multi-select filter dropdowns (Status / Sender Accounts / Campaigns /
     Classification) — same simple floating-menu behavior as .soi-more-menu:
     a plain button that opens a plain checkbox list, no chip rendering.
     One generic component drives all 4. ── */

  var multiSelects = []; // populated by initMultiSelect, used to close-all-but-one

  function closeAllMultiSelects(except) {
    multiSelects.forEach(function (m) {
      if (m.container !== except) {
        m.panel.classList.remove('open');
        m.trigger.classList.remove('open');
        m.trigger.setAttribute('aria-expanded', 'false');
      }
    });
  }

  function initMultiSelect(id, stateKey) {
    var container = $(id);
    if (!container) return;
    var trigger = container.querySelector('.soi-rd-trigger');
    var triggerLabel = container.querySelector('.soi-rd-trigger-label');
    var panel = container.querySelector('.soi-rd-panel');
    var searchInput = container.querySelector('.soi-rd-search');
    var placeholder = container.dataset.placeholder || '';
    var opts = Array.prototype.slice.call(container.querySelectorAll('.soi-rd-option'));

    function updateLabel() {
      var checked = opts.filter(function (o) { return o.querySelector('.soi-rd-chk').checked; });
      if (!checked.length) {
        triggerLabel.textContent = placeholder;
      } else if (checked.length === 1) {
        triggerLabel.textContent = checked[0].querySelector('.soi-rd-name').textContent;
      } else {
        triggerLabel.textContent = checked.length + ' selected';
      }
      trigger.classList.toggle('has-value', checked.length > 0);
    }

    function open() {
      closeAllMultiSelects(container);
      // Normal document flow (no position:fixed/absolute) — opening pushes
      // the rest of the sidebar down, so .soi-sidebar's own overflow-y:auto
      // scrollbar reaches it; no separate scroll region needed here.
      panel.classList.add('open');
      trigger.classList.add('open');
      trigger.setAttribute('aria-expanded', 'true');
      if (searchInput) { searchInput.value = ''; opts.forEach(function (o) { o.style.display = ''; }); searchInput.focus(); }
    }
    function close() {
      panel.classList.remove('open');
      trigger.classList.remove('open');
      trigger.setAttribute('aria-expanded', 'false');
    }

    trigger.addEventListener('click', function () {
      panel.classList.contains('open') ? close() : open();
    });
    trigger.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); panel.classList.contains('open') ? close() : open(); }
      if (e.key === 'Escape') close();
    });
    panel.addEventListener('click', function (e) { e.stopPropagation(); });

    opts.forEach(function (opt) {
      opt.querySelector('.soi-rd-chk').addEventListener('change', function () {
        opt.classList.toggle('selected', this.checked);
        STATE[stateKey] = opts.filter(function (o) { return o.querySelector('.soi-rd-chk').checked; })
          .map(function (o) { return o.querySelector('.soi-rd-chk').value; });
        updateLabel();
        loadConversations({ page: 1 });
      });
    });

    if (searchInput) {
      searchInput.addEventListener('input', function () {
        var q = this.value.trim().toLowerCase();
        opts.forEach(function (o) { o.style.display = (!q || o.dataset.label.indexOf(q) !== -1) ? '' : 'none'; });
      });
    }

    multiSelects.push({ container: container, trigger: trigger, panel: panel });
  }

  // Single-select variant of the same .soi-rd component (Compose's "From"
  // sender-account picker) — click an option to select it and close,
  // instead of the checkbox multi-select behavior above. Shares the same
  // open/close/outside-click machinery via the multiSelects array.
  function initSingleSelect(id, hiddenInputId) {
    var container = $(id);
    if (!container) return;
    var trigger = container.querySelector('.soi-rd-trigger');
    var triggerLabel = container.querySelector('.soi-rd-trigger-label');
    var panel = container.querySelector('.soi-rd-panel');
    var hiddenInput = $(hiddenInputId);
    var opts = Array.prototype.slice.call(container.querySelectorAll('.soi-rd-option'));

    function select(opt) {
      opts.forEach(function (o) { o.classList.remove('selected'); });
      opt.classList.add('selected');
      hiddenInput.value = opt.dataset.value;
      triggerLabel.textContent = opt.querySelector('.soi-rd-name').textContent;
      trigger.classList.add('has-value');
    }

    // position:fixed panel (see CSS) — computed here rather than relying on
    // static in-flow positioning, since this trigger sits inside a bordered
    // field row where an in-flow panel would grow the row's height instead
    // of floating over the rest of the form.
    function positionPanel() {
      var r = trigger.getBoundingClientRect();
      panel.style.left = r.left + 'px';
      panel.style.top = (r.bottom + 2) + 'px';
      // min-width, not width — options stay on one row (see CSS), so a long
      // email needs the panel free to grow past the trigger's own width
      // rather than being clamped to it.
      panel.style.minWidth = r.width + 'px';
    }
    function open() {
      closeAllMultiSelects(container);
      positionPanel();
      panel.classList.add('open'); trigger.classList.add('open'); trigger.setAttribute('aria-expanded', 'true');
    }
    function close() { panel.classList.remove('open'); trigger.classList.remove('open'); trigger.setAttribute('aria-expanded', 'false'); }

    trigger.addEventListener('click', function () {
      panel.classList.contains('open') ? close() : open();
    });
    trigger.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); panel.classList.contains('open') ? close() : open(); }
      if (e.key === 'Escape') close();
    });
    panel.addEventListener('click', function (e) { e.stopPropagation(); });
    opts.forEach(function (opt) {
      opt.addEventListener('click', function () { select(opt); close(); });
    });

    if (opts.length) select(opts[0]);
    multiSelects.push({ container: container, trigger: trigger, panel: panel });
  }

  document.addEventListener('click', function (e) {
    if (!e.target.closest('.soi-rd')) closeAllMultiSelects(null);
  });

  function wireFilters() {
    document.querySelectorAll('.soi-folder').forEach(function (btn) {
      btn.addEventListener('click', function () {
        STATE.folder = btn.dataset.folder;
        document.querySelectorAll('.soi-folder').forEach(function (b) {
          b.classList.toggle('active', b === btn);
        });
        loadConversations({ page: 1 });
      });
    });
    initMultiSelect('soiStatusFilter', 'statuses');
    initMultiSelect('soiAccountFilter', 'accountIds');
    initMultiSelect('soiCampaignFilter', 'campaignIds');
    initMultiSelect('soiClassificationFilter', 'classifications');
    $('soiSearch').addEventListener('input', function () {
      var val = this.value;
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(function () { STATE.q = val.trim(); loadConversations({ page: 1 }); }, 300);
    });
    document.querySelectorAll('.soi-tag-chip').forEach(function (chip) {
      chip.addEventListener('click', function () {
        var wasActive = chip.classList.contains('active');
        document.querySelectorAll('.soi-tag-chip').forEach(function (c) { c.classList.remove('active'); });
        STATE.tagId = wasActive ? '' : chip.dataset.tagId;
        if (!wasActive) chip.classList.add('active');
        loadConversations({ page: 1 });
      });
    });
    $('soiSort').addEventListener('change', function () {
      STATE.sort = this.value; loadConversations({ page: 1 });
    });
    $('soiSelectAll').addEventListener('change', function () {
      var checked = this.checked;
      STATE.selected = {};
      document.querySelectorAll('[data-select]').forEach(function (cb) {
        cb.checked = checked;
        if (checked) STATE.selected[cb.dataset.select] = true;
      });
      updateBulkBar();
    });
    $('soiLoadMore').addEventListener('click', function () {
      if (STATE.hasNext && !STATE.loading) loadConversations({ page: STATE.page + 1 });
    });
  }

  function wireBulkBar() {
    $('soiBulkMoreBtn').addEventListener('click', function (e) {
      e.stopPropagation();
      $('soiBulkMoreMenu').hidden = !$('soiBulkMoreMenu').hidden;
    });
    document.addEventListener('click', function () { $('soiBulkMoreMenu').hidden = true; });
    $('soiBulkMoreMenu').addEventListener('click', function (e) { e.stopPropagation(); });

    document.querySelectorAll('#soiBulkMoreMenu [data-bulk]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var ids = Object.keys(STATE.selected);
        if (!ids.length) return;
        doAction(btn.dataset.bulk, ids, {}, function () {
          toast('Updated ' + ids.length + ' conversation' + (ids.length === 1 ? '' : 's'));
          STATE.selected = {};
          updateBulkBar();
          loadConversations({ page: 1 });
        });
        $('soiBulkMoreMenu').hidden = true;
      });
    });
    $('soiBulkAddTag').addEventListener('click', function () {
      var ids = Object.keys(STATE.selected);
      $('soiBulkMoreMenu').hidden = true;
      if (!ids.length) return;
      var name = window.prompt('Tag name:');
      if (!name || !name.trim()) return;
      doAction('add_tag', ids, { value: name.trim() }, function () {
        toast('Tag added');
        STATE.selected = {};
        updateBulkBar();
        loadConversations({ page: 1 });
      });
    });
  }

  /* ── Thread ──────────────────────────────────────────────────────────── */

  function setMobilePane(showThread) {
    if (window.innerWidth > MOBILE_BREAKPOINT) return;
    var listCol = document.querySelector('.soi-list-col');
    var threadCol = document.querySelector('.soi-thread-col');
    if (listCol) listCol.classList.toggle('soi-mobile-hidden', showThread);
    if (threadCol) threadCol.classList.toggle('soi-mobile-hidden', !showThread);
  }

  function openConversation(id) {
    STATE.activeConversationId = id;
    document.querySelectorAll('.soi-conv-card').forEach(function (c) {
      c.classList.toggle('active', parseInt(c.dataset.id, 10) === id);
      if (parseInt(c.dataset.id, 10) === id) c.classList.remove('unread');
    });
    $('soiThreadEmpty').hidden = true;
    $('soiThread').hidden = false;
    $('soiTimeline').innerHTML = '<div class="soi-list-loading">Loading…</div>';
    setMobilePane(true);

    closeComposer();

    apiGet(URLS.thread_base + id + '/').then(function (d) {
      if (d.status !== 'ok') { toast(d.message || 'Failed to load conversation', 'error'); return; }
      STATE.activeConversation = d;
      var row = STATE.rows.filter(function (r) { return r.id === id; })[0];
      if (row) row.is_unread = false;
      renderThreadHeader(d);
      renderTimeline(d);
      renderMoreMenuExtras(d);
    }).catch(function () { toast('Failed to load conversation', 'error'); });
  }

  function lastMessage(d) {
    var messages = (d.timeline || []).filter(function (item) { return item.kind === 'message'; });
    return messages.length ? messages[messages.length - 1] : null;
  }

  function renderThreadHeader(d) {
    var conv = d.conversation;
    $('soiThreadAvatar').textContent = (conv.prospect_name || conv.email || '?').charAt(0).toUpperCase();
    $('soiThreadName').textContent = conv.prospect_name;

    var last = lastMessage(d);
    var fromEmail = last ? last.from_email : (conv.account_email || '—');
    $('soiThreadMeta').textContent = fromEmail;
    renderThreadBadges(conv);
    $('soiPausedBanner').hidden = !d.sequence || d.sequence.status !== 'stopped';
  }

  function tlItemHtml(item, isLastMessage) {
    if (item.kind === 'message') {
      var fromLine = esc(item.from_email) +
        (item.is_sequence_step ? ' <span class="soi-tl-auto">(automated)</span>' : '');
      var body = item.body_html && item.body_html.trim() ? item.body_html : esc(item.body_text || '(no content)');
      var dirIcon = item.direction === 'inbound'
        ? '<span class="soi-tl-dir-icon inbound"><i class="fas fa-inbox"></i> Received</span>'
        : '<span class="soi-tl-dir-icon outbound"><i class="fas fa-reply"></i> Sent</span>';
      return (
        '<div class="soi-tl-item ' + item.direction + '">' +
          '<div class="soi-tl-card">' +
            '<div class="soi-tl-card-head">' +
              '<div class="soi-tl-head-row">' +
                '<span class="soi-tl-subject">' + esc(item.subject || '(no subject)') + '</span>' +
                '<span class="soi-tl-datetime">' + formatDateTime(item.timestamp) + '</span>' +
              '</div>' +
              '<div class="soi-tl-head-row">' +
                '<span class="soi-tl-from"><b>From:</b> ' + fromLine + '</span>' +
                dirIcon +
              '</div>' +
              '<div class="soi-tl-head-row">' +
                '<span class="soi-tl-to"><b>To:</b> ' + esc(item.to_email) + '</span>' +
                '<button type="button" class="soi-tl-forward" data-forward-msg="' + item.id + '" title="Forward"><i class="fas fa-share"></i></button>' +
              '</div>' +
            '</div>' +
            '<div class="soi-tl-card-body">' + body +
              (item.has_attachments ? '<div class="soi-tl-attach"><i class="fas fa-paperclip"></i> Attachment</div>' : '') +
              (isLastMessage ? '<button type="button" class="soi-tl-reply-btn" data-reply-btn title="Reply"><i class="fas fa-reply"></i> Reply</button>' : '') +
            '</div>' +
          '</div>' +
        '</div>'
      );
    }
    if (item.kind === 'note') {
      return (
        '<div class="soi-tl-item note"><div class="soi-tl-bubble">' +
          '<div class="soi-tl-note-label"><i class="fas fa-sticky-note"></i> Internal note — ' + esc(item.author) + '</div>' +
          esc(item.body) +
        '</div></div>'
      );
    }
    if (item.kind === 'event') {
      var verb = item.event_type === 'opened' ? 'Prospect opened the email' : 'Prospect clicked a link';
      return '<div class="soi-tl-item event"><div class="soi-tl-bubble">' + verb + ' &middot; ' + formatDateTime(item.timestamp) + '</div></div>';
    }
    return '';
  }

  function wireTimelineActions() {
    $('soiTimeline').addEventListener('click', function (e) {
      var forwardBtn = e.target.closest('[data-forward-msg]');
      if (forwardBtn) {
        var d = STATE.activeConversation;
        if (!d) return;
        var msgId = parseInt(forwardBtn.dataset.forwardMsg, 10);
        var item = (d.timeline || []).filter(function (t) { return t.kind === 'message' && t.id === msgId; })[0];
        openComposer('forward', item);
        return;
      }
      if (e.target.closest('[data-reply-btn]')) {
        openComposer('reply');
      }
    });
  }

  function renderTimeline(d) {
    var timeline = d.timeline || [];
    var lastMessageIdx = -1;
    timeline.forEach(function (item, i) { if (item.kind === 'message') lastMessageIdx = i; });
    var html = timeline.map(function (item, i) { return tlItemHtml(item, i === lastMessageIdx); }).join('');
    if (d.sequence && d.sequence.status === 'stopped') {
      html += '<div class="soi-tl-item system"><div class="soi-tl-bubble">' +
        '<i class="fas fa-pause-circle"></i> Sequence paused because prospect replied.</div></div>';
    }
    $('soiTimeline').innerHTML = html || '<div class="soi-list-empty">No messages yet.</div>';
    $('soiTimeline').scrollTop = $('soiTimeline').scrollHeight;
  }

  function quoteForForward(d, last) {
    if (!last) return '';
    var header = 'From: ' + esc(last.from_email) + '<br/>Date: ' + esc(formatDateTime(last.timestamp)) +
      '<br/>Subject: ' + esc(last.subject || d.conversation.subject || '') +
      '<br/>To: ' + esc(last.to_email);
    var body = last.body_html && last.body_html.trim() ? last.body_html : esc(last.body_text || '');
    return '<p>---------- Forwarded message ----------</p><p>' + header + '</p><blockquote>' + body + '</blockquote>';
  }

  function setInlineReplyHidden(hidden) {
    var btn = document.querySelector('.soi-tl-reply-btn');
    if (btn) btn.hidden = hidden;
  }

  function defaultSubject(mode, conv) {
    var base = (conv && conv.subject) || '';
    if (mode === 'forward') return /^fwd:/i.test(base) ? base : (base ? ('Fwd: ' + base) : 'Fwd:');
    return /^re:/i.test(base) ? base : (base ? ('Re: ' + base) : 'Re:');
  }

  function renderThreadBadges(conv) {
    var html = '<span class="soi-badge-sm direction-' + conv.last_message_direction + '">' +
      (conv.last_message_direction === 'inbound' ? 'Needs Reply' : 'Waiting') + '</span>';
    if (conv.message_count > 1) {
      html += '<span class="soi-conv-msgcount"><i class="fas fa-comment"></i> ' + conv.message_count + '</span>';
    }
    $('soiThreadBadges').innerHTML = html;
  }

  // Pause Sequence / View Prospect / View Campaign only make sense for a
  // campaign-linked conversation — the server already no-ops pause_sequence
  // for one with no campaign_contact, so these entries stay hidden rather
  // than showing an action that would silently do nothing.
  function renderMoreMenuExtras(d) {
    var viewProspect = $('soiActViewProspect');
    viewProspect.hidden = !d.prospect_url;
    if (d.prospect_url) viewProspect.href = d.prospect_url;

    var viewCampaign = $('soiActViewCampaign');
    viewCampaign.hidden = !d.campaign_url;
    if (d.campaign_url) viewCampaign.href = d.campaign_url;

    $('soiActPauseSequence').hidden = !d.sequence;
  }

  function wireThreadActions() {
    $('soiActMore').addEventListener('click', function (e) {
      e.stopPropagation();
      $('soiMoreMenu').hidden = !$('soiMoreMenu').hidden;
    });
    document.addEventListener('click', function () { $('soiMoreMenu').hidden = true; });

    $('soiMoreMenu').addEventListener('click', function (e) {
      var btn = e.target.closest('button');
      if (!btn) return;
      var id = STATE.activeConversationId;
      if (!id) return;
      if (btn.dataset.classify !== undefined) {
        doAction('classify', [id], { value: btn.dataset.classify }, function () {
          toast('Updated');
          openConversation(id);
          loadConversations({ page: 1 });
        });
      } else if (btn.id === 'soiActAddTag') {
        var name = window.prompt('Tag name:');
        if (name && name.trim()) {
          doAction('add_tag', [id], { value: name.trim() }, function () {
            toast('Tag added');
            openConversation(id);
          });
        }
      } else if (btn.id === 'soiActUnsubscribe') {
        if (!window.confirm('Unsubscribe this prospect? They will no longer receive any emails.')) return;
        doAction('unsubscribe', [id], {}, function () {
          toast('Unsubscribed');
          openConversation(id);
          loadConversations({ page: 1 });
        });
      }
      $('soiMoreMenu').hidden = true;
    });

    $('soiActMarkUnread').addEventListener('click', function () {
      var id = STATE.activeConversationId;
      if (!id) return;
      doAction('mark_unread', [id], {}, function () {
        toast('Marked unread');
        loadConversations({ page: 1 });
      });
    });
    $('soiActArchive').addEventListener('click', function () {
      var id = STATE.activeConversationId;
      if (!id || !STATE.activeConversation) return;
      var isArchived = STATE.activeConversation.conversation.is_archived;
      doAction(isArchived ? 'unarchive' : 'archive', [id], {}, function () {
        toast(isArchived ? 'Unarchived' : 'Archived');
        loadConversations({ page: 1 });
      });
    });
    $('soiResumeBtn').addEventListener('click', function () {
      var id = STATE.activeConversationId;
      if (!id) return;
      doAction('resume_sequence', [id], {}, function () {
        toast('Sequence resumed');
        openConversation(id);
      });
    });
    $('soiActPauseSequence').addEventListener('click', function () {
      var id = STATE.activeConversationId;
      if (!id) return;
      doAction('pause_sequence', [id], {}, function () {
        toast('Sequence paused');
        openConversation(id);
        loadConversations({ page: 1 });
      });
    });
    $('soiThreadBack').addEventListener('click', function () {
      setMobilePane(false);
    });
  }

  /* ── Composer — one shared Quill instance/editor/toolbar for Reply,
     Forward, and Compose (see openComposer(mode, forwardItem) below) ────── */

  var SIZE_STEPS = ['12px', '14px', '16px', '18px', '24px', '32px'];
  var LINEHEIGHT_STEPS = ['1', '1.2', '1.5', '1.8', '2'];

  // Icon + value + up/down stepper for Font size / Line height — not a
  // Quill picker at all, just steps the current selection's format through
  // its whitelist and mirrors the new value in the small display span.
  function stepFormat(formatName, steps, dir, displayEl) {
    if (!quillCompose) return;
    var range = quillCompose.getSelection(true);
    if (!range) return;
    var current = quillCompose.getFormat(range)[formatName] || steps[0];
    var idx = steps.indexOf(current);
    if (idx === -1) idx = 0;
    idx = dir === 'up' ? Math.min(steps.length - 1, idx + 1) : Math.max(0, idx - 1);
    quillCompose.format(formatName, steps[idx]);
    if (displayEl) displayEl.textContent = steps[idx].replace('px', '');
    quillCompose.focus();
  }

  function updateStepperDisplays() {
    if (!quillCompose) return;
    var range = quillCompose.getSelection();
    var fmt = range ? quillCompose.getFormat(range) : {};
    $('soiComposeSizeValue').textContent = (fmt.size || '14px').replace('px', '');
    $('soiComposeLineHeightValue').textContent = fmt.lineheight || '1';
  }

  function initComposeEditor() {
    if (typeof Quill === 'undefined') return;

    var Size = Quill.import('attributors/style/size');
    Size.whitelist = SIZE_STEPS;
    Quill.register(Size, true);
    Quill.register(Quill.import('attributors/style/color'), true);
    Quill.register(Quill.import('attributors/style/align'), true);
    // Line height — Parchment's class name differs between Quill 1.x and 2.x;
    // degrades to a no-op select if neither shape is found.
    try {
      var Parchment = Quill.import('parchment');
      var StyleAttr = Parchment.StyleAttributor || (Parchment.Attributor && Parchment.Attributor.Style);
      if (StyleAttr) {
        var LineHeight = new StyleAttr('lineheight', 'line-height', {
          scope: Parchment.Scope.BLOCK, whitelist: LINEHEIGHT_STEPS,
        });
        Quill.register({ 'formats/lineheight': LineHeight }, true);
      }
    } catch (e) { /* no-op */ }

    quillCompose = new Quill('#soiComposeEditor', {
      theme: 'snow',
      modules: {
        toolbar: '#soiComposeToolbar',
        history: { delay: 800, maxStack: 200, userOnly: true },
        table: true,
      },
      placeholder: 'Write your message…',
    });

    updateStepperDisplays();
    quillCompose.on('selection-change', updateStepperDisplays);
    quillCompose.on('text-change', updateStepperDisplays);

    // Quill's Colour picker and the Link tooltip are position:absolute,
    // anchored (via CSS top:100% or inline style offsets) relative to an
    // ancestor inside .soi-compose-modal's own overflow-y:auto scroll box —
    // so either could open near that box's edge and get clipped or hidden.
    // Reposition to position:fixed with real viewport coordinates once Quill
    // actually opens it, so it escapes that clipping entirely (same fix as
    // the .soi-tb-pop popovers).
    document.querySelectorAll('#soiComposeToolbar .ql-picker').forEach(function (picker) {
      picker.addEventListener('click', function () {
        var options = picker.querySelector('.ql-picker-options');
        if (!options || !picker.classList.contains('ql-expanded')) return;
        var r = picker.getBoundingClientRect();
        options.style.position = 'fixed';
        options.style.left = r.left + 'px';
        options.style.top = (r.bottom + 2) + 'px';
        options.style.margin = '0';
        options.style.zIndex = '200';
      });
    });

    var tooltipEl = document.querySelector('#soiComposeEditor .ql-tooltip');
    if (tooltipEl && typeof MutationObserver !== 'undefined') {
      // Centered in the modal rather than converting Quill's own
      // container-relative left/top (which depends on exactly where the
      // selection sits and was landing off-position or offscreen) — a
      // fixed, reliable spot beats a fragile coordinate conversion here.
      new MutationObserver(function () {
        if (tooltipEl.classList.contains('ql-hidden')) return;
        var modalEl = document.querySelector('.soi-compose-modal');
        var mRect = modalEl ? modalEl.getBoundingClientRect() : { left: 0, width: window.innerWidth, top: 0 };
        tooltipEl.style.position = 'fixed';
        tooltipEl.style.left = (mRect.left + mRect.width / 2) + 'px';
        tooltipEl.style.top = (mRect.top + 90) + 'px';
        tooltipEl.style.transform = 'translateX(-50%)';
        tooltipEl.style.margin = '0';
        tooltipEl.style.zIndex = '200';
      }).observe(tooltipEl, { attributes: true, attributeFilter: ['class'] });
    }
  }

  /* ── Toolbar insert popovers: merge tag / emoji / table ────────────────── */

  var EMOJI = ('😀 😃 😄 😁 😊 😍 🤩 😎 🤝 👋 👍 👏 🙌 💪 🙏 🤔 😅 😉 🥳 🚀 ' +
               '✨ ⭐ 🔥 💡 🎯 📈 📊 💼 🏆 ✅ ❗ ❓ ⏰ 📅 📌 🔔 💬 📣 📩 📬 ' +
               '💰 💳 🎁 🎉 🧠 🛠️ ⚙️ 🔍 🔗 📎 📝 📄 🗂️ 🧩 🌟 ☀️ 🌍 ⚡ 🥇 🤖').split(' ');

  function positionTbPop(pop, anchor) {
    var r = anchor.getBoundingClientRect();
    pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 260)) + 'px';
    pop.style.top = (r.bottom + 6) + 'px';
  }
  function closeTbPops() {
    ['soiComposeMergePop', 'soiComposeEmojiPop', 'soiComposeTablePop'].forEach(function (id) { $(id).classList.remove('open'); });
    document.querySelectorAll('.soi-tb-btn.active').forEach(function (b) { b.classList.remove('active'); });
  }
  function toggleTbPop(pop, anchor) {
    var wasOpen = pop.classList.contains('open');
    closeTbPops();
    if (!wasOpen) { positionTbPop(pop, anchor); pop.classList.add('open'); anchor.classList.add('active'); }
  }
  function insertAtCursor(quill, text) {
    if (!quill) return;
    var range = quill.getSelection(true);
    quill.insertText(range ? range.index : quill.getLength(), text, 'user');
  }
  function insertTableInto(quill, rows, cols) {
    if (!quill) return;
    var mod = quill.getModule('table');
    if (mod && typeof mod.insertTable === 'function') {
      quill.focus();
      mod.insertTable(rows, cols);
    } else {
      toast('Tables are not supported in this editor.', 'error');
    }
  }
  // getQuill is a getter (not the instance directly) because quillCompose is
  // assigned after this runs — the click handlers below must read its
  // current value at click time, not capture it here.
  function buildInsertPopovers(getQuill, ids) {
    $(ids.mergeList).innerHTML = PERSONALIZATION_TAGS.map(function (t) {
      return '<button type="button" data-tag="' + t + '">' + t + '</button>';
    }).join('');
    $(ids.mergeList).addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (b) { insertAtCursor(getQuill(), b.dataset.tag); closeTbPops(); }
    });

    $(ids.emojiGrid).innerHTML = EMOJI.map(function (e) { return '<button type="button">' + e + '</button>'; }).join('');
    $(ids.emojiGrid).addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (b) { insertAtCursor(getQuill(), b.textContent); closeTbPops(); }
    });

    var grid = $(ids.tableGrid), html = '';
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
      $(ids.tableLbl).textContent = R + ' × ' + C;
    });
    grid.addEventListener('click', function (e) {
      var cell = e.target.closest('span');
      if (!cell) return;
      insertTableInto(getQuill(), +cell.dataset.r, +cell.dataset.c);
      closeTbPops();
    });
  }

  function wireImageInsert(getQuill, btnId, inputId) {
    $(btnId).addEventListener('click', function () { $(inputId).click(); });
    $(inputId).addEventListener('change', function () {
      var file = this.files && this.files[0];
      this.value = '';
      var quill = getQuill();
      if (!file || !quill) return;
      var fd = new FormData();
      fd.append('image', file);
      toast('Uploading image…', 'info');
      fetch(URLS.upload_image, { method: 'POST', headers: { 'X-CSRFToken': CSRF }, body: fd })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.status !== 'ok' || !d.url) { toast(d.message || 'Upload failed.', 'error'); return; }
          var abs = new URL(d.url, window.location.origin).href;
          var range = quill.getSelection(true);
          quill.insertEmbed(range ? range.index : 0, 'image', abs, 'user');
        })
        .catch(function () { toast('Upload failed.', 'error'); });
    });
  }

  function toggleCcBccRow(rowId, toggleId, inputId) {
    var row = $(rowId), toggle = $(toggleId);
    var show = row.hidden;
    row.hidden = !show;
    toggle.classList.toggle('active', show);
    if (show) $(inputId).focus(); else $(inputId).value = '';
  }

  function wireNoteModal() {
    $('soiNoteBtn').addEventListener('click', function () {
      if (!STATE.activeConversationId) return;
      $('soiNoteText').value = '';
      $('soiNoteModalOverlay').hidden = false;
    });
    $('soiNoteCancel').addEventListener('click', function () { $('soiNoteModalOverlay').hidden = true; });
    $('soiNoteModalOverlay').addEventListener('click', function (e) {
      if (e.target === $('soiNoteModalOverlay')) $('soiNoteModalOverlay').hidden = true;
    });
    $('soiNoteSave').addEventListener('click', function () {
      var id = STATE.activeConversationId;
      var body = $('soiNoteText').value.trim();
      if (!id || !body) return;
      apiPostJson(URLS.note, { conversation_id: id, body: body }).then(function (d) {
        if (d.status !== 'ok') { toast(d.message || 'Failed to save note', 'error'); return; }
        $('soiNoteModalOverlay').hidden = true;
        toast('Note saved');
        openConversation(id);
      });
    });
  }

  /* ── Reply / Forward / Compose all open the same modal + editor ────────── */

  function openComposer(mode, forwardItem) {
    var conv = STATE.activeConversation && STATE.activeConversation.conversation;
    if (mode !== 'compose' && !conv) return;
    STATE.composerMode = mode;
    setInlineReplyHidden(true);

    $('soiComposeModalTitle').textContent = mode === 'reply' ? 'Reply' : mode === 'forward' ? 'Forward' : 'New Message';
    $('soiComposeFields').classList.remove('collapsed');
    $('soiComposeFieldsToggle').classList.remove('collapsed');
    $('soiComposeFieldsToggle').title = 'Hide fields';
    document.querySelector('.soi-compose-modal').style.height = '';

    // From: fixed account text for reply/forward (the open conversation's own
    // account); the account picker only makes sense for a fresh compose.
    var accountRow = $('soiComposeAccountRd');
    if (mode === 'compose') {
      $('soiComposeFromStatic').hidden = true;
      if (accountRow) accountRow.hidden = false;
    } else {
      $('soiComposeFromStatic').hidden = false;
      $('soiComposeFromStatic').textContent = conv.account_email || '—';
      if (accountRow) accountRow.hidden = true;
    }

    // To: fixed for reply (the conversation's counterpart), editable for
    // forward and compose.
    if (mode === 'reply') {
      $('soiComposeToStatic').hidden = false;
      $('soiComposeToStatic').textContent = conv.email;
      $('soiComposeTo').hidden = true;
    } else {
      $('soiComposeToStatic').hidden = true;
      $('soiComposeTo').hidden = false;
    }
    $('soiComposeTo').value = '';

    $('soiComposeSubject').value = mode === 'compose' ? '' : defaultSubject(mode, conv);
    $('soiComposeCc').value = '';
    $('soiComposeBcc').value = '';
    $('soiComposeCcRow').hidden = true;
    $('soiComposeBccRow').hidden = true;
    $('soiComposeCcToggle').classList.remove('active');
    $('soiComposeBccToggle').classList.remove('active');

    if (mode === 'compose') {
      var firstAccountOpt = document.querySelector('#soiComposeAccountRd .soi-rd-option');
      if (firstAccountOpt) firstAccountOpt.click();
    }

    if (quillCompose) {
      if (mode === 'forward') quillCompose.root.innerHTML = quoteForForward(STATE.activeConversation, forwardItem || lastMessage(STATE.activeConversation));
      else quillCompose.setText('');
    }
    updateStepperDisplays();
    pendingAttachments = [];
    $('soiComposeAttachInput').value = '';
    $('soiComposeAttachList').textContent = '';
    $('soiComposeModalOverlay').hidden = false;
    if (mode === 'forward' || mode === 'compose') $('soiComposeTo').focus();
    if (quillCompose) quillCompose.focus();
  }

  function closeComposer() {
    STATE.composerMode = null;
    $('soiComposeModalOverlay').hidden = true;
    setInlineReplyHidden(false);
    closeTbPops();
  }

  function wireComposer() {
    $('soiComposeBtn').addEventListener('click', function () { openComposer('compose'); });
    // Deliberately no click-outside-to-close — a half-written reply/forward/
    // compose is easy to lose to a stray click; closing this is only ever
    // the X button (closeComposer) or a successful Send.
    $('soiComposeModalClose').addEventListener('click', closeComposer);
    $('soiComposeFieldsToggle').addEventListener('click', function () {
      var modal = document.querySelector('.soi-compose-modal');
      var fields = $('soiComposeFields');
      var collapsing = !fields.classList.contains('collapsed');
      // Measure BEFORE collapsing — fields must still be visible/taking up
      // space for this height to be the "before" size worth locking in.
      // Reading it after toggling would just capture the already-shrunk
      // height, pinning the modal to its new smaller size instead of
      // preventing the shrink (the actual bug here previously).
      if (collapsing) modal.style.height = modal.getBoundingClientRect().height + 'px';
      fields.classList.toggle('collapsed', collapsing);
      if (!collapsing) modal.style.height = '';
      this.classList.toggle('collapsed', collapsing);
      this.title = collapsing ? 'Show fields' : 'Hide fields';
    });
    $('soiComposeCcToggle').addEventListener('click', function () {
      toggleCcBccRow('soiComposeCcRow', 'soiComposeCcToggle', 'soiComposeCc');
    });
    $('soiComposeBccToggle').addEventListener('click', function () {
      toggleCcBccRow('soiComposeBccRow', 'soiComposeBccToggle', 'soiComposeBcc');
    });

    buildInsertPopovers(function () { return quillCompose; }, {
      mergeList: 'soiComposeMergeList', emojiGrid: 'soiComposeEmojiGrid',
      tableGrid: 'soiComposeTableGrid', tableLbl: 'soiComposeTableLbl',
    });
    $('soiComposeTbMerge').addEventListener('click', function (e) { e.stopPropagation(); toggleTbPop($('soiComposeMergePop'), this); });
    $('soiComposeTbEmoji').addEventListener('click', function (e) { e.stopPropagation(); toggleTbPop($('soiComposeEmojiPop'), this); });
    $('soiComposeTbTable').addEventListener('click', function (e) { e.stopPropagation(); toggleTbPop($('soiComposeTablePop'), this); });
    $('soiComposeTbUndo').addEventListener('click', function () { if (quillCompose) quillCompose.history.undo(); });
    $('soiComposeTbRedo').addEventListener('click', function () { if (quillCompose) quillCompose.history.redo(); });
    wireImageInsert(function () { return quillCompose; }, 'soiComposeTbImage', 'soiComposeImageInput');

    $('soiComposeSizeUp').addEventListener('click', function () { stepFormat('size', SIZE_STEPS, 'up', $('soiComposeSizeValue')); });
    $('soiComposeSizeDown').addEventListener('click', function () { stepFormat('size', SIZE_STEPS, 'down', $('soiComposeSizeValue')); });
    $('soiComposeLineHeightUp').addEventListener('click', function () { stepFormat('lineheight', LINEHEIGHT_STEPS, 'up', $('soiComposeLineHeightValue')); });
    $('soiComposeLineHeightDown').addEventListener('click', function () { stepFormat('lineheight', LINEHEIGHT_STEPS, 'down', $('soiComposeLineHeightValue')); });

    $('soiComposeAttachBtn').addEventListener('click', function () { $('soiComposeAttachInput').click(); });
    $('soiComposeAttachInput').addEventListener('change', function () {
      pendingAttachments = Array.prototype.slice.call(this.files).slice(0, 5);
      $('soiComposeAttachList').textContent = pendingAttachments.length
        ? pendingAttachments.map(function (f) { return f.name; }).join(', ')
        : '';
    });

    $('soiComposeSendBtn').addEventListener('click', function () {
      if (!quillCompose || quillCompose.getText().trim() === '') { toast('Write a message first', 'error'); return; }
      var mode = STATE.composerMode;
      var subject = $('soiComposeSubject').value.trim();
      var fd = new FormData();
      fd.append('body_html', quillCompose.root.innerHTML);
      fd.append('subject', subject);
      fd.append('cc_email', $('soiComposeCc').value.trim());
      fd.append('bcc_email', $('soiComposeBcc').value.trim());
      pendingAttachments.forEach(function (f) { fd.append('attachments', f); });

      var url, successMsg, onSuccess;
      if (mode === 'compose') {
        var accSel = $('soiComposeAccount');
        if (!accSel) { toast('Connect a sender account first', 'error'); return; }
        var toEmail = $('soiComposeTo').value.trim();
        if (!toEmail || toEmail.indexOf('@') === -1) { toast('Enter a valid recipient', 'error'); return; }
        if (!subject) { toast('Subject is required', 'error'); return; }
        fd.append('account_id', accSel.value);
        fd.append('to_email', toEmail);
        url = URLS.compose;
        successMsg = 'Message sent';
        onSuccess = function (d) {
          loadConversations({ page: 1 });
          if (d.conversation_id) openConversation(d.conversation_id);
        };
      } else {
        var id = STATE.activeConversationId;
        if (!id) return;
        var isForward = mode === 'forward';
        var toEmail2 = isForward
          ? $('soiComposeTo').value.trim()
          : (STATE.activeConversation && STATE.activeConversation.conversation.email);
        if (!toEmail2 || toEmail2.indexOf('@') === -1) {
          toast(isForward ? 'Enter a valid recipient to forward to' : 'Enter a valid recipient', 'error');
          return;
        }
        fd.append('conversation_id', id);
        fd.append('to_email', toEmail2);
        if (isForward) fd.append('forward', '1');
        url = URLS.reply;
        successMsg = isForward ? 'Forwarded' : 'Reply sent';
        onSuccess = function () {
          openConversation(id);
          loadConversations({ page: 1 });
        };
      }

      $('soiComposeSendBtn').disabled = true;
      fetch(url, { method: 'POST', headers: { 'X-CSRFToken': CSRF }, body: fd })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          $('soiComposeSendBtn').disabled = false;
          if (d.status !== 'ok') { toast(d.message || 'Send failed', 'error'); return; }
          toast(successMsg);
          closeComposer();
          onSuccess(d);
        })
        .catch(function () { $('soiComposeSendBtn').disabled = false; toast('Send failed', 'error'); });
    });
  }

  /* ── Auto-refresh polling ────────────────────────────────────────────── */
  // New mail should appear without a manual refresh. Polls the list (not
  // sockets/IMAP IDLE — see services/so_imap.py for why), paused while the
  // tab is hidden and skipped mid-draft/mid-typing so a poll never disrupts
  // an in-progress reply or search.

  var POLL_INTERVAL_MS = 25000;
  var pollTimer = null;

  function pollTick() {
    if (STATE.loading || STATE.composerMode) return;
    if (document.activeElement === $('soiSearch')) return;
    loadConversations({ page: 1, silent: true });
  }

  function maybeRefreshOpenThread() {
    var id = STATE.activeConversationId;
    if (!id || STATE.composerMode) return;
    var row = STATE.rows.filter(function (r) { return r.id === id; })[0];
    var current = STATE.activeConversation;
    if (!row || !current) return;
    if (row.last_message_at && row.last_message_at !== current.conversation.last_message_at) {
      openConversation(id);
    }
  }

  function startPolling() {
    pollTimer = setInterval(pollTick, POLL_INTERVAL_MS);
    document.addEventListener('visibilitychange', function () {
      clearInterval(pollTimer);
      if (!document.hidden) {
        pollTick();
        pollTimer = setInterval(pollTick, POLL_INTERVAL_MS);
      }
    });
  }

  /* ── Init ────────────────────────────────────────────────────────────── */

  function init() {
    wireFilters();
    wireBulkBar();
    wireThreadActions();
    wireTimelineActions();
    wireComposer();
    initComposeEditor();
    wireNoteModal();
    initSingleSelect('soiComposeAccountRd', 'soiComposeAccount');
    document.addEventListener('click', closeTbPops);
    loadConversations({ page: 1 });
    startPolling();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return { init: init };
})();
