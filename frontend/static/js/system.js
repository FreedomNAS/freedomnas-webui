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

  document.getElementById('f-service-filter')?.addEventListener('input', renderServices);
  document.getElementById('services-body')?.addEventListener('click', onServiceAction);
  document.getElementById('ui-level-select-inline')?.addEventListener('change', () => setTimeout(loadServicesOnce));
  loadServicesOnce();

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

let _services = null;

function loadServicesOnce() {
  if (_services !== null || UILevel.current() === 'assisted') return;
  _services = [];
  loadServices();
}

async function loadServices() {
  try {
    const d = await apiJSON('GET', '/api/services/');
    _services = d.services || [];
  } catch (e) {
    document.getElementById('services-body').innerHTML =
      `<tr><td colspan="4" class="text-muted">Could not list services: ${esc(e.message)}</td></tr>`;
    return;
  }
  renderServices();
}

function renderServices() {
  const body = document.getElementById('services-body');
  if (!body || !_services) return;
  const filter = (document.getElementById('f-service-filter')?.value || '').trim().toLowerCase();
  const rows = _services.filter(s => !filter || s.name.toLowerCase().includes(filter)
                                    || (s.role || '').toLowerCase().includes(filter));
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="4" class="text-muted">No service matches.</td></tr>';
    return;
  }
  body.innerHTML = rows.map(s => {
    const n = esc(s.name);
    return `
    <tr>
      <td><span class="font-mono">${n}</span>${s.role ? ` <span class="badge badge-info">${esc(s.role)}</span>` : ''}</td>
      <td><span class="badge ${s.running ? 'badge-ok' : 'badge-muted'}">${s.running ? 'Running' : 'Stopped'}</span></td>
      <td class="expert-only"><span class="badge ${s.enabled ? 'badge-ok' : 'badge-muted'}">${s.enabled ? 'Enabled' : 'Disabled'}</span></td>
      <td style="white-space:nowrap;text-align:right">
        ${s.running
          ? `<button class="btn btn-secondary btn-sm" data-service="${n}" data-action="restart">Restart</button>
             <button class="btn btn-secondary btn-sm" data-service="${n}" data-action="stop">Stop</button>`
          : `<button class="btn btn-secondary btn-sm" data-service="${n}" data-action="start">Start</button>`}
        <button class="btn btn-secondary btn-sm expert-only" data-service="${n}"
          data-action="${s.enabled ? 'disable' : 'enable'}">${s.enabled ? 'Disable' : 'Enable'}</button>
      </td>
    </tr>`;
  }).join('');
}

async function onServiceAction(e) {
  const btn = e.target.closest('button[data-action]');
  if (!btn) return;
  const name = btn.dataset.service;
  const action = btn.dataset.action;
  const svc = (_services || []).find(s => s.name === name) || {};
  if (action === 'stop' || action === 'disable') {
    const what = action === 'stop' ? `Stop ${name} now?` : `Keep ${name} from starting at boot?`;
    const ssh = svc.role === 'SSH' ? ' SSH logins will not be possible while it is off.' : '';
    if (!await confirm(action === 'stop' ? 'Stop service' : 'Disable service',
                       `${what} Whatever depends on it stops working.${ssh}`,
                       action === 'stop' ? 'Stop' : 'Disable')) return;
  }
  setLoading(btn, true, '…');
  try {
    const updated = await apiJSON('POST', `/api/services/${encodeURIComponent(name)}/${action}`);
    _services = _services.map(s => s.name === name ? updated : s);
    toast(`${name}: ${action} done.`, 'ok');
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  } finally {
    renderServices();
  }
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
