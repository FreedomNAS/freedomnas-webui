'use strict';

let _activeTab = 'pools';

document.addEventListener('DOMContentLoaded', async () => {
  if (!initShared()) return;

  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  document.getElementById('btn-create-snap')?.addEventListener('click', () => openModal('modal-snapshot'));
  bindModalClose('modal-snapshot');

  document.getElementById('form-snapshot').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn     = document.getElementById('btn-snap-submit');
    const dataset = document.getElementById('f-snap-dataset').value.trim();
    const name    = document.getElementById('f-snap-name').value.trim();
    if (!dataset || !name) { toast('Dataset and snapshot name are required.', 'warn'); return; }
    setLoading(btn, true, 'Creating…');
    try {
      await apiJSON('POST', '/api/storage/snapshots', { dataset, snap_name: name });
      toast('Snapshot created.', 'ok');
      closeModal('modal-snapshot');
      document.getElementById('form-snapshot').reset();
      if (_activeTab === 'snapshots') loadSnapshots();
    } catch (e) {
      toast(`Error: ${e.message}`, 'err');
    } finally {
      setLoading(btn, false);
    }
  });

  switchTab('pools');
});

function switchTab(tab) {
  _activeTab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `panel-${tab}`));

  const createBtn = document.getElementById('btn-create-snap');
  if (createBtn) createBtn.hidden = tab !== 'snapshots';

  if (tab === 'pools')     loadPools();
  if (tab === 'datasets')  loadDatasets();
  if (tab === 'snapshots') loadSnapshots();
}

async function loadPools() {
  const el = document.getElementById('panel-pools');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    const d = await apiJSON('GET', '/api/storage/pools');
    if (!d.pools?.length) { el.innerHTML = emptyState('No ZFS pools found.'); return; }
    el.innerHTML = d.pools.map(renderPool).join('');
    // Pre-fill snapshot dataset datalist
    const dl = document.getElementById('ds-list');
    if (dl) dl.innerHTML = d.pools.map(p => `<option value="${p.name}">`).join('');
  } catch (e) {
    el.innerHTML = errorState(e.message);
  }
}

async function loadDatasets() {
  const el = document.getElementById('panel-datasets');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    const d = await apiJSON('GET', '/api/storage/datasets');
    if (!d.datasets?.length) { el.innerHTML = emptyState('No datasets found.'); return; }
    el.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Dataset</th><th>Used</th><th>Available</th><th>Referenced</th><th>Mount</th>
          </tr></thead>
          <tbody>
            ${d.datasets.map(r => `
              <tr>
                <td class="mono">${esc(r.name)}</td>
                <td>${esc(r.used)}</td>
                <td>${esc(r.avail)}</td>
                <td>${esc(r.refer)}</td>
                <td class="mono text-muted">${esc(r.mountpoint)}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  } catch (e) {
    el.innerHTML = errorState(e.message);
  }
}

async function loadSnapshots() {
  const el = document.getElementById('panel-snapshots');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    const d = await apiJSON('GET', '/api/storage/snapshots');
    if (!d.snapshots?.length) { el.innerHTML = emptyState('No snapshots found.'); return; }
    el.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Snapshot</th><th>Created</th><th>Used</th>
          </tr></thead>
          <tbody>
            ${d.snapshots.map(r => `
              <tr>
                <td class="mono">${esc(r.name)}</td>
                <td class="muted">${esc(r.creation)}</td>
                <td>${esc(r.used)}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  } catch (e) {
    el.innerHTML = errorState(e.message);
  }
}

function renderPool(p) {
  const status = (p.status || p.health || 'ONLINE').toUpperCase();
  const badgeClass = status === 'ONLINE' ? 'ok' : status === 'DEGRADED' ? 'warn' : 'err';
  const usedPct = p.used_pct ?? 0;
  const barClass = usedPct > 85 ? 'err' : usedPct > 70 ? 'warn' : 'ok';
  return `
    <div class="pool-card mb-4">
      <div class="pool-header">
        <div>
          <div class="pool-name">${esc(p.name)}</div>
          <div class="pool-detail">${esc(p.type || p.vdev_type || '')}</div>
        </div>
        <span class="badge badge-${badgeClass}">${status}</span>
      </div>
      ${usedPct > 0 ? `
        <div class="progress-wrap" title="${usedPct}% used">
          <div class="progress-bar ${barClass}" style="width:${usedPct}%"></div>
        </div>` : ''}
      <div class="pool-stats">
        <div class="pool-stat-item"><span class="pool-stat-label">Size</span><span class="pool-stat-value">${esc(p.size || '—')}</span></div>
        <div class="pool-stat-item"><span class="pool-stat-label">Used</span><span class="pool-stat-value">${esc(p.used || '—')}</span></div>
        <div class="pool-stat-item"><span class="pool-stat-label">Free</span><span class="pool-stat-value">${esc(p.free || '—')}</span></div>
      </div>
    </div>`;
}

function emptyState(msg) {
  return `<div class="empty-state">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>
    <h3>${msg}</h3>
  </div>`;
}

function errorState(msg) {
  return `<div class="alert alert-err"><span>${esc(msg)}</span></div>`;
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
