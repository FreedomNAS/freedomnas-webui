'use strict';

document.addEventListener('DOMContentLoaded', async () => {
  if (!initShared()) return;

  const tzSelect = document.getElementById('f-timezone');
  if (tzSelect) fillTimezones(tzSelect, 'UTC');
  await loadSystemInfo();

  document.getElementById('form-hostname').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn  = document.getElementById('btn-hostname');
    const host = document.getElementById('f-hostname').value.trim();
    if (!host) { toast('Hostname is required.', 'warn'); return; }
    setLoading(btn, true, 'Saving…');
    try {
      await apiJSON('POST', '/api/system/hostname', { hostname: host });
      toast('Hostname updated.', 'ok');
    } catch (e) {
      toast(`Error: ${e.message}`, 'err');
    } finally {
      setLoading(btn, false);
    }
  });

  document.getElementById('form-timezone')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = document.getElementById('btn-timezone');
    const tz  = document.getElementById('f-timezone').value.trim();
    if (!tz)  { toast('Timezone is required.', 'warn'); return; }
    setLoading(btn, true, 'Saving…');
    try {
      await apiJSON('POST', '/api/system/timezone', { timezone: tz });
      toast(`Timezone set to ${tz}.`, 'ok');
    } catch (e) {
      toast(`Error: ${e.message}`, 'err');
    } finally {
      setLoading(btn, false);
    }
  });

  const ssh = document.getElementById('f-ssh');
  if (ssh) {
    apiJSON('GET', '/api/system/ssh').then(d => { ssh.checked = !!d.enabled; }).catch(() => {});
    ssh.addEventListener('change', async () => {
      const wanted = ssh.checked;
      ssh.disabled = true;
      try {
        await apiJSON('POST', '/api/system/ssh', { enabled: wanted });
        toast(wanted ? 'SSH is on.' : 'SSH is off.', 'ok');
      } catch (e) {
        ssh.checked = !wanted;
        toast(`Error: ${e.message}`, 'err');
      } finally {
        ssh.disabled = false;
      }
    });
  }

  loadAutorollback();

  document.getElementById('btn-reboot')?.addEventListener('click', async () => {
    if (!await confirm('Reboot System', 'The system will reboot. All active sessions will be disconnected.', 'Reboot')) return;
    try {
      await apiJSON('POST', '/api/system/reboot');
      toast('Reboot initiated. This page will be unavailable for a few minutes.', 'warn', 8000);
    } catch (e) {
      toast(`Error: ${e.message}`, 'err');
    }
  });

  document.getElementById('btn-shutdown')?.addEventListener('click', async () => {
    if (!await confirm('Shut Down System', 'The system will power off. You must physically turn it back on.', 'Shut Down')) return;
    try {
      await apiJSON('POST', '/api/system/shutdown');
      toast('Shutdown initiated.', 'warn', 8000);
    } catch (e) {
      toast(`Error: ${e.message}`, 'err');
    }
  });
});

const BOOT_STATES = {
  ok: 'The running kernel booted normally.',
  trial: 'A new kernel is on trial: it is confirmed once the system has finished booting.',
  'fallback-requested': 'A rollback is in progress.',
  'rolled-back': 'The last kernel update was rolled back.',
};

async function loadAutorollback() {
  const section = document.getElementById('autorollback-section');
  const box = document.getElementById('f-autorollback');
  if (!section || !box) return;
  let d;
  try { d = await apiJSON('GET', '/api/system/boot'); } catch { return; }
  if (!d.supported) return;
  section.hidden = false;
  box.checked = !!d.autorollback;
  document.getElementById('autorollback-state').textContent = BOOT_STATES[d.state] || '';
  box.addEventListener('change', async () => {
    const wanted = box.checked;
    box.disabled = true;
    try {
      await apiJSON('POST', '/api/system/boot/autorollback', { enabled: wanted });
      toast(wanted ? 'Automatic rollback is on.' : 'Automatic rollback is off.', 'ok');
    } catch (e) {
      box.checked = !wanted;
      toast(`Error: ${e.message}`, 'err');
    } finally {
      box.disabled = false;
    }
  });
}

async function loadSystemInfo() {
  try {
    const d = await apiJSON('GET', '/api/system/info');
    const h = document.getElementById('f-hostname');
    if (h) h.value = d.hostname || '';
    const t = document.getElementById('f-timezone');
    if (t) fillTimezones(t, d.timezone?.trim() || 'UTC');
    const edEl = document.getElementById('info-edition');
    if (edEl) edEl.textContent = d.edition || '—';
    const upEl = document.getElementById('info-uptime');
    if (upEl) upEl.textContent = d.uptime_since?.trim().replace(/^up\s*/i, '') || '—';
  } catch (e) {
    toast(`Could not load system info: ${e.message}`, 'err');
  }
}

// Every IANA zone the browser knows. A <datalist> would filter by what the
// field already holds, so a field starting at "UTC" never showed the others.
function fillTimezones(select, selected) {
  let zones = [];
  try { zones = Intl.supportedValuesOf('timeZone'); } catch { /* older browser */ }
  if (!zones.includes('UTC')) zones = ['UTC', ...zones];
  if (selected && !zones.includes(selected)) zones = [selected, ...zones];
  select.replaceChildren(...zones.map(z => new Option(z, z, false, z === selected)));
}
