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

async function loadModuleHealth() {
  const grid = document.getElementById('health-grid');
  if (!grid) return;

  const modules = ['storage', 'network', 'shares', 'users', 'updates'];

  try {
    const d = await apiJSON('GET', '/api/system/info');
    grid.innerHTML = modules.map(name => healthCard(name, 'running')).join('');
  } catch {
    grid.innerHTML = modules.map(name => healthCard(name, 'unknown')).join('');
  }
}

function healthCard(name, state) {
  const dotClass = state === 'running' ? 'ok' : state === 'failed' ? 'err' : 'stopped';
  const label    = state === 'running' ? 'Running' : state === 'failed' ? 'Failed' : 'Unknown';
  return `
    <div class="health-item">
      <div class="health-name">${name}</div>
      <div class="health-state">
        <div class="health-dot ${dotClass}"></div>
        <span class="health-state-text">${label}</span>
      </div>
    </div>`;
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
