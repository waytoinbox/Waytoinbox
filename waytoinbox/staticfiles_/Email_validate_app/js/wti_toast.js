/* WTI — unified toast + fetch helpers */
(function (global) {
  'use strict';

  var WTI = global.WTI || {};

  // ── Internal helpers ────────────────────────────────────────────────────────

  function _getContainer() {
    var el = document.getElementById('wti-toast-container');
    if (!el) {
      el = document.createElement('div');
      el.id = 'wti-toast-container';
      el.setAttribute('aria-live', 'polite');
      el.setAttribute('aria-atomic', 'false');
      el.style.cssText = [
        'position:fixed', 'bottom:1.5rem', 'right:1.5rem',
        'z-index:9999', 'display:flex', 'flex-direction:column',
        'gap:.5rem', 'max-width:360px', 'pointer-events:none',
      ].join(';');
      document.body.appendChild(el);
    }
    return el;
  }

  var _colours = {
    success: '#16a34a',
    error:   '#dc2626',
    warning: '#d97706',
    info:    '#2563eb',
  };

  // ── Public API ───────────────────────────────────────────────────────────────

  /**
   * Show a toast notification.
   * @param {string} message
   * @param {'success'|'error'|'warning'|'info'} [type='info']
   * @param {number} [duration=5000]  ms before auto-dismiss; 0 = sticky
   */
  WTI.toast = function (message, type, duration) {
    type     = type     || 'info';
    duration = (duration === undefined) ? 5000 : duration;

    var bg    = _colours[type] || _colours.info;
    var toast = document.createElement('div');

    toast.setAttribute('role', 'alert');
    toast.style.cssText = [
      'background:' + bg,
      'color:#fff',
      'padding:.75rem 1rem',
      'border-radius:8px',
      'font-size:.875rem',
      'line-height:1.4',
      'box-shadow:0 4px 12px rgba(0,0,0,.15)',
      'pointer-events:auto',
      'cursor:pointer',
      'opacity:1',
      'transition:opacity .3s',
      'word-break:break-word',
    ].join(';');
    toast.textContent = message;

    toast.addEventListener('click', function () { _dismiss(toast); });

    _getContainer().appendChild(toast);

    if (duration > 0) {
      setTimeout(function () { _dismiss(toast); }, duration);
    }
  };

  function _dismiss(el) {
    el.style.opacity = '0';
    setTimeout(function () {
      if (el.parentNode) { el.parentNode.removeChild(el); }
    }, 300);
  }

  // ── Fetch helpers ─────────────────────────────────────────────────────────

  /**
   * Handle a failed fetch (network error or non-ok Response).
   * Shows a toast and logs to console in non-production.
   */
  WTI.handleFetchError = function (err, fallbackMsg) {
    fallbackMsg = fallbackMsg || 'An error occurred. Please try again.';
    var msg = fallbackMsg;

    if (err && err.message) {
      msg = err.message !== 'Failed to fetch' ? err.message : fallbackMsg;
    }

    WTI.toast(msg, 'error');

    if (typeof console !== 'undefined') {
      console.error('[WTI] fetch error:', err);
    }
  };

  /**
   * CSRF-aware fetch wrapper.
   * Returns a Promise resolving to {ok, data, error}.
   * On HTTP error, calls WTI.handleFetchError unless options.silent is true.
   */
  WTI.apiFetch = function (url, options) {
    options = options || {};

    var headers = Object.assign(
      { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      options.headers || {}
    );

    var csrfToken = _getCsrf();
    if (csrfToken) { headers['X-CSRFToken'] = csrfToken; }

    var loadingEl = options.loadingEl || null;
    var silent    = options.silent    || false;

    var fetchOpts = Object.assign({}, options, {
      headers:     headers,
      credentials: options.credentials || 'same-origin',
    });
    delete fetchOpts.silent;
    delete fetchOpts.loadingEl;

    if (loadingEl) { loadingEl.disabled = true; }

    return fetch(url, fetchOpts)
      .then(function (res) {
        return res.json().then(function (data) {
          if (!res.ok) {
            var errMsg = (data && data.message) ? data.message : 'Request failed (' + res.status + ')';
            if (res.status === 401 && data && data.code === 'unauthorized') {
              WTI.toast(errMsg, 'warning', 3000);
              setTimeout(function () {
                window.location.href = (data.redirect || '/login/') +
                  '?next=' + encodeURIComponent(window.location.pathname);
              }, 1200);
              return { ok: false, data: data, error: errMsg };
            }
            if (!silent) { WTI.toast(errMsg, 'error'); }
            return { ok: false, data: data, error: errMsg };
          }
          return { ok: true, data: data, error: null };
        });
      })
      .catch(function (err) {
        if (!silent) { WTI.handleFetchError(err); }
        return { ok: false, data: null, error: err };
      })
      .then(function (result) {
        if (loadingEl) { loadingEl.disabled = false; }
        return result;
      });
  };

  function _getCsrf() {
    var cookieMatch = document.cookie.match(/csrftoken=([^;]+)/);
    if (cookieMatch) { return cookieMatch[1]; }
    var metaEl = document.querySelector('meta[name="csrf-token"]');
    return metaEl ? metaEl.getAttribute('content') : null;
  }

  // ── Django messages bridge ────────────────────────────────────────────────

  /**
   * Convert Django messages rendered as .wti-server-message elements to JS toasts.
   * Call on DOMContentLoaded (done automatically below).
   */
  WTI._initDjangoMessages = function () {
    var els = document.querySelectorAll('.wti-server-message');
    els.forEach(function (el) {
      var msg  = el.getAttribute('data-message') || el.textContent.trim();
      var type = el.getAttribute('data-type') || 'info';
      // Django message tags: success, error, warning, info, debug
      if (type === 'debug') { type = 'info'; }
      WTI.toast(msg, type);
      el.parentNode && el.parentNode.removeChild(el);
    });
  };

  document.addEventListener('DOMContentLoaded', WTI._initDjangoMessages);

  global.WTI = WTI;
}(window));
