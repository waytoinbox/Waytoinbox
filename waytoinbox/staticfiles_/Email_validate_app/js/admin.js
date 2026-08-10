/* ── Admin Console JS ── */
(function (global) {
  'use strict';

  var Admin = global.Admin || {};

  // ── AdminToggle ────────────────────────────────────────────────────────────
  // Handles POST toggle actions (activate/deactivate, enable/disable).
  // Usage: <button data-toggle-url="/wti-admin/users/5/toggle/" data-toggle-target="#row-5">

  Admin.Toggle = {
    init: function () {
      document.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-toggle-url]');
        if (!btn) return;
        e.preventDefault();
        btn.disabled = true;
        WTI.apiFetch(btn.dataset.toggleUrl, { method: 'POST' }).then(function (res) {
          if (res.ok) {
            if (btn.dataset.toggleReload) {
              window.location.reload();
            } else if (res.data && res.data.redirect) {
              window.location.href = res.data.redirect;
            } else {
              WTI.toast(res.data && res.data.message || 'Updated.', 'success');
              // Update badge if present
              var target = btn.dataset.toggleTarget && document.querySelector(btn.dataset.toggleTarget);
              if (target && res.data && res.data.badge_html) {
                target.innerHTML = res.data.badge_html;
              }
            }
          }
          btn.disabled = false;
        });
      });
    },
  };

  // ── AdminModal ─────────────────────────────────────────────────────────────
  // Reusable confirm modal.
  // Usage: Admin.Modal.confirm('Delete user?', 'This cannot be undone.', function() { ... })

  Admin.Modal = {
    _backdrop: null,
    _okBtn: null,
    _cancelBtn: null,

    _ensure: function () {
      if (!this._backdrop) {
        this._backdrop  = document.getElementById('adminConfirmModal');
        this._okBtn     = document.getElementById('adminConfirmOk');
        this._cancelBtn = document.getElementById('adminConfirmCancel');
        var self = this;
        this._cancelBtn.addEventListener('click', function () { self.close(); });
        this._backdrop.addEventListener('click', function (e) {
          if (e.target === self._backdrop) self.close();
        });
      }
    },

    confirm: function (title, body, onOk, okLabel, danger) {
      this._ensure();
      document.getElementById('adminConfirmTitle').textContent = title || 'Confirm';
      document.getElementById('adminConfirmBody').textContent  = body  || 'Are you sure?';
      this._okBtn.textContent = okLabel || 'Confirm';
      this._okBtn.className   = 'btn-admin ' + (danger !== false ? 'btn-admin-danger' : 'btn-admin-primary');
      this._backdrop.classList.add('open');
      var self = this;
      var handler = function () {
        self._okBtn.removeEventListener('click', handler);
        self.close();
        if (onOk) onOk();
      };
      this._okBtn.addEventListener('click', handler);
    },

    close: function () {
      this._ensure();
      this._backdrop.classList.remove('open');
    },
  };

  // ── AdminChart ─────────────────────────────────────────────────────────────
  // Thin Chart.js wrapper with consistent Waytoinbox styling.

  Admin.Chart = {
    defaults: {
      line: {
        tension: 0.4,
        fill: false,
        borderWidth: 2,
        pointRadius: 3,
        pointHoverRadius: 5,
      },
      bar: {
        borderRadius: 4,
        borderSkipped: 'bottom',
      },
    },

    colors: ['#0099CC', '#00C6A2', '#FF6B35', '#7c3aed', '#2563eb', '#eab308'],

    create: function (canvasId, type, labels, datasets, options) {
      var ctx = document.getElementById(canvasId);
      if (!ctx) return null;
      var self = this;
      var defaults = self.defaults[type] || {};

      datasets = datasets.map(function (ds, i) {
        return Object.assign({
          borderColor: self.colors[i % self.colors.length],
          backgroundColor: type === 'bar'
            ? self.colors[i % self.colors.length] + '99'
            : self.colors[i % self.colors.length] + '22',
        }, defaults, ds);
      });

      return new Chart(ctx, {
        type: type,
        data: { labels: labels, datasets: datasets },
        options: Object.assign({
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { labels: { font: { family: 'DM Sans', size: 12 }, color: '#3A506B' } },
            tooltip: { bodyFont: { family: 'DM Sans' }, titleFont: { family: 'Sora' } },
          },
          scales: type !== 'pie' && type !== 'doughnut' ? {
            x: { grid: { color: 'rgba(216,228,239,.5)' }, ticks: { color: '#7B93AD', font: { family: 'DM Sans', size: 11 } } },
            y: { grid: { color: 'rgba(216,228,239,.5)' }, ticks: { color: '#7B93AD', font: { family: 'DM Sans', size: 11 } } },
          } : {},
        }, options || {}),
      });
    },
  };

  // ── AdminLogs ──────────────────────────────────────────────────────────────
  // Auto-refresh log viewer.

  Admin.Logs = {
    _timer: null,

    init: function (opts) {
      opts = opts || {};
      var url       = opts.url       || '/wti-admin/system/logs/';
      var container = opts.container || '#logOutput';
      var fileInput = opts.fileInput || '#logFile';
      var linesInput= opts.linesInput|| '#logLines';
      var levelInput= opts.levelInput|| '#logLevel';
      var interval  = opts.interval  || 0;

      function load() {
        var file  = (document.querySelector(fileInput)  || {}).value || 'errors';
        var lines = (document.querySelector(linesInput) || {}).value || 200;
        var level = (document.querySelector(levelInput) || {}).value || '';

        WTI.apiFetch(url + '?file=' + file + '&lines=' + lines + '&level=' + level, { silent: true })
          .then(function (res) {
            if (!res.ok) return;
            var el = document.querySelector(container);
            if (!el) return;
            var html = (res.data.lines || []).map(function (line) {
              var cls = '';
              if (line.indexOf(' ERROR ') !== -1 || line.indexOf('ERROR') === 0)    cls = 'log-line-ERROR';
              else if (line.indexOf(' WARNING ') !== -1)  cls = 'log-line-WARNING';
              else if (line.indexOf(' INFO ') !== -1)     cls = 'log-line-INFO';
              else if (line.indexOf(' DEBUG ') !== -1)    cls = 'log-line-DEBUG';
              else if (line.indexOf('CRITICAL') !== -1)   cls = 'log-line-CRITICAL';
              var escaped = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
              return cls ? '<span class="' + cls + '">' + escaped + '</span>' : escaped;
            }).join('\n');
            el.innerHTML = html;
            el.scrollTop = el.scrollHeight;
          });
      }

      load();
      if (interval > 0) {
        if (this._timer) clearInterval(this._timer);
        this._timer = setInterval(load, interval);
      }

      // Wire up controls
      ['change', 'input'].forEach(function (ev) {
        [fileInput, linesInput, levelInput].forEach(function (sel) {
          var el = document.querySelector(sel);
          if (el) el.addEventListener(ev, load);
        });
      });

      var refreshBtn = document.querySelector('[data-log-refresh]');
      if (refreshBtn) refreshBtn.addEventListener('click', load);

      return { load: load, stop: function () { clearInterval(Admin.Logs._timer); } };
    },
  };

  // ── AdminTable ─────────────────────────────────────────────────────────────
  // Client-side sortable table.

  Admin.Table = {
    init: function (tableId) {
      var table = document.getElementById(tableId);
      if (!table) return;
      table.querySelectorAll('th[data-sort]').forEach(function (th) {
        th.addEventListener('click', function () {
          var col  = th.dataset.sort;
          var asc  = th.classList.contains('sorted-asc');
          var tbody = table.querySelector('tbody');
          var rows  = Array.from(tbody.querySelectorAll('tr'));

          rows.sort(function (a, b) {
            var aVal = (a.querySelector('[data-col="' + col + '"]') || {}).textContent || '';
            var bVal = (b.querySelector('[data-col="' + col + '"]') || {}).textContent || '';
            return asc
              ? bVal.localeCompare(aVal, undefined, { numeric: true })
              : aVal.localeCompare(bVal, undefined, { numeric: true });
          });

          rows.forEach(function (r) { tbody.appendChild(r); });
          table.querySelectorAll('th').forEach(function (h) {
            h.classList.remove('sorted', 'sorted-asc', 'sorted-desc');
          });
          th.classList.add('sorted', asc ? 'sorted-desc' : 'sorted-asc');
        });
      });
    },
  };

  // ── Init ───────────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    Admin.Toggle.init();
  });

  global.Admin = Admin;
}(window));
