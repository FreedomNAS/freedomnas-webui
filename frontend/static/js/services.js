'use strict';

document.addEventListener('DOMContentLoaded', () => {
  if (!initShared()) return;
  document.getElementById('f-service-filter').addEventListener('input', renderServices);
  document.getElementById('services-body').addEventListener('click', onServiceAction);
  document.getElementById('ui-level-select')?.addEventListener('change', applyLevel);
  applyLevel();
});

function applyLevel() {
  const assisted = UILevel.current() === 'assisted';
  document.getElementById('services-assisted').hidden = !assisted;
  if (!assisted && _services === null) loadServices();
}

let _services = null;

async function loadServices() {
  _services = [];
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
