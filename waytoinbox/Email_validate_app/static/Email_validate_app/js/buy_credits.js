/* WayToInbox — service-credit purchase card.
 *
 * Seven independent quantity steppers, one server-priced total.
 *
 * The rule this file exists to respect: **the server owns the price.** The
 * client mirror below (pricingConfig) only paints an instant figure while the
 * debounced /subscription/quote/ request is in flight; every authoritative
 * number — the quote, the order amount, the credits granted — comes from the
 * server, and /subscription/order/ re-quotes from scratch regardless of
 * anything this page sends.
 *
 * Stepper pattern follows so_campaign.js::stepFormat: real <button>s, a
 * clamped step, and the display mirrored back after every change.
 */
(function () {
  'use strict';

  var DEBOUNCE_MS = 350;
  var MAX_QTY = 1000000000;   // guards against pasted nonsense, not a price cap
  var MIN_QTY = 250;          // server default; a row's own config.min_qty wins if present

  var cfgEl  = document.getElementById('sc-pricing-config');
  var config = {};
  try {
    config = cfgEl ? JSON.parse(cfgEl.textContent) : {};
  } catch (e) {
    config = {};             // mirror is optional; the server still prices
  }

  var rows       = Array.prototype.slice.call(document.querySelectorAll('.sc-row'));
  var totalEl    = document.getElementById('scTotal');
  var noteEl     = document.getElementById('scTotalNote');
  var breakdownEl = document.getElementById('scBreakdown');
  var ctaEl      = document.getElementById('scGetStarted');
  var ctaHintEl  = document.getElementById('scCtaHint');
  var promoTgl   = document.getElementById('scPromoToggle');
  var promoPanel = document.getElementById('scPromoPanel');
  var promoInput = document.getElementById('scPromoInput');
  var promoApply = document.getElementById('scPromoApply');
  var promoMsg   = document.getElementById('scPromoMsg');

  var modal        = document.getElementById('scModal');
  var modalLines   = document.getElementById('scModalLines');
  var modalTotal   = document.getElementById('scModalTotal');
  var modalConfirm = document.getElementById('scModalConfirm');
  var modalCancel  = document.getElementById('scModalCancel');

  if (!rows.length || !totalEl) { return; }

  var urls = {
    quote:  totalEl.dataset.quoteUrl,
    order:  totalEl.dataset.orderUrl,
    verify: totalEl.dataset.verifyUrl,
    self:   totalEl.dataset.selfUrl
  };

  var quantities = {};     // service key -> integer quantity
  var lastQuote  = null;   // most recent successful server quote
  var quoteSeq   = 0;      // only the newest response may paint
  var debounceId = null;
  var inFlight   = false;

  /* ── helpers ─────────────────────────────────────────── */

  function csrf() {
    if (window.WTICheckout && WTICheckout.csrfToken) { return WTICheckout.csrfToken(); }
    var el = document.querySelector('[name=csrfmiddlewaretoken]');
    return el ? el.value : '';
  }

  function notify(type, message) {
    if (window.WTI && typeof WTI.toast === 'function') { WTI.toast(message, type); }
  }

  function money(cents) {
    return '$' + (cents / 100).toLocaleString(undefined, {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }

  /* Only non-zero services are sent — a zero means "not selected", not
     "grant me zero credits". */
  function buildCart() {
    var cart = {};
    Object.keys(quantities).forEach(function (key) {
      if (quantities[key] > 0) { cart[key] = quantities[key]; }
    });
    return cart;
  }

  function cartIsEmpty() { return Object.keys(buildCart()).length === 0; }

  /* Display-only mirror of the server ladders. Returns null whenever it is
     not completely sure, so the UI falls back to "…" rather than showing a
     number the server might disagree with. */
  function mirrorTotalCents() {
    var cart = buildCart();
    var keys = Object.keys(cart);
    if (!keys.length) { return 0; }

    var total = 0;
    for (var i = 0; i < keys.length; i++) {
      var key = keys[i], qty = cart[key], entry = config[key];
      if (!entry) { return null; }

      if (entry.mode === 'block') {
        var block = entry.block_size || 1;
        total += Math.ceil(qty / block) * entry.block_price_cents;
      } else {
        var tiers = entry.tiers || [], matched = null;
        for (var t = 0; t < tiers.length; t++) {
          var lo = tiers[t].min, hi = tiers[t].max;
          if (qty >= lo && (hi === null || hi === undefined || qty <= hi)) {
            matched = tiers[t];
            break;
          }
        }
        if (!matched) { return null; }   // above the top band: server decides
        total += matched.price_cents;
      }
    }
    return total;
  }

  /* ── rendering ───────────────────────────────────────── */

  function setTotal(cents, loading) {
    totalEl.textContent = money(cents);
    totalEl.classList.toggle('is-loading', !!loading);
  }

  function setNote(text, isError) {
    noteEl.textContent = text || '';
    noteEl.classList.toggle('is-error', !!isError);
  }

  /* Shows the subtotal/discount breakdown the server returned. Hidden
     entirely when there is no discount, so a full-price cart still shows one
     clean number. Built with DOM nodes rather than innerHTML because the
     figures come from a server response. */
  function setBreakdown(quote) {
    if (!breakdownEl) { return; }
    if (!quote || !quote.discount_cents) {
      breakdownEl.hidden = true;
      breakdownEl.textContent = '';
      return;
    }
    breakdownEl.textContent = '';
    var was = document.createElement('s');
    was.textContent = money(quote.subtotal_cents);
    var off = document.createElement('b');
    off.textContent = '−' + money(quote.discount_cents);
    breakdownEl.appendChild(was);
    breakdownEl.appendChild(document.createTextNode('  ·  '));
    breakdownEl.appendChild(off);
    breakdownEl.hidden = false;
  }

  function refreshCta() {
    var empty = cartIsEmpty();
    ctaEl.disabled = empty || inFlight;
    ctaHintEl.textContent = empty
      ? 'Choose a quantity for at least one service to continue.'
      : '';
  }

  /* ── quoting ─────────────────────────────────────────── */

  function scheduleQuote() {
    refreshCta();
    window.clearTimeout(debounceId);

    if (cartIsEmpty()) {
      lastQuote = null;
      setTotal(0, false);
      setNote('');
      setBreakdown(null);
      return;
    }

    // Paint the mirror immediately so the number never sits stale, then let
    // the server confirm it.
    var hasPromo = promoInput && promoInput.value.trim() !== '';
    var guess = hasPromo ? null : mirrorTotalCents();
    if (guess !== null) { setTotal(guess, true); }
    else { totalEl.classList.add('is-loading'); }
    setNote('Updating total…');

    debounceId = window.setTimeout(requestQuote, DEBOUNCE_MS);
  }

  /* Returns a promise resolving to the quote, or null if it could not be
     priced. Callers that need the quote (the Get Started button) await it
     rather than guessing at a timeout. */
  function requestQuote() {
    var seq  = ++quoteSeq;
    var cart = buildCart();
    if (!Object.keys(cart).length) { return Promise.resolve(null); }

    return fetch(urls.quote, {
      method: 'POST',
      headers: {
        'Content-Type':     'application/json',
        'X-CSRFToken':      csrf(),
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: JSON.stringify({ cart: cart, promo_code: promoInput ? promoInput.value.trim() : '' })
    })
      .then(function (res) {
        return res.json().then(function (data) { return { ok: res.ok, data: data }; });
      })
      .then(function (r) {
        if (seq !== quoteSeq) { return null; }     // a newer quote already won

        if (!r.ok || r.data.status !== 'ok') {
          setNote(r.data.message || 'Could not price this selection.', true);
          totalEl.classList.remove('is-loading');
          setBreakdown(null);
          lastQuote = null;
          refreshCta();
          return null;
        }

        lastQuote = r.data;
        setTotal(r.data.total_cents, false);
        setNote('');
        setBreakdown(r.data);

        // Always reassigned, never only-when-present: otherwise clearing the
        // code would leave the previous "applied" message on screen next to a
        // full-price total.
        if (promoMsg) {
          promoMsg.textContent = r.data.promo_message || '';
          promoMsg.className = 'sc-promo-msg' +
            (r.data.promo_message ? (r.data.promo_applied ? ' is-ok' : ' is-error') : '');
        }
        refreshCta();
        return r.data;
      })
      .catch(function () {
        if (seq !== quoteSeq) { return null; }
        setNote('Could not reach the server. Check your connection and try again.', true);
        totalEl.classList.remove('is-loading');
        setBreakdown(null);
        lastQuote = null;
        refreshCta();
        return null;
      });
  }

  /* ── steppers ────────────────────────────────────────── */

  function parseQty(raw) {
    var digits = String(raw).replace(/[^0-9]/g, '');   // strips commas, signs
    if (!digits) { return 0; }
    var n = parseInt(digits, 10);
    if (isNaN(n) || n < 0) { return 0; }
    return Math.min(n, MAX_QTY);
  }

  function paintRow(row, input, qty, focused) {
    // Raw digits while typing so the caret behaves; grouped when idle.
    input.value = focused ? String(qty) : qty.toLocaleString();
    row.classList.toggle('is-active', qty > 0);
    var down = row.querySelector('.sc-down');
    if (down) { down.disabled = qty === 0; }
  }

  function initRow(row) {
    var key    = row.dataset.service;
    var input  = row.querySelector('.sc-qty');
    var up     = row.querySelector('.sc-up');
    var down   = row.querySelector('.sc-down');
    var minQty = (config[key] && config[key].min_qty) || MIN_QTY;

    quantities[key] = 0;
    paintRow(row, input, 0, false);

    // 0 always means "not selected". Any other value must be at least
    // minQty -- values in between snap up rather than being rejected, which
    // covers the common case (typing "10", clicking + once from zero)
    // without a round trip. The server enforces the real floor regardless
    // (services/pricing.py::quote_cart), so this is a convenience, not the
    // authority.
    function clampSelected(qty) {
      if (qty > 0 && qty < minQty) { return minQty; }
      return qty;
    }

    function commit(qty, focused) {
      // While typing, keep the raw value so multi-digit entry (e.g. "300")
      // isn't clobbered mid-keystroke by the floor; clamp once they stop.
      var stored = focused ? qty : clampSelected(qty);
      quantities[key] = stored;
      paintRow(row, input, stored, focused);
      scheduleQuote();
    }

    function step(delta) {
      var cur = quantities[key], next;
      if (delta > 0) {
        next = cur === 0 ? minQty : cur + delta;
      } else if (cur <= minQty) {
        next = 0;   // stepping down from the floor removes the selection
      } else {
        next = Math.max(minQty, cur + delta);
      }
      commit(Math.min(MAX_QTY, next), document.activeElement === input);
    }

    up.addEventListener('click', function () { step(1); });
    down.addEventListener('click', function () { step(-1); });

    input.addEventListener('input', function () {
      commit(parseQty(input.value), true);
    });

    input.addEventListener('focus', function () {
      input.value = String(quantities[key]);
      input.select();
    });

    input.addEventListener('blur', function () {
      var clamped = clampSelected(quantities[key]);
      var changed = clamped !== quantities[key];
      quantities[key] = clamped;
      paintRow(row, input, clamped, false);
      if (changed) { scheduleQuote(); }
    });

    // Arrow keys step by one, matching the buttons. The input is type=text
    // (so commas can be displayed), so this is wired explicitly rather than
    // relying on a number input's native behaviour.
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowUp')   { e.preventDefault(); step(1); }
      if (e.key === 'ArrowDown') { e.preventDefault(); step(-1); }
      if (e.key === 'Enter')     { e.preventDefault(); input.blur(); }
    });
  }

  rows.forEach(initRow);

  /* ── promo ───────────────────────────────────────────── */

  if (promoTgl && promoPanel) {
    promoTgl.addEventListener('click', function () {
      var open = promoPanel.hasAttribute('hidden');
      if (open) {
        promoPanel.removeAttribute('hidden');
        promoTgl.setAttribute('aria-expanded', 'true');
        promoInput.focus();
      } else {
        promoPanel.setAttribute('hidden', '');
        promoTgl.setAttribute('aria-expanded', 'false');
      }
    });
  }

  if (promoApply) {
    // Re-quotes with the code attached. No discount is invented here: the
    // total only moves if the server says it moves.
    promoApply.addEventListener('click', function () {
      promoMsg.textContent = '';
      promoMsg.className = 'sc-promo-msg';
      if (cartIsEmpty()) {
        promoMsg.textContent = 'Add a service before applying a code.';
        promoMsg.className = 'sc-promo-msg is-error';
        return;
      }
      scheduleQuote();
    });
  }

  if (promoInput) {
    promoInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); promoApply.click(); }
    });
  }

  /* ── confirmation modal ──────────────────────────────── */

  function openModal() {
    if (!lastQuote) { return; }

    modalLines.innerHTML = '';
    lastQuote.lines.forEach(function (line) {
      var div = document.createElement('div');
      div.className = 'sc-modal-line';
      // Quantity and service only — no per-line price, by design.
      var name = document.createElement('span');
      name.textContent = line.label;
      var qty = document.createElement('b');
      qty.textContent = Number(line.quantity).toLocaleString();
      div.appendChild(name);
      div.appendChild(qty);
      modalLines.appendChild(div);
    });

    if (lastQuote.discount_cents) {
      var drow = document.createElement('div');
      drow.className = 'sc-modal-line sc-modal-discount';
      var dname = document.createElement('span');
      dname.textContent = 'Promo' + (lastQuote.promo_code ? ' (' + lastQuote.promo_code + ')' : '');
      var dval = document.createElement('b');
      dval.textContent = '−' + money(lastQuote.discount_cents);
      drow.appendChild(dname);
      drow.appendChild(dval);
      modalLines.appendChild(drow);
    }

    modalTotal.innerHTML = '';
    var lbl = document.createElement('span'); lbl.textContent = 'Total';
    var amt = document.createElement('span'); amt.textContent = money(lastQuote.total_cents);
    modalTotal.appendChild(lbl);
    modalTotal.appendChild(amt);

    modal.classList.add('is-open');
    modalConfirm.disabled = false;
    modalConfirm.focus();
  }

  function closeModal() { modal.classList.remove('is-open'); }

  if (ctaEl) {
    ctaEl.addEventListener('click', function () {
      if (cartIsEmpty()) { return; }
      if (lastQuote) { openModal(); return; }

      // Either the user beat the debounce or the last quote failed. Re-quote
      // and wait for the actual answer — never guess at a timeout, and never
      // leave the click doing nothing visible.
      setNote('Calculating your total…');
      window.clearTimeout(debounceId);
      ctaEl.disabled = true;
      requestQuote().then(function (quote) {
        ctaEl.disabled = false;
        refreshCta();
        if (quote) {
          openModal();
        } else if (!noteEl.classList.contains('is-error')) {
          setNote('Could not calculate your total. Please try again.', true);
        }
      });
    });
  }

  if (modalCancel) { modalCancel.addEventListener('click', closeModal); }
  if (modal) {
    modal.addEventListener('click', function (e) { if (e.target === modal) { closeModal(); } });
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal && modal.classList.contains('is-open')) { closeModal(); }
  });

  /* ── checkout ────────────────────────────────────────── */

  if (modalConfirm) {
    modalConfirm.addEventListener('click', function () {
      modalConfirm.disabled = true;
      inFlight = true;
      refreshCta();

      fetch(urls.order, {
        method: 'POST',
        headers: {
          'Content-Type':     'application/json',
          'X-CSRFToken':      csrf(),
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify({
          cart: buildCart(),
          promo_code: promoInput ? promoInput.value.trim() : ''
        })
      })
        .then(function (res) {
          return res.json().then(function (data) { return { ok: res.ok, data: data }; });
        })
        .then(function (r) {
          inFlight = false;
          refreshCta();

          if (!r.ok || r.data.status !== 'ok') {
            closeModal();
            notify('error', r.data.message || 'Could not start checkout.');
            setNote(r.data.message || 'Could not start checkout.', true);
            return;
          }

          closeModal();

          // The shared Phase 3 implementation. This page supplies only its
          // own URLs and copy — openRazorpay is not reimplemented here.
          WTICheckout.open(r.data, {
            verifyUrl:   urls.verify,
            returnUrl:   urls.self,
            json:        true,
            description: 'WayToInbox Credits',
            notify:      notify,
            successMessage: 'Payment successful! Your credits have been added.'
          });
        })
        .catch(function () {
          inFlight = false;
          modalConfirm.disabled = false;
          refreshCta();
          notify('error', 'Could not reach the server. Please try again.');
        });
    });
  }

  /* ── initial paint ───────────────────────────────────── */
  setTotal(0, false);
  refreshCta();
})();
