'use strict';

let _activeTab = 'pools';

document.addEventListener('DOMContentLoaded', async () => {
  if (!initShared()) return;

  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  document.getElementById('btn-create-snap')?.addEventListener('click', () => openModal('modal-snapshot'));
  bindModalClose('modal-snapshot');

  bindModalClose('modal-pool');
  document.getElementById('btn-create-pool')?.addEventListener('click', openPoolForm);
  document.getElementById('f-pool-encrypt')?.addEventListener('change', (e) => {
    document.getElementById('pool-passphrase-group').hidden = !e.target.checked;
  });
  document.getElementById('form-pool').addEventListener('submit', submitPool);

  bindModalClose('modal-import');
  document.getElementById('btn-import-pool')?.addEventListener('click', openImportForm);
  document.getElementById('btn-import-check').addEventListener('click', checkImport);
  document.getElementById('form-import').addEventListener('submit', submitImport);

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
    if (dl) dl.innerHTML = d.pools.map(p => `<option value="${esc(p.name)}">`).join('');
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
        <div class="pool-stat-item"><span class="pool-stat-label">Size</span><span class="pool-stat-value">${poolSize(p.size)}</span></div>
        <div class="pool-stat-item"><span class="pool-stat-label">Used</span><span class="pool-stat-value">${poolSize(p.used)}</span></div>
        <div class="pool-stat-item"><span class="pool-stat-label">Free</span><span class="pool-stat-value">${poolSize(p.free)}</span></div>
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
  if (!await confirm('Detach Device', `Detach <span class="mono">${esc(device)}</span> from <strong>${esc(pool)}</strong>? No data is lost: a cache or log device holds none.`, 'Detach')) return;
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
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}


// ── Creating a pool ──────────────────────────────────────────────────────────
// The one screen in the WebUI that destroys what it touches: it lists what is
// already on each device, refuses to preselect anything, and asks for the
// erase to be acknowledged before the button does anything.

async function openPoolForm() {
  const list = document.getElementById('pool-devices');
  list.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  document.getElementById('form-pool').reset();
  document.getElementById('pool-passphrase-group').hidden = true;
  openModal('modal-pool');

  let devices;
  try {
    devices = (await apiJSON('GET', '/api/storage/devices')).devices;
  } catch (e) {
    list.innerHTML = errorState(e.message);
    return;
  }

  const disks = devices.filter(d => d.type === 'disk' || d.type === 'part');
  if (!disks.length) { list.innerHTML = emptyState('No block devices found.'); return; }

  list.innerHTML = disks.map(d => `
    <label class="device-row${d.in_use ? ' device-busy' : ''}">
      <input type="checkbox" value="${esc(d.name)}"${d.in_use ? ' disabled' : ''}>
      <span class="mono">${esc(d.name)}</span>
      <span class="text-muted">${humanSize(d.size)}${d.model ? ' — ' + esc(d.model) : ''}</span>
      ${d.in_use ? `<span class="badge badge-warn">${esc(d.fstype || d.mountpoint)}</span>` : ''}
    </label>`).join('');

  const free = disks.filter(d => !d.in_use);
  for (const id of ['f-pool-cache', 'f-pool-log']) {
    const sel = document.getElementById(id);
    if (!sel) continue;
    sel.innerHTML = '<option value="">None</option>' +
      free.map(d => `<option value="${esc(d.name)}">${esc(d.name)} — ${humanSize(d.size)}</option>`).join('');
  }
}

// `zpool list -p` reports exact byte counts, which are unreadable as they
// stand; a profile without `-p` sends a string such as '3.6T' instead, and
// that is already how it should be shown.
function poolSize(value) {
  if (value === undefined || value === null || value === '') return '—';
  return typeof value === 'number' ? humanSize(value) : esc(value);
}

function humanSize(bytes) {
  if (!bytes) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

async function submitPool(e) {
  e.preventDefault();
  const name = document.getElementById('f-pool-name').value.trim();
  const devices = [...document.querySelectorAll('#pool-devices input:checked')].map(i => i.value);
  const cache = document.getElementById('f-pool-cache').value;
  const log = document.getElementById('f-pool-log')?.value || '';
  const encrypted = document.getElementById('f-pool-encrypt').checked;
  const passphrase = document.getElementById('f-pool-passphrase').value;

  if (!devices.length) { toast('Select at least one data device.', 'err'); return; }
  if (!await confirm('Create Pool', `Create <strong>${esc(name)}</strong> on:<br><span class="mono">${devices.map(esc).join('<br>')}</span><br><br>Everything on those devices will be destroyed.`, 'Create and erase')) return;

  const btn = document.getElementById('btn-pool-submit');
  btn.disabled = true;
  try {
    const res = await apiJSON('POST', '/api/storage/pools', {
      name,
      vdev_type: document.getElementById('f-pool-layout').value,
      devices,
      cache_devices: cache ? [cache] : [],
      log_devices: log ? [log] : [],
      encrypted,
      master_passphrase: encrypted ? passphrase : '',
    });
    if (res.warnings?.length) {
      toast(`Pool ${res.pool} created, but ${res.warnings[0].role} did not attach.`, 'warn');
    } else {
      toast(`Pool ${res.pool} created.`, 'ok');
    }
    closeModal('modal-pool');
    loadPools();
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    btn.disabled = false;
  }
}


// ── Importing a pool ─────────────────────────────────────────────────────────
// Two steps: Check imports the chosen pool read-only and exports it again, so
// the report below is what the pool holds; Import then brings it in with the
// name, mountpoint, passphrases, shares and users chosen from that report.

let _importReport = null;
let _importPassphrases = {};

async function openImportForm() {
  const list = document.getElementById('import-pools');
  _importReport = null;
  _importPassphrases = {};
  document.getElementById('form-import').reset();
  document.getElementById('import-report').innerHTML = '';
  document.getElementById('import-force-group').hidden = true;
  document.getElementById('btn-import-submit').disabled = true;
  list.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  openModal('modal-import');

  let pools;
  try {
    pools = (await apiJSON('GET', '/api/storage/pools/importable')).pools;
  } catch (e) {
    list.innerHTML = errorState(e.message);
    return;
  }
  if (!pools.length) { list.innerHTML = emptyState('No pool to import on the attached disks.'); return; }

  list.innerHTML = pools.map(p => `
    <label class="device-row${p.importable ? '' : ' device-busy'}">
      <input type="radio" name="import-pool" value="${esc(p.id)}" data-foreign="${p.foreign ? 1 : 0}"${p.importable ? '' : ' disabled'}>
      <span class="mono">${esc(p.name)}</span>
      <span class="text-muted">${esc(p.state)} — id ${esc(p.id)}</span>
      ${p.foreign ? '<span class="badge badge-warn">other system</span>' : ''}
      ${p.importable ? '' : `<span class="badge badge-err">${esc(p.status || 'cannot be imported')}</span>`}
    </label>`).join('');
  list.querySelectorAll('input[name="import-pool"]').forEach(r => r.addEventListener('change', () => {
    _importReport = null;
    _importPassphrases = {};
    document.getElementById('import-report').innerHTML = '';
    document.getElementById('btn-import-submit').disabled = true;
    document.getElementById('f-import-force').checked = false;
    document.getElementById('import-force-group').hidden = r.dataset.foreign !== '1';
  }));
}

function importRequest() {
  const chosen = document.querySelector('input[name="import-pool"]:checked');
  const body = {
    pool_id: chosen ? chosen.value : '',
    force: document.getElementById('f-import-force').checked,
  };
  const name = document.getElementById('f-import-name');
  const mountpoint = document.getElementById('f-import-mountpoint');
  if (name && name.value.trim()) body.name = name.value.trim();
  if (mountpoint && mountpoint.value.trim() && mountpoint.value.trim() !== mountpoint.dataset.was) {
    body.mountpoint = mountpoint.value.trim();
  }
  document.querySelectorAll('[data-passphrase]').forEach(i => {
    if (i.value) _importPassphrases[i.dataset.passphrase] = i.value;
  });
  body.passphrases = {..._importPassphrases};
  return body;
}

async function checkImport() {
  const body = importRequest();
  if (!body.pool_id) { toast('Choose a pool first.', 'err'); return; }
  const out = document.getElementById('import-report');
  const btn = document.getElementById('btn-import-check');
  btn.disabled = true;
  out.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    _importReport = await apiJSON('POST', '/api/storage/pools/import/inspect', body);
    renderImportReport(_importReport, body);
  } catch (e) {
    _importReport = null;
    out.innerHTML = errorState(e.message);
    document.getElementById('btn-import-submit').disabled = true;
  } finally {
    btn.disabled = false;
  }
}

function importBlocked(r) {
  if (r.refused.length) return true;
  if (r.name_taken || r.conflicts.length) return true;
  return r.encryption_roots.some(k => k.keyformat === 'passphrase' && !k.unlocked);
}

function renderImportReport(r, asked) {
  const out = document.getElementById('import-report');
  const parts = [];
  if (r.refused.length) {
    parts.push(`<div class="alert alert-err"><span>This pool cannot be imported: it holds another
      system's root, and its datasets would mount over this system's own directories
      (${r.refused.map(esc).join('; ')}).</span></div>`);
    out.innerHTML = parts.join('');
    document.getElementById('btn-import-submit').disabled = true;
    return;
  }

  parts.push(`<div class="form-group">
      <label class="form-label" for="f-import-name">Name on this system</label>
      <input id="f-import-name" class="input" type="text" value="${esc(r.pool.final_name)}" autocomplete="off">
      ${r.name_taken ? '<span class="form-hint text-err">A pool with this name already exists here: choose another, then Check again.</span>' : ''}
    </div>`);
  parts.push(`<div class="form-group">
      <label class="form-label" for="f-import-mountpoint">Mountpoint</label>
      <input id="f-import-mountpoint" class="input mono" type="text" value="${esc(r.root_mountpoint)}"
             data-was="${esc(asked.mountpoint ? '' : r.root_mountpoint)}" autocomplete="off">
      <span class="form-hint">${r.freedomnas
        ? 'Where the pool mounted on the system it comes from; datasets that inherit it follow.'
        : 'This pool was not made by FreedomNAS: check where it mounts. Changing it sets <span class="mono">zfs set mountpoint=</span> on the pool.'}</span>
    </div>`);

  for (const k of r.encryption_roots) {
    const label = esc(k.dataset || r.pool.final_name);
    if (k.keyformat !== 'passphrase') {
      parts.push(`<div class="alert alert-warn"><span>${label} is encrypted with a
        ${esc(k.keyformat || 'non-passphrase')} key: it cannot be unlocked here and stays locked.</span></div>`);
    } else if (!k.unlocked) {
      parts.push(`<div class="form-group">
          <label class="form-label" for="f-key-${label}">Passphrase for ${label}</label>
          <input id="f-key-${label}" class="input" type="password" data-passphrase="${esc(k.dataset)}" autocomplete="off">
          ${k.error ? `<span class="form-hint text-err">${esc(k.error)}</span>` : ''}
        </div>`);
    } else {
      parts.push(`<div class="alert alert-ok"><span>${label}: passphrase accepted.</span></div>`);
    }
  }

  for (const c of r.conflicts) {
    parts.push(`<div class="alert alert-warn"><span>${esc(c.dataset || r.pool.final_name)} would mount on
      <span class="mono">${esc(c.mountpoint)}</span>: ${esc(c.reason)}. Choose another mountpoint, then Check again.</span></div>`);
  }

  parts.push(`<div class="table-wrap"><table>
      <thead><tr><th>Dataset</th><th>Mountpoint</th><th>Used</th></tr></thead>
      <tbody>${r.datasets.map(d => `<tr>
        <td class="mono">${esc(d.dataset || r.pool.final_name)}${d.locked ? ' <span class="badge badge-warn">locked</span>' : ''}</td>
        <td class="mono">${d.canmount === 'on' ? esc(d.mountpoint) : '<span class="text-muted">not mounted</span>'}</td>
        <td>${humanSize(d.used)}</td></tr>`).join('')}</tbody>
    </table></div>`);

  if (r.config_error) {
    parts.push(`<div class="alert alert-warn"><span>Its FreedomNAS configuration could not be read: ${esc(r.config_error)}</span></div>`);
  }
  const shares = [...r.shares.smb.map(s => ({...s, kind: 'smb'})), ...r.shares.nfs.map(s => ({...s, kind: 'nfs'}))];
  if (shares.length) {
    parts.push(`<div class="form-group"><label class="form-label">Shares from its FreedomNAS configuration</label>
      <div class="device-list">${shares.map(s => `
        <label class="device-row${s.exists_here ? ' device-busy' : ''}">
          <input type="checkbox" data-share="${s.kind}" value="${esc(s.name)}"${s.exists_here ? ' disabled' : ' checked'}>
          <span class="badge badge-info">${s.kind.toUpperCase()}</span>
          <span class="mono">${esc(s.name)}</span>
          <span class="text-muted mono">${esc(s.path)}</span>
          ${s.exists_here ? '<span class="badge badge-warn">name already used here</span>' : ''}
        </label>`).join('')}</div></div>`);
  }
  parts.push('<div id="import-users"></div>');

  out.innerHTML = parts.join('');
  out.querySelectorAll('[data-share]').forEach(c => c.addEventListener('change', () => renderImportUsers(r)));
  renderImportUsers(r);
  document.getElementById('btn-import-submit').disabled = importBlocked(r);
}

function importUsersNeeded(r) {
  const chosen = new Set([...document.querySelectorAll('[data-share="smb"]:checked')].map(c => c.value));
  const needed = new Set();
  r.shares.smb.filter(s => chosen.has(s.name)).forEach(s => (s.users || []).forEach(u => needed.add(u)));
  return r.users.filter(u => needed.has(u.username) && !u.exists_here);
}

function renderImportUsers(r) {
  const box = document.getElementById('import-users');
  const kept = {};
  box.querySelectorAll('[data-user-password]').forEach(i => { kept[i.dataset.userPassword] = i.value; });
  const users = importUsersNeeded(r);
  if (!users.length) { box.innerHTML = ''; return; }
  box.innerHTML = `<div class="form-group"><label class="form-label">Users these shares need</label>
    <span class="form-hint">They do not exist here. Passwords are never stored in the configuration, so each needs a new one.</span>
    </div>
    ${users.map(u => `<div class="form-group">
      <label class="form-label"><span class="mono">${esc(u.username)}</span>
        <span class="text-muted">${u.uid ? (u.uid_taken ? `— uid ${esc(u.uid)} is taken here: a new one will be used` : `— uid ${esc(u.uid)}, as on the other system`) : ''}</span></label>
      <input class="input" type="password" placeholder="New password, at least 8 characters" autocomplete="new-password"
             data-user-password="${esc(u.username)}" value="${esc(kept[u.username] || '')}">
      ${u.admin ? `<label class="form-label"><input type="checkbox" data-user-admin="${esc(u.username)}"> Administrator, as on the other system</label>` : ''}
    </div>`).join('')}`;
}

async function submitImport(e) {
  e.preventDefault();
  if (!_importReport) { toast('Check the pool first.', 'err'); return; }
  const body = importRequest();
  body.smb = [...document.querySelectorAll('[data-share="smb"]:checked')].map(c => c.value);
  body.nfs = [...document.querySelectorAll('[data-share="nfs"]:checked')].map(c => c.value);
  body.users = [...document.querySelectorAll('[data-user-password]')].map(i => ({
    username: i.dataset.userPassword,
    password: i.value,
    admin: !!document.querySelector(`[data-user-admin="${CSS.escape(i.dataset.userPassword)}"]`)?.checked,
  }));
  if (!await confirm('Import Pool', `Import <strong>${esc(body.name || _importReport.pool.final_name)}</strong>?`, 'Import')) return;

  const btn = document.getElementById('btn-import-submit');
  btn.disabled = true;
  try {
    const res = await apiJSON('POST', '/api/storage/pools/import', body);
    const extra = [res.users.length ? `${res.users.length} user(s)` : '', res.shares.length ? `${res.shares.length} share(s)` : '']
      .filter(Boolean).join(' and ');
    toast(`Pool ${res.pool} imported${extra ? ' with ' + extra : ''}.`, res.warnings.length ? 'warn' : 'ok');
    res.warnings.forEach(w => toast(w, 'warn', 8000));
    closeModal('modal-import');
    loadPools();
  } catch (err) {
    toast(err.message, 'err');
    btn.disabled = false;
  }
}
