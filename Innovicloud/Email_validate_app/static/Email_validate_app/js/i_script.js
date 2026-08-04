/* ══════════════════════════════════════════════════════
   Innovicloud — i_script.js
   ══════════════════════════════════════════════════════ */

document.addEventListener('DOMContentLoaded', () => {

  /* ── NAV: Add shadow on scroll ─────────────────────── */
  window.addEventListener('scroll', () => {
    var mainNav = document.getElementById('mainNav');
    if (mainNav) mainNav.classList.toggle('scrolled', window.scrollY > 10);
  });

  /* ── MOBILE NAV: Toggle open/close ─────────────────── */
  window.toggleMobileNav = function () {
    document.getElementById('mobileNav').classList.toggle('open');
  };

  /* ── DEMO: Switch between Email / DMARC tabs ─────────── */
  window.switchTab = function (tab, btn) {
    document.querySelectorAll('.demo-pane').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.demo-tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('pane-' + tab).classList.add('active');
    btn.classList.add('active');
  };

  /* ── Restore active tab after Django POST ────────────── */
  if (window._activeTab === 'dmarc') {
    document.querySelectorAll('.demo-pane').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.demo-tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('pane-dmarc').classList.add('active');
    document.querySelector('[onclick*="dmarc"]').classList.add('active');
  }
  if (window._activeTab === 'email') {
    document.querySelectorAll('.demo-pane').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.demo-tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('pane-email').classList.add('active');
    document.querySelector('[onclick*="email"]').classList.add('active');
  }

  /* ── SCROLL ANIMATIONS: Fade-up on enter viewport ───── */
  const fadeObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
        fadeObserver.unobserve(entry.target);
      }
    });
  }, { threshold: 0.12 });

  document.querySelectorAll('.fade-up').forEach(el => fadeObserver.observe(el));

});


/* ── LOADING STATE on form submit ────────────────────── */
const formConfig = {
  'pane-email': 'Verifying email...',
  'pane-dmarc': 'Checking domain...',
};

document.querySelectorAll('.demo-pane form').forEach(form => {
  form.addEventListener('submit', () => {
    const paneId = form.closest('.demo-pane').id;
    const btn = form.querySelector('.demo-verify-btn');
    btn.disabled = true;
    btn.innerHTML = `<span class="btn-spinner"></span> ${formConfig[paneId] || 'Checking...'}`;
  });
});


/* ── SUPPORT FORM: open / close ──────────────────────── */
window.supportform = function () {
  document.getElementById('supportform').style.display = 'flex';
};
window.closesupportform = function () {
  document.getElementById('supportform').style.display = 'none';
};

// Close on backdrop click
var _sfEl = document.getElementById('supportform');
if (_sfEl) {
  _sfEl.addEventListener('click', function (e) {
    if (e.target === this) closesupportform();
  });
}

// Loading state on submit
const contactForm = document.getElementById('Contact_Us');
if (contactForm) {
  contactForm.addEventListener('submit', () => {
    const btn = contactForm.querySelector('button[type="submit"]');
    btn.disabled = true;
    btn.innerHTML = '<span class="btn-spinner"></span> Sending...';
    document.getElementById('Contact_loader_div').style.display = 'flex';
  });
}