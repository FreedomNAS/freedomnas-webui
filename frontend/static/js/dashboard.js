'use strict';

document.addEventListener('DOMContentLoaded', async () => {
  if (!initShared()) return;
  await Promise.all([loadSystemInfo(), loadUpdateStatus(), loadModuleHealth()]);
});

async function loadSystemInfo() {
  try {
    const d = await apiJSON('GET', '/api/system/info');
    setText('stat-hostname', d.hostname || '—');
    setText('stat-edition',  d.edition  || '—');
    setText('stat-uptime',   formatUptime(d.uptime_since));
    setText('stat-timezone', d.timezone || '—');
  } catch (e) {
    toast(`Could not load system info: ${e.message}`, 'err');
  }
}

async function loadUpdateStatus() {
  try {
    const d = await apiJSON('GET', '/api/updates/status');
    const count = d.pending_updates || 0;
    const el = document.getElementById('stat-updates');
    if (el) {
      el.textContent = count === 0 ? 'Up to date' : `${count} pending`;
      el.className   = 'stat-value ' + (count === 0 ? 'text-ok' : 'text-warn');
    }
  } catch { /* ignore */ }
}

const MODULE_PAGES = {
  storage: '/storage/', network: '/network/', shares: '/shares/',
  users: '/users/', updates: '/updates/',
};

async function loadModuleHealth() {
  const grid = document.getElementById('health-grid');
  if (!grid) return;

  try {
    const d = await apiJSON('GET', '/api/system/health');
    grid.innerHTML = (d.modules || []).map(healthCard).join('');
  } catch (e) {
    grid.innerHTML = `<div class="text-sm text-muted">Module health unavailable: ${esc(e.message)}</div>`;
  }
}

function healthCard(m) {
  const dotClass = m.state === 'healthy' ? 'ok' : m.state === 'unhealthy' ? 'err' : 'stopped';
  const label    = m.state === 'healthy' ? 'Healthy' : m.state === 'unhealthy' ? 'Problem' : 'Error';
  const page     = MODULE_PAGES[m.id];
  const body = `
      <div class="health-name">${esc(m.name)}</div>
      <div class="health-state">
        <div class="health-dot ${dotClass}"></div>
        <span class="health-state-text">${label}</span>
      </div>
      ${m.detail ? `<div class="text-sm text-muted">${esc(m.detail)}</div>` : ''}`;
  return page
    ? `<a class="health-item" href="${page}">${body}</a>`
    : `<div class="health-item">${body}</div>`;
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function formatUptime(raw) {
  if (!raw) return '—';
  const trimmed = raw.trim();
  if (!trimmed) return '—';
  const lines = trimmed.split('\n');
  return lines[0].replace(/^up\s*/i, '').trim() || trimmed;
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
