'use strict';

document.addEventListener('DOMContentLoaded', async () => {
  const theme = localStorage.getItem('fnas_theme') || 'dark';
  document.documentElement.setAttribute('data-theme', theme);

  // Redirect if already initialized
  try {
    const res = await fetch('/api/setup/status');
    const d   = await res.json();
    if (d.initialized) { location.href = '/dashboard/'; return; }
    const select = document.getElementById('f-edition');
    const option = [...select.options].find(o => o.value && o.value === d.detected_edition);
    if (option) {
      select.value = option.value;
      document.getElementById('edition-detected').textContent = `Detected: ${option.textContent}.`;
    }
  } catch { /* proceed */ }

  let step = 1;

  const steps = {
    1: document.getElementById('step-1'),
    2: document.getElementById('step-2'),
    3: document.getElementById('step-3'),
  };

  showStep(1);

  document.getElementById('btn-next-1').addEventListener('click', () => {
    const host = document.getElementById('f-hostname').value.trim();
    const tz   = document.getElementById('f-timezone').value.trim();
    if (!host) { fieldErr('f-hostname', 'Hostname is required.'); return; }
    if (!isValidHostname(host)) { fieldErr('f-hostname', 'Invalid hostname (letters, digits, hyphens only).'); return; }
    clearErr('f-hostname');
    showStep(2);
  });

  document.getElementById('btn-back-2').addEventListener('click', () => showStep(1));
  document.getElementById('btn-next-2').addEventListener('click', () => {
    const edition = document.getElementById('f-edition').value;
    if (!edition) { fieldErr('f-edition', 'Please select an edition.'); return; }
    clearErr('f-edition');
    buildSummary();
    showStep(3);
  });

  document.getElementById('btn-back-3').addEventListener('click', () => showStep(2));
  document.getElementById('btn-initialize').addEventListener('click', initialize);

  function showStep(n) {
    step = n;
    Object.values(steps).forEach(s => s && (s.hidden = true));
    if (steps[n]) steps[n].hidden = false;
    document.querySelectorAll('.wizard-footer').forEach(f => f.hidden = f.id !== `footer-${n}`);

    document.querySelectorAll('.wizard-step').forEach(el => {
      const sn = parseInt(el.dataset.step);
      el.classList.toggle('active', sn === n);
      el.classList.toggle('done', sn < n);
    });
  }

  function buildSummary() {
    const host    = document.getElementById('f-hostname').value.trim();
    const tz      = document.getElementById('f-timezone').value.trim() || 'UTC';
    const select  = document.getElementById('f-edition');
    document.getElementById('summary-hostname').textContent = host;
    document.getElementById('summary-timezone').textContent = tz;
    document.getElementById('summary-edition').textContent  = select.options[select.selectedIndex].textContent;
  }

  async function initialize() {
    const btn     = document.getElementById('btn-initialize');
    const host    = document.getElementById('f-hostname').value.trim();
    const tz      = document.getElementById('f-timezone').value.trim() || 'UTC';
    const edition = document.getElementById('f-edition').value;

    btn.disabled = true;
    btn.textContent = 'Initializing…';
    document.getElementById('init-error').hidden = true;

    try {
      const res = await fetch('/api/setup/initialize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hostname: host, timezone: tz, edition }),
      });

      if (res.ok) {
        document.getElementById('step-3').hidden = true;
        document.getElementById('footer-3').hidden = true;
        document.getElementById('step-done').hidden = false;
        return;
      }

      const d = await res.json();
      document.getElementById('init-error').textContent = d.detail || 'Initialization failed.';
      document.getElementById('init-error').hidden = false;
    } catch (e) {
      document.getElementById('init-error').textContent = `Error: ${e.message}`;
      document.getElementById('init-error').hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Initialize System';
    }
  }

  document.getElementById('btn-go-auth').addEventListener('click', () => {
    location.href = '/auth/';
  });
});

function isValidHostname(h) {
  return /^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$/.test(h);
}

function fieldErr(id, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.borderColor = 'var(--err)';
  let hint = el.parentElement.querySelector('.field-err');
  if (!hint) { hint = document.createElement('span'); hint.className = 'form-error field-err'; el.parentElement.appendChild(hint); }
  hint.textContent = msg;
}

function clearErr(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.borderColor = '';
  el.parentElement.querySelector('.field-err')?.remove();
}
