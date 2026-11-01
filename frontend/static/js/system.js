'use strict';

document.addEventListener('DOMContentLoaded', async () => {
  if (!initShared()) return;

  const tzSelect = document.getElementById('f-timezone');
  if (tzSelect) fillTimezones(tzSelect, 'UTC');
  await loadSystemInfo();
  loadCertificate();

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
  loadBootEnvironments();

  document.getElementById('btn-reboot')?.addEventListener('click', rebootSystem);
  document.getElementById('btn-restart-webui')?.addEventListener('click', restartWebUI);

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
  trial: 'An update is on trial: it is confirmed once the system has finished booting.',
  'fallback-requested': 'A rollback is in progress.',
  'rolled-back': 'The last update was rolled back.',
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

function renderBootEnvironments(envs) {
  const el = document.getElementById('bootenv-list');
  el.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>System</th><th>Made</th><th>Space</th><th></th><th></th></tr></thead>
    <tbody>${envs.map(e => `<tr>
      <td class="mono">${esc(e.name)}</td>
      <td class="text-muted">${esc(e.created)}</td>
      <td>${esc(e.space)}</td>
      <td>${e.running ? '<span class="badge badge-ok">Running now</span>' : ''}
          ${e.next_boot ? `<span class="badge badge-info">${e.once ? 'Next start only' : 'Starts next'}</span>` : ''}</td>
      <td>${e.next_boot && !e.once ? '' : `<button class="btn btn-secondary btn-sm" data-be-activate="${esc(e.name)}">Start this one</button>`}
          ${e.running || e.next_boot ? '' : `<button class="btn btn-ghost btn-sm" data-be-destroy="${esc(e.name)}">Delete</button>`}</td>
    </tr>`).join('')}</tbody>
  </table></div>`;
  el.querySelectorAll('[data-be-activate]').forEach(b => b.addEventListener('click', () => activateBootEnvironment(b.dataset.beActivate)));
  el.querySelectorAll('[data-be-destroy]').forEach(b => b.addEventListener('click', () => destroyBootEnvironment(b.dataset.beDestroy)));
}

async function loadBootEnvironments() {
  const section = document.getElementById('bootenv-section');
  if (!section) return;
  let d;
  try { d = await apiJSON('GET', '/api/system/boot-environments'); } catch { return; }
  if (!d.supported) return;
  section.hidden = false;
  renderBootEnvironments(d.environments);
}

async function activateBootEnvironment(name) {
  if (!await confirm('Start this system',
    `From the next start the system boots <span class="mono">${esc(name)}</span>: everything outside the data pools goes back to how it was then. You can switch back here the same way.`,
    'Start this one')) return;
  try {
    const d = await apiJSON('POST', `/api/system/boot-environments/${encodeURIComponent(name)}/activate`);
    renderBootEnvironments(d.environments);
    toast(`${name} starts next time.`, 'ok');
    await rebootSystem();
  } catch (e) { toast(`Error: ${e.message}`, 'err'); }
}

async function destroyBootEnvironment(name) {
  if (!await confirm('Delete this system', `Delete the copy <span class="mono">${esc(name)}</span>? It can no longer be started.`, 'Delete')) return;
  try {
    const d = await apiJSON('DELETE', `/api/system/boot-environments/${encodeURIComponent(name)}`);
    renderBootEnvironments(d.environments);
    toast(`${name} deleted.`, 'ok');
  } catch (e) { toast(`Error: ${e.message}`, 'err'); }
}

async function loadCertificate() {
  let d;
  try { d = await apiJSON('GET', '/api/system/certificate'); } catch { return; }
  if (!d.ca_fingerprint) return;
  document.getElementById('cert-ca').textContent = d.ca_fingerprint;
  document.getElementById('cert-fp').textContent = d.fingerprint;
  document.getElementById('cert-names').textContent = [...d.names, ...d.addresses].join(', ');
  document.getElementById('cert-expires').textContent = new Date(d.expires).toLocaleDateString();
  document.getElementById('certificate-section').hidden = false;
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

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
