/* WayToInbox — shared Razorpay checkout.
 *
 * Replaces three near-identical copies of openRazorpay() that lived in
 * i_pricing.html, i_subscription.html and i_bulk_email_verify.html. They
 * differed only in six places: the CSRF getter, the alert function, the
 * verify URL, where to redirect afterwards, the modal description, and which
 * extra form fields to post. Those are the options below; everything else is
 * shared.
 *
 * The copies could not simply be moved into a static file because each one
 * hard-coded {% url %} template tags. Those are now passed in by the caller,
 * which is why this file can live in static/ at all.
 *
 * Usage:
 *   WTICheckout.open(order, {
 *     verifyUrl:   '/payment/',            // required
 *     returnUrl:   '/pricing/',            // optional: redirect when finished
 *     description: 'Email Credits Purchase',
 *     fields:      { plan: 'x' },          // extra POST fields (form mode)
 *     json:        false,                  // true -> POST JSON, not FormData
 *     notify:      function (type, msg) {},
 *     successMessage: 'Payment successful!',
 *     onSuccess:   function (data) {},
 *     onError:     function (message, data) {}
 *   });
 *
 * `order` is whatever the server's order endpoint returned: it must carry
 * key_id, order_id, currency and either amount (paise/cents, as Razorpay
 * wants it) or amount_cents.
 */
(function (global) {
  'use strict';

  function csrfToken() {
    var el = document.querySelector('[name=csrfmiddlewaretoken]');
    if (el) { return el.value; }
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  /* Falls back through the two notifiers the app already uses, then to
   * alert(), so a caller that passes nothing still tells the user something. */
  function defaultNotify(type, message) {
    if (global.WTI && typeof global.WTI.toast === 'function') {
      global.WTI.toast(message, type);
    } else if (typeof global.subShowAlert === 'function') {
      global.subShowAlert(type, message);
    } else {
      global.alert(message);
    }
  }

  function go(url, delay) {
    if (!url) { return; }
    global.setTimeout(function () { global.location.href = url; }, delay);
  }

  function open(order, opts) {
    opts = opts || {};

    if (!order || !order.order_id || !order.key_id) {
      defaultNotify('error', 'Could not start checkout. Please try again.');
      return;
    }
    if (!opts.verifyUrl) {
      throw new Error('WTICheckout.open: verifyUrl is required');
    }

    var notify = opts.notify || defaultNotify;
    var amount = (order.amount !== undefined && order.amount !== null)
      ? order.amount
      : order.amount_cents;

    function finish(ok, message, data) {
      notify(ok ? 'success' : 'error', message);
      if (ok && typeof opts.onSuccess === 'function') { opts.onSuccess(data); }
      if (!ok && typeof opts.onError === 'function') { opts.onError(message, data); }
      go(opts.returnUrl, ok ? 1500 : 2000);
    }

    var options = {
      key:         order.key_id,
      amount:      amount,
      currency:    order.currency,
      name:        order.user_name,
      description: opts.description || 'Credits Purchase',
      order_id:    order.order_id,

      handler: async function (response) {
        var res, data;
        try {
          if (opts.json) {
            res = await fetch(opts.verifyUrl, {
              method: 'POST',
              headers: {
                'Content-Type':     'application/json',
                'X-CSRFToken':      csrfToken(),
                'X-Requested-With': 'XMLHttpRequest'
              },
              body: JSON.stringify({
                razorpay_order_id:   order.order_id,
                razorpay_payment_id: response.razorpay_payment_id,
                razorpay_signature:  response.razorpay_signature,
                user_name:           order.user_name || ''
              })
            });
          } else {
            var fd = new FormData();
            fd.append('csrfmiddlewaretoken', csrfToken());
            fd.append('payment_id',         response.razorpay_payment_id);
            fd.append('order_id',           order.order_id);
            fd.append('razorpay_signature', response.razorpay_signature);
            fd.append('amount',             amount);
            fd.append('credits',            order.credit);
            fd.append('user_email',         order.user_email);
            fd.append('user_name',          order.user_name);
            fd.append('currency',           order.currency);
            var extra = opts.fields || {};
            Object.keys(extra).forEach(function (k) { fd.append(k, extra[k]); });

            res = await fetch(opts.verifyUrl, {
              method:  'POST',
              headers: { 'X-Requested-With': 'XMLHttpRequest' },
              body:    fd
            });
          }

          data = await res.json();
        } catch (err) {
          finish(false, 'Something went wrong finalising payment. Please contact support.');
          return;
        }

        if (data && data.status === 'ok') {
          var msg = typeof opts.successMessage === 'function'
            ? opts.successMessage(data)
            : (opts.successMessage || 'Payment successful! Credits have been added to your account.');
          finish(true, msg, data);
        } else {
          finish(false, (data && data.message) || 'Payment finalisation failed.', data);
        }
      },

      modal: {
        ondismiss: function () {
          if (typeof opts.onDismiss === 'function') { opts.onDismiss(); }
          go(opts.returnUrl, 0);
        }
      },

      theme: { color: '#3399cc' }
    };

    var rzp = new global.Razorpay(options);
    rzp.on('payment.failed', function (response) {
      var desc = (response && response.error && response.error.description) || 'Unknown error.';
      notify('error', 'Payment failed: ' + desc);
      if (typeof opts.onError === 'function') { opts.onError(desc, response); }
      go(opts.returnUrl, 2000);
    });
    rzp.open();
  }

  global.WTICheckout = { open: open, csrfToken: csrfToken };
})(window);
