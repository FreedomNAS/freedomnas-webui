'use strict';

document.addEventListener('DOMContentLoaded', async () => {
  if (!initShared()) return;
  await Promise.all([loadSystemInfo(), loadUpdateStatus(), loadModuleHealth(), loadRollback()]);
});

function formatStamp(stamp) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/.exec(stamp || '');
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` : (stamp || '');
}

async function loadRollback() {
  const banner = document.getElementById('rollback-banner');
  if (!banner) return;
  let d;
  try { d = await apiJSON('GET', '/api/system/boot'); } catch { return; }
  const r = d.rollback;
  if (!r) { banner.hidden = true; return; }
  if (r.failed_system) {
    document.getElementById('rollback-text').textContent =
      `On ${formatStamp(r.date)} the system was updated into ${r.failed_system}, which did not finish starting, ` +
      `so FreedomNAS went back to ${r.restored_system || 'the system from before'}, as it was before the update. ` +
      `Your data pools were not touched. The failed copy is kept until you delete it.`;
    const del = document.getElementById('btn-rollback-destroy');
    del.hidden = false;
    banner.hidden = false;
    del.onclick = async () => {
      if (!await confirm('Delete the failed system',
          `${r.failed_system} will be destroyed. The running system is not touched.`, 'Delete')) return;
      await forgetRollback(true);
    };
    document.getElementById('btn-rollback-dismiss').onclick = () => forgetRollback(false);
    return;
  }
  const restored = r.system_restored
    ? ` The system files were restored from the snapshot taken before the update${r.failed_dataset ? `; the failed system is kept as ${r.failed_dataset}` : ''}.`
    : ' The system files were left as they were.';
  document.getElementById('rollback-text').textContent =
    `On ${formatStamp(r.date)} kernel ${r.failed_kernel} did not boot (${r.reason || 'unknown reason'}), ` +
    `so FreedomNAS went back to kernel ${r.restored_kernel || 'the previous one'}.${restored} ` +
    `That kernel stays out of use until this notice is dismissed.`;
  const destroy = document.getElementById('btn-rollback-destroy');
  destroy.hidden = !r.failed_dataset;
  banner.hidden = false;

  destroy.onclick = async () => {
    if (!await confirm('Delete the failed system',
        `${r.failed_dataset} and its snapshots will be destroyed. The running system is not touched.`, 'Delete')) return;
    await forgetRollback(true);
  };
  document.getElementById('btn-rollback-dismiss').onclick = () => forgetRollback(false);
}

async function forgetRollback(destroyFailed) {
  try {
    const d = await apiJSON('POST', '/api/system/boot/forget', { destroy_failed: destroyFailed });
    toast(d.destroyed ? `${d.destroyed} deleted.` : 'Notice dismissed.', 'ok');
    document.getElementById('rollback-banner').hidden = true;
  } catch (e) {
    toast(`Error: ${e.message}`, 'err');
  }
}

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
