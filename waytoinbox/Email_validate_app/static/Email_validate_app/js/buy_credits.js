/* WayToInbox — service-credit purchase card.
 *
 * Seven independent quantity steppers, one server-priced total.
 *
 * The rule this file exists to respect: **the server owns the price.** There
 * is no client-side price estimate at all — pricing is per-credit and some
 * rates are sub-cent, so only the server's exact Decimal math
 * (/subscription/quote/) may compute a total; this file just shows a loading
 * state until the debounced response arrives. pricingConfig (window.config
 * below) carries only label/unit/min_qty for rendering the rows, never a
 * price. /subscription/order/ re-quotes from scratch regardless of anything
 * this page sends.
 *
 * Stepper pattern follows so_campaign.js::stepFormat: real <button>s, a
 * clamped step, and the display mirrored back after every change.
 */
(function () {
  'use strict';

  var DEBOUNCE_MS = 350;
  var MAX_QTY = 1000000000;   // guards against pasted nonsense, not a price cap
  // Minimums differ per service (e.g. 1,000 for bulk services, 1 for
  // per-unit ones) and come from each row's own config.min_qty. This is only
  // the defensive fallback for the (should-never-happen) case where the
  // config JSON failed to load for a row — the server re-validates the real
  // per-service minimum regardless (services/pricing.py::quote_cart).
  var MIN_QTY_FALLBACK = 1;

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

  /* Any nonempty subset of the 7 services may be checked out — a service
     left at 0 is simply not selected, not incomplete (cartIsEmpty() below
     is what catches "nothing selected at all"). A service the user HAS
     entered a quantity for must still clear its own existing minimum.
     Returns one entry per selected-but-under-minimum service: { label,
     needsMore } where needsMore is the target minimum. Used both to gate
     "Get Started" and to name exactly what's missing. */
  function incompleteServices() {
    return rows.reduce(function (acc, row) {
      var key    = row.dataset.service;
      var minQty = (config[key] && config[key].min_qty) || MIN_QTY_FALLBACK;
      var qty    = quantities[key] || 0;
      if (qty === 0 || qty >= minQty) { return acc; }
      var label = (config[key] && config[key].label) || key;
      acc.push({ label: label, needsMore: minQty });
      return acc;
    }, []);
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
    var incomplete = incompleteServices();
    var empty = cartIsEmpty();
    ctaEl.disabled = empty || incomplete.length > 0 || inFlight;
    if (empty) {
      ctaHintEl.textContent = 'Select credits for at least one service to continue.';
      return;
    }
    if (incomplete.length === 0) {
      ctaHintEl.textContent = '';
      return;
    }
    var names = incomplete.map(function (s) {
      return s.label + ' (increase to ' + s.needsMore.toLocaleString() + ')';
    });
    ctaHintEl.textContent = 'Increase these to their minimum to continue — ' + names.join(', ') + '.';
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

    // No safe client-side estimate: pricing is per-credit and some rates are
    // sub-cent, so only the server's exact Decimal math (subscription_quote)
    // may compute a total. Keep whatever total was last shown, dimmed, until
    // the debounced response replaces it.
    totalEl.classList.add('is-loading');
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
    var minQty = (config[key] && config[key].min_qty) || MIN_QTY_FALLBACK;

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

  /* ── checkout ────────────────────────────────────────── */

  /* Creates the order server-side (re-validating everything fresh from the
     live cart — at least one service selected, and each selected service
     meets its own minimum, which the stepper only clamps client-side as a
     convenience), then hands off to the "Confirm your purchase" / "Order
     Summary" popup: openServiceConfirm() / closeServiceConfirm() /
     proceedToServicePay() are defined once in this page's own inline
     script, styled consistently with (but a separate popup from)
     Pay-As-You-Go's own Order Summary modal. A rejected order (e.g. a
     selected service still below its minimum, or nothing selected at all)
     never reaches that step — the server's exact message is shown inline
     instead, and no payment can start. */
  function createOrder() {
    inFlight = true;
    ctaEl.disabled = true;
    refreshCta();

    return fetch(urls.order, {
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
          // subscription_order() returns this exact string for an
          // anonymous request (views/credits.py) -- swapped for a message
          // that actually tells the visitor what to do next, rather than
          // a generic "Not authenticated." that reads like an error on
          // their end.
          var message = r.data.message || 'Could not start checkout.';
          if (message === 'Not authenticated.') {
            message = 'Please sign up or log in to purchase credits.';
          }
          notify('error', message);
          setNote(message, true);
          return;
        }

        window.openServiceConfirm(r.data);
      })
      .catch(function () {
        inFlight = false;
        refreshCta();
        notify('error', 'Could not reach the server. Please try again.');
      });
  }

  if (ctaEl) {
    ctaEl.addEventListener('click', function () {
      if (cartIsEmpty() || incompleteServices().length > 0 || inFlight) { refreshCta(); return; }
      if (lastQuote) { createOrder(); return; }

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
          createOrder();
        } else if (!noteEl.classList.contains('is-error')) {
          setNote('Could not calculate your total. Please try again.', true);
        }
      });
    });
  }

  /* ── initial paint ───────────────────────────────────── */
  setTotal(0, false);
  refreshCta();
})();
