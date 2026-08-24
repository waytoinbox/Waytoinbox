/*
 * wti_timezones.js
 * ----------------
 * Shared schedule-control + timezone-picker helpers.
 *
 * Extracted from i_Create_Campaign.html so the Sales Outreach campaign page can
 * reuse the same behaviour. Every function takes a config object of element ids,
 * defaulting to the ids used by the Email Marketing page.
 */
var WTITZ = (function () {
  'use strict';

  var TZ_LIST = [
    'UTC',
    'Africa/Cairo','Africa/Johannesburg','Africa/Lagos','Africa/Nairobi',
    'America/Anchorage','America/Bogota','America/Chicago','America/Denver',
    'America/Los_Angeles','America/Mexico_City','America/New_York','America/Phoenix',
    'America/Santiago','America/Sao_Paulo','America/Toronto',
    'Asia/Bangkok','Asia/Colombo','Asia/Dhaka','Asia/Dubai','Asia/Hong_Kong',
    'Asia/Jakarta','Asia/Karachi','Asia/Kathmandu','Asia/Kolkata','Asia/Kuala_Lumpur',
    'Asia/Manila','Asia/Riyadh','Asia/Seoul','Asia/Shanghai','Asia/Singapore',
    'Asia/Taipei','Asia/Tashkent','Asia/Tehran','Asia/Tokyo',
    'Atlantic/Azores','Atlantic/Reykjavik',
    'Australia/Adelaide','Australia/Brisbane','Australia/Melbourne','Australia/Perth','Australia/Sydney',
    'Europe/Amsterdam','Europe/Athens','Europe/Berlin','Europe/Brussels',
    'Europe/Bucharest','Europe/Dublin','Europe/Helsinki','Europe/Istanbul',
    'Europe/Kyiv','Europe/Lisbon','Europe/London','Europe/Madrid',
    'Europe/Moscow','Europe/Oslo','Europe/Paris','Europe/Prague',
    'Europe/Rome','Europe/Stockholm','Europe/Vienna','Europe/Warsaw','Europe/Zurich',
    'Pacific/Auckland','Pacific/Fiji','Pacific/Guam','Pacific/Honolulu','Pacific/Midway'
  ];

  var DEFAULTS = {
    date:     'scheduleDate',
    hour:     'scheduleHour',
    minute:   'scheduleMinute',
    ampm:     'scheduleAmPm',
    hidden:   'campaignTz',
    trigger:  'tzTrigger',
    dropdown: 'tzDropdown',
    search:   'tzSearch',
    list:     'tzList',
    label:    'tzLabelText',
    optClass: 'crc-sch-tz-opt'
  };

  function cfg(o) {
    var c = {}, k;
    for (k in DEFAULTS) { if (DEFAULTS.hasOwnProperty(k)) c[k] = DEFAULTS[k]; }
    if (o) { for (k in o) { if (o.hasOwnProperty(k)) c[k] = o[k]; } }
    return c;
  }
  function el(id) { return document.getElementById(id); }

  /* Hours 1-12 and minutes 00-55 in steps of 5 */
  function populateScheduleControls(o) {
    var c = cfg(o), h, m, opt, ms;
    var hrSel = el(c.hour), minSel = el(c.minute);
    if (hrSel) {
      hrSel.innerHTML = '';
      for (h = 1; h <= 12; h++) {
        opt = document.createElement('option');
        opt.value = h; opt.textContent = h;
        hrSel.appendChild(opt);
      }
      hrSel.value = '12';
    }
    if (minSel) {
      minSel.innerHTML = '';
      for (m = 0; m < 60; m += 5) {
        ms = String(m).padStart(2, '0');
        opt = document.createElement('option');
        opt.value = ms; opt.textContent = ms;
        minSel.appendChild(opt);
      }
      minSel.value = '00';
    }
  }

  /* 'HH:MM' 24-hour, read off the AM/PM selects */
  function get24hrTime(o) {
    var c = cfg(o);
    var hour = parseInt(el(c.hour).value, 10);
    var min  = el(c.minute).value;
    var ampm = el(c.ampm).value;
    var h24  = hour;
    if (ampm === 'PM' && hour !== 12) h24 = hour + 12;
    if (ampm === 'AM' && hour === 12) h24 = 0;
    return String(h24).padStart(2, '0') + ':' + min;
  }

  /* Populate the controls from a stored UTC ISO string, rendered in `campTz` */
  function restoreScheduleFromUTC(utcStr, campTz, o) {
    if (!utcStr) return;
    var c  = cfg(o);
    var tz = campTz || (el(c.hidden) && el(c.hidden).value) || 'Asia/Kolkata';
    try {
      var d   = new Date(utcStr);
      var fmt = new Intl.DateTimeFormat('en-CA', {
        timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false
      });
      var pts = {};
      fmt.formatToParts(d).forEach(function (x) { pts[x.type] = x.value; });
      if (el(c.date)) el(c.date).value = pts.year + '-' + pts.month + '-' + pts.day;
      var h24  = parseInt(pts.hour, 10);
      var ampm = h24 >= 12 ? 'PM' : 'AM';
      var h12  = h24 % 12 || 12;
      if (el(c.hour)) el(c.hour).value = h12;
      var minRound = Math.round(parseInt(pts.minute, 10) / 5) * 5;
      if (minRound >= 60) minRound = 55;
      if (el(c.minute)) el(c.minute).value = String(minRound).padStart(2, '0');
      if (el(c.ampm))   el(c.ampm).value   = ampm;
    } catch (e) { /* unsupported tz — leave controls as-is */ }
  }

  /* Searchable timezone dropdown */
  function initTzDropdown(o) {
    var c = cfg(o);
    var trigger = el(c.trigger), dropdown = el(c.dropdown), search = el(c.search),
        list = el(c.list), hidden = el(c.hidden), label = el(c.label);
    if (!trigger || !dropdown || !list || !hidden) return;

    function renderList(q) {
      var f = q ? TZ_LIST.filter(function (tz) { return tz.toLowerCase().indexOf(q) >= 0; }) : TZ_LIST;
      list.innerHTML = '';
      f.forEach(function (tz) {
        var node = document.createElement('div');
        node.className = c.optClass + (hidden.value === tz ? ' tz-selected' : '');
        node.textContent = tz;
        node.addEventListener('mousedown', function (e) {
          e.preventDefault();
          hidden.value = tz;
          if (label) label.textContent = tz;
          dropdown.hidden = true;
          renderList('');
        });
        list.appendChild(node);
      });
    }

    trigger.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = !dropdown.hidden;
      dropdown.hidden = open;
      if (!open && search) { search.value = ''; renderList(''); search.focus(); }
    });
    if (search) {
      search.addEventListener('input', function () { renderList(this.value.trim().toLowerCase()); });
      search.addEventListener('click', function (e) { e.stopPropagation(); });
    }
    document.addEventListener('click', function () { dropdown.hidden = true; });
    renderList('');
  }

  /* Browser timezone if we recognise it, else the house default */
  function detectTz(fallback) {
    try {
      var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (tz && TZ_LIST.indexOf(tz) >= 0) return tz;
    } catch (e) { /* ignore */ }
    return fallback || 'Asia/Kolkata';
  }

  return {
    TZ_LIST: TZ_LIST,
    populateScheduleControls: populateScheduleControls,
    get24hrTime: get24hrTime,
    restoreScheduleFromUTC: restoreScheduleFromUTC,
    initTzDropdown: initTzDropdown,
    detectTz: detectTz
  };
})();
