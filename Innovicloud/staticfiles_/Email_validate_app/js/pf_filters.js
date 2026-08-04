/* pf_filters.js v1.0 — Shared filter bar utilities */

const PF = (() => {
  /* ── Internal state ─────────────────────────────────── */
  let _cfg = {
    debounce: 300,
    urlSync:  true,
  };
  let _debounceTimer = null;
  let _abortCtrl    = null;

  /* ── Config ─────────────────────────────────────────── */
  function init(cfg) {
    _cfg = { ..._cfg, ...cfg };
  }

  /* ── Read current filter values ─────────────────────── */
  function getFilters() {
    return {
      search:   (_el('pfSearch')?.value   || '').trim(),
      status:   (_el('pfStatus')?.value   || '').trim().toLowerCase(),
      dateFrom: _el('pfDateFrom')?.value  || '',
      dateTo:   _el('pfDateTo')?.value    || '',
    };
  }

  /* ── Date validation: swap if from > to ─────────────── */
  function validateDates() {
    const f = _el('pfDateFrom');
    const t = _el('pfDateTo');
    if (f && t && f.value && t.value && f.value > t.value) {
      [f.value, t.value] = [t.value, f.value];
    }
  }

  /* ── Show/hide Clear button ─────────────────────────── */
  function updateClearBtn() {
    const f      = getFilters();
    const active = f.search || f.status || f.dateFrom || f.dateTo;
    const btn    = _el('pfClearBtn');
    const sc     = _el('pfSearchClear');
    if (btn) btn.style.display = active    ? '' : 'none';
    if (sc)  sc.style.display  = f.search ? '' : 'none';
  }

  /* ── Result counter ("Showing X of Y") ──────────────── */
  function setCount(filtered, total) {
    const el = _el('pfCount');
    if (!el) return;
    if (total === undefined || filtered === total) {
      el.innerHTML = '';
    } else {
      el.innerHTML =
        `Showing <strong>${filtered.toLocaleString()}</strong>` +
        ` of <strong>${total.toLocaleString()}</strong>`;
    }
  }

  /* ── Loading state ───────────────────────────────────── */
  function setLoading(on) {
    const sp  = _el('pfSpinner');
    const bar = _el('pfBar');
    if (sp)  sp.style.display = on ? 'inline-block' : 'none';
    if (bar) bar.classList.toggle('pf-loading', on);
  }

  /* ── Empty state ─────────────────────────────────────── */
  function showEmpty(show, msg) {
    const el = _el('pfEmptyState');
    if (!el) return;
    el.style.display = show ? '' : 'none';
    if (show && msg) {
      const p = el.querySelector('p');
      if (p) p.textContent = msg;
    }
  }

  /* ── Error state ─────────────────────────────────────── */
  function showError(show, onRetry) {
    const el = _el('pfErrorState');
    if (!el) return;
    el.style.display = show ? '' : 'none';
    if (show && onRetry) {
      const btn = el.querySelector('.pf-error-btn');
      if (btn) btn.onclick = onRetry;
    }
  }

  /* ── URL synchronisation ─────────────────────────────── */
  function syncURL(extra) {
    if (!_cfg.urlSync) return;
    const f = getFilters();
    const p = new URLSearchParams(window.location.search);
    ['search', 'status', 'date_from', 'date_to', 'page'].forEach(k => p.delete(k));
    if (f.search)   p.set('search',    f.search);
    if (f.status)   p.set('status',    f.status);
    if (f.dateFrom) p.set('date_from', f.dateFrom);
    if (f.dateTo)   p.set('date_to',   f.dateTo);
    if (extra) {
      Object.entries(extra).forEach(([k, v]) =>
        v != null ? p.set(k, String(v)) : p.delete(k));
    }
    history.replaceState(null, '', '?' + p.toString());
  }

  /* ── Restore filters from URL on page load ───────────── */
  function restoreFromURL() {
    const p = new URLSearchParams(window.location.search);
    [
      ['pfSearch',   'search'],
      ['pfStatus',   'status'],
      ['pfDateFrom', 'date_from'],
      ['pfDateTo',   'date_to'],
    ].forEach(([id, key]) => {
      const el = _el(id);
      if (el && p.get(key)) el.value = p.get(key);
    });
    updateClearBtn();
  }

  /* ── AJAX request cancellation ───────────────────────── */
  function cancelPrev() {
    if (_abortCtrl) _abortCtrl.abort();
    _abortCtrl = new AbortController();
    return _abortCtrl.signal;
  }

  /* ── Clear all filters ───────────────────────────────── */
  function clear(cb) {
    ['pfSearch', 'pfStatus', 'pfDateFrom', 'pfDateTo'].forEach(id => {
      const el = _el(id); if (el) el.value = '';
    });
    updateClearBtn();
    if (cb) cb();
  }

  /* ── Wire all filter inputs to a callback ────────────── */
  function wire(cb) {
    const run = () => { validateDates(); updateClearBtn(); cb(); };
    const debounced = () => {
      clearTimeout(_debounceTimer);
      _debounceTimer = setTimeout(run, _cfg.debounce);
    };

    const s = _el('pfSearch');
    if (s) {
      s.addEventListener('input', debounced);
      _el('pfSearchClear')?.addEventListener('click', () => {
        s.value = ''; updateClearBtn(); run();
      });
    }
    ['pfStatus', 'pfDateFrom', 'pfDateTo'].forEach(id =>
      _el(id)?.addEventListener('change', run)
    );
    _el('pfClearBtn')?.addEventListener('click', () => clear(cb));
  }

  /* ── Client-side record matching ─────────────────────── */
  function matchesSearch(haystack, needle) {
    return !needle || haystack.toLowerCase().includes(needle.toLowerCase());
  }
  function matchesStatus(rowStatus, filterStatus) {
    return !filterStatus || (rowStatus || '').toLowerCase() === filterStatus.toLowerCase();
  }
  function matchesDate(dateStr, from, to) {
    const d = (dateStr || '').slice(0, 10);
    if (from && d < from) return false;
    if (to   && d > to)   return false;
    return true;
  }

  /* ── Private helper ──────────────────────────────────── */
  function _el(id) { return document.getElementById(id); }

  /* ── Public API ──────────────────────────────────────── */
  return {
    init,
    getFilters, validateDates, updateClearBtn,
    setCount, setLoading, showEmpty, showError,
    syncURL, restoreFromURL, cancelPrev,
    wire, clear,
    matchesSearch, matchesStatus, matchesDate,
  };
})();
