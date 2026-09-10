'use strict';

let _activeTab = 'pools';

document.addEventListener('DOMContentLoaded', async () => {
  if (!initShared()) return;

  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  document.getElementById('btn-create-snap')?.addEventListener('click', () => openModal('modal-snapshot'));
  bindModalClose('modal-snapshot');

  bindModalClose('modal-properties');
  document.getElementById('form-properties').addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const changed = {};
    form.querySelectorAll('[data-prop]').forEach(el => {
      if (el.value !== el.dataset.was) changed[el.dataset.prop] = el.value;
    });
    if (!Object.keys(changed).length) { closeModal('modal-properties'); return; }

    const btn = document.getElementById('btn-prop-submit');
    btn.disabled = true;
    try {
      const res = await apiJSON(
        'PUT', `/api/storage/datasets/${form.dataset.dataset}/properties`, changed);
      if (res.failed?.length) {
        toast(`Applied ${res.applied.length}, failed ${res.failed.length}: ` +
              res.failed.map(f => `${f.property}`).join(', '), 'err');
      } else {
        toast(`Applied: ${res.applied.join(', ')}`, 'ok');
        closeModal('modal-properties');
      }
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  });

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
    el.querySelectorAll('[data-attach]').forEach(b =>
      b.addEventListener('click', () => attachDevice(b.dataset.attach)));
    el.querySelectorAll('[data-detach]').forEach(b =>
      b.addEventListener('click', () => detachDevice(b.dataset.pool, b.dataset.detach)));
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
            <th class="advanced-only"></th>
          </tr></thead>
          <tbody>
            ${d.datasets.map(r => `
              <tr>
                <td class="mono">${esc(r.name)}</td>
                <td>${esc(r.used)}</td>
                <td>${esc(r.avail)}</td>
                <td>${esc(r.refer)}</td>
                <td class="mono text-muted">${esc(r.mountpoint)}</td>
                <td class="advanced-only">
                  <button class="btn btn-secondary btn-sm" data-props="${esc(r.name)}">Properties</button>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
    el.querySelectorAll('[data-props]').forEach(b =>
      b.addEventListener('click', () => openProperties(b.dataset.props)));
  } catch (e) {
    el.innerHTML = errorState(e.message);
  }
}

// ── Dataset properties ───────────────────────────────────────────────────────
// The catalogue comes from the profile, and each property says which UI level
// it belongs to: the assisted level never gets here at all (the button that
// opens this is .advanced-only), and expert-only tunables stay hidden until
// the level is raised again.

let propCatalogue = null;

async function openProperties(dataset) {
  const body = document.getElementById('prop-body');
  document.getElementById('prop-dataset').textContent = dataset;
  document.getElementById('form-properties').dataset.dataset = dataset;
  body.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  openModal('modal-properties');

  try {
    if (!propCatalogue) {
      propCatalogue = (await apiJSON('GET', '/api/storage/properties')).properties;
    }
    const current = (await apiJSON(
      'GET', `/api/storage/datasets/${dataset}/properties`)).properties;
    body.innerHTML = Object.entries(propCatalogue)
      .filter(([, spec]) => !spec.create_only)
      .map(([name, spec]) => propertyRow(name, spec, current[name]))
      .join('');
  } catch (e) {
    body.innerHTML = errorState(e.message);
  }
}

function propertyRow(name, spec, cur) {
  const value = cur ? cur.value : (spec.default ?? '');
  const gate = spec.level === 'expert' ? ' expert-only'
             : spec.level === 'advanced' ? ' advanced-only' : '';
  const inherited = cur && cur.source !== 'local'
    ? `<span class="text-muted"> — ${esc(cur.source)}</span>` : '';
  const control = spec.type === 'enum'
    ? `<select class="select" data-prop="${esc(name)}" data-was="${esc(value)}">
         ${spec.values.map(v =>
           `<option${v === value ? ' selected' : ''}>${esc(v)}</option>`).join('')}
       </select>`
    : `<input class="input" type="text" data-prop="${esc(name)}"
              data-was="${esc(value)}" value="${esc(value)}">`;
  return `
    <div class="form-group${gate}">
      <label class="form-label mono">${esc(name)}${inherited}</label>
      ${control}
      <span class="form-hint">${esc(spec.help || '')}</span>
    </div>`;
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
      <div class="advanced-only mt-3">
        ${attachedDevices(p, 'cache', 'Cache (L2ARC)')}
        ${attachedDevices(p, 'log', 'Log (SLOG)')}
        <button class="btn btn-secondary btn-sm" data-attach="${esc(p.name)}">Add cache or log device</button>
      </div>
    </div>`;
}

// Cache and log hold no data: losing one costs speed, not files. That is why
// they can be attached to a pool that already exists — and detached again.
function attachedDevices(pool, role, label) {
  const devices = pool[`${role}_devices`] || [];
  if (!devices.length) return '';
  return `
    <div class="pool-detail mb-2">
      <span class="text-muted">${label}:</span>
      ${devices.map(d => `
        <span class="mono">${esc(d)}</span>
        <button class="btn btn-ghost btn-sm" title="Detach"
                data-detach="${esc(d)}" data-pool="${esc(pool.name)}">×</button>`).join(' ')}
    </div>`;
}

async function attachDevice(pool) {
  const role = prompt(`Attach to ${pool} — type "cache" for read cache (L2ARC) or "log" for the intent log (SLOG):`, 'cache');
  if (!role) return;
  if (role !== 'cache' && role !== 'log') { toast('Type cache or log.', 'err'); return; }
  const device = prompt(`Device for the ${role} (a whole disk or partition, e.g. /dev/disk/by-id/ata-SSD…):`);
  if (!device) return;
  try {
    const res = await apiJSON('POST', `/api/storage/pools/${pool}/devices`,
                              { role, devices: [device.trim()] });
    toast(`Attached ${res.attached.join(', ')} as ${role}`, 'ok');
    loadPools();
  } catch (e) { toast(e.message, 'err'); }
}

async function detachDevice(pool, device) {
  if (!confirm(`Detach ${device} from ${pool}?\n\nNo data is lost: a cache or log device holds none.`)) return;
  try {
    await apiJSON('DELETE',
      `/api/storage/pools/${pool}/devices?device=${encodeURIComponent(device)}`);
    toast(`Detached ${device}`, 'ok');
    loadPools();
  } catch (e) { toast(e.message, 'err'); }
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
