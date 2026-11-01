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
    document.getElementById('pool-usb-group').hidden = !e.target.checked;
    if (e.target.checked) loadUsbKeys();
  });
  document.getElementById('form-pool').addEventListener('submit', submitPool);

  bindModalClose('modal-dataset');
  document.getElementById('form-dataset').addEventListener('submit', (e) => {
    e.preventDefault();
    if (_datasetSubmit) _datasetSubmit();
  });

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
      const recursive = document.getElementById('f-snap-recursive')?.checked || false;
      await apiJSON('POST', '/api/storage/snapshots', { dataset, snap_name: name, recursive });
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
  stopPoolDetail();
  stopReplicationPoll();
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `panel-${tab}`));

  const createBtn = document.getElementById('btn-create-snap');
  if (createBtn) createBtn.hidden = tab !== 'snapshots';

  if (tab === 'pools')     loadPools();
  if (tab === 'datasets')  loadDatasets();
  if (tab === 'snapshots') loadSnapshots();
  if (tab === 'replication') loadReplication();
  if (tab === 'events') loadEvents();
  if (tab === 'disks')     loadDisks();
}

async function loadPools() {
  const el = document.getElementById('panel-pools');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    const d = await apiJSON('GET', '/api/storage/pools');
    if (!d.pools?.length) { el.innerHTML = emptyState('No ZFS pools found.'); return; }
    el.innerHTML = '<div id="arc-card" class="advanced-only"></div>' + d.pools.map(renderPool).join('');
    loadArc();
    el.querySelectorAll('[data-attach]').forEach(b =>
      b.addEventListener('click', () => attachDevice(b.dataset.attach)));
    el.querySelectorAll('[data-detach]').forEach(b =>
      b.addEventListener('click', () => detachDevice(b.dataset.pool, b.dataset.detach)));
    el.querySelectorAll('[data-export]').forEach(b =>
      b.addEventListener('click', () => openRemovePool(b.dataset.export, 'export')));
    el.querySelectorAll('[data-destroy-pool]').forEach(b =>
      b.addEventListener('click', () => openRemovePool(b.dataset.destroyPool, 'destroy')));
    el.querySelectorAll('[data-pool-detail]').forEach(b =>
      b.addEventListener('click', () => openPoolDetail(b.dataset.poolDetail)));
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
    _datasetRows = d.datasets || [];
    const header = `<div class="section-header">
        <span class="section-title">Datasets</span>
        <span>
          <button class="btn btn-secondary btn-sm advanced-only" id="btn-create-volume">Create Volume</button>
          <button class="btn btn-primary btn-sm" id="btn-create-dataset">Create Dataset</button>
        </span>
      </div>`;
    if (!_datasetRows.length) {
      el.innerHTML = header + emptyState('No datasets found.');
    } else {
      el.innerHTML = header + `
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Dataset</th><th>Used</th><th>Available</th><th>Referenced</th><th>Mount</th>
            <th class="advanced-only"></th>
          </tr></thead>
          <tbody>
            ${_datasetRows.map(r => `
              <tr>
                <td class="mono">${esc(r.name)}${r.protected
                  ? ` <span class="badge badge-muted" title="${esc(r.protected)}">system</span>` : ''}</td>
                <td>${poolSize(r.used)}</td>
                <td>${poolSize(r.avail)}</td>
                <td>${poolSize(r.refer)}</td>
                <td class="mono text-muted">${esc(r.mountpoint)}</td>
                <td class="advanced-only" style="white-space: nowrap">${r.protected ? '' : `
                  <button class="btn btn-secondary btn-sm" data-props="${esc(r.name)}">Properties</button>
                  ${r.type === 'volume' ? '' : `<button class="btn btn-secondary btn-sm" data-move="${esc(r.name)}">Mountpoint</button>`}
                  ${r.name.includes('/') ? `
                  <button class="btn btn-secondary btn-sm" data-rename="${esc(r.name)}">Rename</button>
                  <button class="btn btn-danger btn-sm" data-destroy="${esc(r.name)}">Delete</button>` : ''}
                  ${datasetMenu(r)}`}
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
    }
    el.querySelector('#btn-create-dataset').addEventListener('click', openCreateDataset);
    el.querySelector('#btn-create-volume')?.addEventListener('click', openCreateVolume);
    el.querySelectorAll('select[data-ds-menu]').forEach(sel => sel.addEventListener('change', () => {
      const action = sel.value;
      sel.value = '';
      if (action) datasetAction(sel.dataset.dsMenu, action);
    }));
    el.querySelectorAll('[data-props]').forEach(b =>
      b.addEventListener('click', () => openProperties(b.dataset.props)));
    el.querySelectorAll('[data-move]').forEach(b =>
      b.addEventListener('click', () => openMoveDataset(b.dataset.move)));
    el.querySelectorAll('[data-rename]').forEach(b =>
      b.addEventListener('click', () => openRenameDataset(b.dataset.rename)));
    el.querySelectorAll('[data-destroy]').forEach(b =>
      b.addEventListener('click', () => openDestroyDataset(b.dataset.destroy)));
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
      .map(([name, spec]) => propertyRow(name, spec, current[name], dataset))
      .join('');
    body.querySelectorAll('[data-reset]').forEach(b => b.addEventListener('click', async () => {
      try {
        await apiJSON('POST', `/api/storage/datasets/${dataset}/inherit`, { property: b.dataset.reset });
        toast(`${b.dataset.reset} is inherited again.`, 'ok');
        openProperties(dataset);
      } catch (e) { toast(e.message, 'err'); }
    }));
  } catch (e) {
    body.innerHTML = errorState(e.message);
  }
}

function propertyRow(name, spec, cur, resetFor = '') {
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
  const reset = resetFor && cur && cur.source === 'local'
    ? ` <button type="button" class="btn btn-ghost btn-sm" data-reset="${esc(name)}" title="Use the inherited value again">Reset</button>` : '';
  return `
    <div class="form-group${gate}">
      <label class="form-label mono">${esc(name)}${inherited}${reset}</label>
      ${control}
      <span class="form-hint">${esc(spec.help || '')}</span>
    </div>`;
}

let _snapRows = [];
let _snapFilter = '';
let _snapPolicies = [];

const SNAP_INTERVALS = [
  ['hourly', 'Hourly'],
  ['daily', 'Daily'],
  ['weekly', 'Weekly'],
  ['monthly', 'Monthly'],
];

function renderSchedules() {
  return `
    <div class="section-header">
      <span class="section-title">Schedules</span>
      <button class="btn btn-primary btn-sm" id="btn-add-schedule">Add Schedule</button>
    </div>
    ${_snapPolicies.length ? `
    <div class="table-wrap"><table>
      <thead><tr><th>Dataset</th>${SNAP_INTERVALS.map(([, label]) => `<th>${label}</th>`).join('')}<th></th></tr></thead>
      <tbody>${_snapPolicies.map(p => `
        <tr>
          <td class="mono">${esc(p.dataset)}</td>
          ${SNAP_INTERVALS.map(([key]) => `<td>${p[key] ? `keep ${p[key]}` : '<span class="text-muted">off</span>'}</td>`).join('')}
          <td>
            <button class="btn btn-ghost btn-sm" data-schedule-edit="${esc(p.dataset)}">Edit</button>
            <button class="btn btn-ghost btn-sm" data-schedule-remove="${esc(p.dataset)}">Remove</button>
          </td>
        </tr>`).join('')}</tbody>
    </table></div>` : '<p class="text-muted">No schedules. A schedule takes snapshots of one dataset every hour, day, week or month and keeps the newest of each.</p>'}
    <div class="section-header mt-6"><span class="section-title">Snapshots</span></div>`;
}

async function openSchedule(dataset) {
  let names;
  try {
    names = ((await apiJSON('GET', '/api/storage/datasets')).datasets || [])
      .filter(r => r.type !== 'volume' && !r.name.split('/').includes('.freedomnas')).map(r => r.name);
  } catch (e) { toast(e.message, 'err'); return; }
  const current = _snapPolicies.find(p => p.dataset === dataset);
  const scheduled = new Set(_snapPolicies.map(p => p.dataset));
  const choices = dataset ? [dataset] : names.filter(n => !scheduled.has(n));
  if (!choices.length) { toast('Every dataset already has a schedule.', 'err'); return; }
  const defaults = { hourly: 24, daily: 7, weekly: 4, monthly: 12 };
  datasetDialog(dataset ? `Schedule — ${dataset}` : 'Add Schedule', 'Save', `
    <div class="form-group">
      <label class="form-label" for="sched-dataset">Dataset</label>
      <select class="select mono" id="sched-dataset"${dataset ? ' disabled' : ''}>
        ${choices.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('')}
      </select>
      <span class="form-hint">Only this dataset: a child dataset needs a schedule of its own.</span>
    </div>
    ${SNAP_INTERVALS.map(([key, label]) => `
    <div class="form-group">
      <label class="form-label" for="sched-${key}">${label} snapshots to keep</label>
      <input class="input" id="sched-${key}" type="number" min="0" step="1"
             value="${current ? current[key] || 0 : defaults[key]}">
    </div>`).join('')}
    <span class="form-hint">0 takes none of that kind. Only the snapshots a schedule took are ever deleted; those taken by hand stay.</span>`,
    async () => {
      const name = document.getElementById('sched-dataset').value;
      const body = {};
      for (const [key] of SNAP_INTERVALS) {
        const value = document.getElementById(`sched-${key}`).value.trim();
        body[key] = value === '' ? 0 : Number(value);
      }
      await apiJSON('PUT', `/api/storage/snapshot-policies/${apiPath(name)}`, body);
      toast('Schedule saved.', 'ok');
      closeModal('modal-dataset');
      loadSnapshots();
    });
}

async function removeSchedule(dataset) {
  if (!await confirm('Remove schedule', `Stop taking scheduled snapshots of <span class="mono">${esc(dataset)}</span>? The snapshots already taken stay.`, 'Remove')) return;
  try {
    await apiJSON('DELETE', `/api/storage/snapshot-policies/${apiPath(dataset)}`);
    toast('Schedule removed.', 'ok');
    loadSnapshots();
  } catch (e) { toast(e.message, 'err'); }
}

async function loadSnapshots() {
  const el = document.getElementById('panel-snapshots');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    const [d, p] = await Promise.all([
      apiJSON('GET', '/api/storage/snapshots'),
      apiJSON('GET', '/api/storage/snapshot-policies'),
    ]);
    _snapRows = d.snapshots || [];
    _snapPolicies = p.policies || [];
    let marks = [];
    if (uiLevel() === 'expert') {
      try { marks = (await apiJSON('GET', '/api/storage/bookmarks')).bookmarks; } catch { marks = []; }
    }
    renderSnapshots(el, marks);
  } catch (e) {
    el.innerHTML = errorState(e.message);
  }
}

function snapTime(value) {
  return typeof value === 'number' ? new Date(value * 1000).toLocaleString() : esc(value);
}

function renderSnapshots(el, marks) {
  const q = _snapFilter.toLowerCase();
  const rows = _snapRows.filter(r => !q || r.name.toLowerCase().includes(q));
  const expert = uiLevel() === 'expert';
  el.innerHTML = renderSchedules() + `
    <div class="section-header">
      <input class="input" id="snap-filter" type="search" placeholder="Filter by dataset or name"
             value="${esc(_snapFilter)}" style="max-width:22rem">
      <button class="btn btn-danger btn-sm" id="snap-destroy-selected" disabled>Destroy selected</button>
    </div>
    ${rows.length ? `
    <div class="table-wrap"><table>
      <thead><tr><th><input type="checkbox" id="snap-all"></th><th>Snapshot</th><th>Created</th>
        <th>Used</th><th>Referenced</th><th>Holds</th><th>Clones</th><th></th></tr></thead>
      <tbody>${rows.map(r => `
        <tr>
          <td><input type="checkbox" data-snap-pick="${esc(r.name)}"></td>
          <td class="mono">${esc(r.name)}</td>
          <td class="text-muted">${snapTime(r.creation)}</td>
          <td>${r.used === 0 ? '0 B' : poolSize(r.used)}</td>
          <td>${poolSize(r.referenced)}</td>
          <td>${r.holds ? `<span class="badge badge-info">${r.holds}</span>` : ''}</td>
          <td class="mono text-muted">${esc((r.clones || []).join(', '))}</td>
          <td><select class="select" data-snap-menu="${esc(r.name)}" style="width:auto">
            <option value="">Actions…</option>
            <option value="rollback">Roll back…</option>
            <option value="clone">Clone…</option>
            <option value="rename">Rename…</option>
            <option value="holds">Holds…</option>
            <option value="diff">Changes since…</option>
            ${expert ? '<option value="bookmark">Bookmark…</option>' : ''}
            <option value="destroy">Destroy…</option>
          </select></td>
        </tr>`).join('')}</tbody>
    </table></div>` : emptyState(_snapRows.length ? 'No snapshot matches.' : 'No snapshots found.')}
    ${expert ? `
    <div class="section-header mt-6"><span class="section-title">Bookmarks</span></div>
    ${marks.length ? `<div class="table-wrap"><table>
      <thead><tr><th>Bookmark</th><th>Created</th><th></th></tr></thead>
      <tbody>${marks.map(m => `<tr><td class="mono">${esc(m.name)}</td><td class="text-muted">${snapTime(m.creation)}</td>
        <td><button class="btn btn-ghost btn-sm" data-bookmark-del="${esc(m.name)}">Delete</button></td></tr>`).join('')}</tbody>
    </table></div>` : '<p class="text-muted">No bookmarks. A bookmark keeps the point of a snapshot after it is destroyed, as the base of an incremental send.</p>'}` : ''}`;

  el.querySelector('#btn-add-schedule').addEventListener('click', () => openSchedule(''));
  el.querySelectorAll('[data-schedule-edit]').forEach(b =>
    b.addEventListener('click', () => openSchedule(b.dataset.scheduleEdit)));
  el.querySelectorAll('[data-schedule-remove]').forEach(b =>
    b.addEventListener('click', () => removeSchedule(b.dataset.scheduleRemove)));
  const filter = el.querySelector('#snap-filter');
  filter.addEventListener('input', () => {
    _snapFilter = filter.value;
    renderSnapshots(el, marks);
    const again = el.querySelector('#snap-filter');
    again.focus();
    again.setSelectionRange(again.value.length, again.value.length);
  });
  const picks = () => [...el.querySelectorAll('[data-snap-pick]:checked')].map(c => c.dataset.snapPick);
  const destroyBtn = el.querySelector('#snap-destroy-selected');
  el.querySelectorAll('[data-snap-pick]').forEach(c => c.addEventListener('change', () => {
    destroyBtn.disabled = !picks().length;
  }));
  el.querySelector('#snap-all')?.addEventListener('change', (e) => {
    el.querySelectorAll('[data-snap-pick]').forEach(c => { c.checked = e.target.checked; });
    destroyBtn.disabled = !picks().length;
  });
  destroyBtn.addEventListener('click', () => destroySnapshots(picks()));
  el.querySelectorAll('select[data-snap-menu]').forEach(sel => sel.addEventListener('change', () => {
    const action = sel.value;
    sel.value = '';
    if (action) snapshotAction(sel.dataset.snapMenu, action);
  }));
  el.querySelectorAll('[data-bookmark-del]').forEach(b => b.addEventListener('click', async () => {
    if (!await confirm('Delete bookmark', `Delete <span class="mono">${esc(b.dataset.bookmarkDel)}</span>? It can no longer be the base of an incremental send.`, 'Delete')) return;
    try {
      await apiJSON('DELETE', `/api/storage/bookmarks/${encodeURIComponent(b.dataset.bookmarkDel)}`);
      toast('Bookmark deleted.', 'ok');
      loadSnapshots();
    } catch (e) { toast(e.message, 'err'); }
  }));
}

const DISK_HEALTH = {
  ok:      ['ok',    'OK'],
  warning: ['warn',  'Attention'],
  failing: ['err',   'Failing'],
  standby: ['muted', 'Asleep'],
  unknown: ['muted', 'No SMART data'],
};

async function loadDisks() {
  const el = document.getElementById('panel-disks');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    const d = await apiJSON('GET', '/api/storage/disks/');
    if (!d.available) {
      el.innerHTML = emptyState('SMART tools are not installed: install smartmontools to see the health of the disks.');
      return;
    }
    if (!d.disks.length) { el.innerHTML = emptyState('No disks found.'); return; }
    el.innerHTML = d.disks.map(renderDisk).join('');
    el.querySelectorAll('[data-smart-test]').forEach(b =>
      b.addEventListener('click', () => startSelfTest(b, b.dataset.disk, b.dataset.smartTest)));
  } catch (e) {
    el.innerHTML = errorState(e.message);
  }
}

function renderDisk(d) {
  const [badge, label] = DISK_HEALTH[d.health] || DISK_HEALTH.unknown;
  const hint = d.health === 'failing'
    ? 'Copy the data on this disk elsewhere and replace it as soon as possible.'
    : d.health === 'warning'
      ? 'The disk shows signs of wear: keep a backup of its data and watch it.'
      : '';
  const smart = d.health !== 'standby' && d.health !== 'unknown';
  const test = d.test_running
    ? `Self-test running, ${esc(d.test_running.remaining_percent)}% left`
    : d.last_test
      ? `Last self-test: ${esc(d.last_test.type)} — ${esc(d.last_test.status)}${d.last_test.hours != null ? ` (at ${esc(d.last_test.hours)} h)` : ''}`
      : 'No self-test recorded';
  return `
    <div class="pool-card mb-4">
      <div class="pool-header">
        <div>
          <div class="pool-name"><span class="mono">${esc(d.name)}</span> ${esc(d.model)}</div>
          <div class="pool-detail">${[d.kind, humanSize(d.size), d.serial && `S/N ${esc(d.serial)}`,
            d.pools.length ? `pool ${d.pools.map(esc).join(', ')}` : 'not in a pool']
            .filter(Boolean).join(' · ')}</div>
        </div>
        <span class="badge badge-${badge}">${label}</span>
      </div>
      <div class="pool-stats">
        <div class="pool-stat-item"><span class="pool-stat-label">Temperature</span><span class="pool-stat-value">${d.temperature != null ? `${esc(d.temperature)} °C` : '—'}</span></div>
        <div class="pool-stat-item"><span class="pool-stat-label">Powered on</span><span class="pool-stat-value">${d.power_on_hours != null ? `${esc(d.power_on_hours)} h` : '—'}</span></div>
      </div>
      ${hint ? `<p class="form-hint mt-3">${hint}</p>` : ''}
      ${d.health === 'standby' || d.health === 'unknown' ? `<p class="form-hint mt-3">${d.reasons.map(esc).join(' ')}</p>` : ''}
      <div class="advanced-only mt-3">
        ${smart && d.reasons.length ? `<ul class="text-muted">${d.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}
        ${smart ? `<p class="text-muted">${test}</p>
          <button class="btn btn-secondary btn-sm" data-disk="${esc(d.name)}" data-smart-test="short">Short self-test</button>
          <button class="btn btn-secondary btn-sm expert-only" data-disk="${esc(d.name)}" data-smart-test="long">Long self-test</button>` : ''}
      </div>
      ${d.attributes.length ? `
      <details class="expert-only mt-3">
        <summary>SMART attributes${d.error_count != null ? ` · ${esc(d.error_count)} errors logged` : ''}</summary>
        <div class="table-wrap">
          <table>
            <thead><tr><th>ID</th><th>Attribute</th><th>Value</th><th>Worst</th><th>Threshold</th><th>Raw</th><th>Failed</th></tr></thead>
            <tbody>${d.attributes.map(a => `
              <tr><td>${esc(a.id)}</td><td class="mono">${esc(a.name)}</td><td>${esc(a.value)}</td>
                <td>${esc(a.worst)}</td><td>${esc(a.thresh)}</td><td class="mono">${esc(a.raw)}</td>
                <td>${esc(a.when_failed || '—')}</td></tr>`).join('')}
            </tbody>
          </table>
        </div>
      </details>` : ''}
    </div>`;
}

async function startSelfTest(btn, disk, type) {
  if (type === 'long' && !await confirm('Long self-test',
      `The long self-test reads the whole surface of ${disk} and can take hours. The disk stays usable, only slower.`,
      'Start')) return;
  setLoading(btn, true, '…');
  try {
    const r = await apiJSON('POST', `/api/storage/disks/${encodeURIComponent(disk)}/test`, { type });
    toast(r.detail, 'ok');
    loadDisks();
  } catch (e) {
    toast(`Error: ${e.message}`, 'err');
    setLoading(btn, false);
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
        <button class="btn btn-primary btn-sm mb-4" data-pool-detail="${esc(p.name)}">Details and devices</button>
        ${attachedDevices(p, 'cache', 'Cache (L2ARC)')}
        ${attachedDevices(p, 'log', 'Log (SLOG)')}
        <button class="btn btn-secondary btn-sm" data-attach="${esc(p.name)}">Add cache or log device</button>
        <div class="mt-3">${p.protected
          ? `<span class="text-muted">Cannot be exported or deleted: ${esc(p.protected)}.</span>`
          : `<button class="btn btn-secondary btn-sm" data-export="${esc(p.name)}">Export</button>
             <button class="btn btn-danger btn-sm" data-destroy-pool="${esc(p.name)}">Delete pool</button>`}
        </div>
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
  let devices;
  try { devices = (await apiJSON('GET', '/api/storage/devices')).devices; }
  catch (e) { toast(e.message, 'err'); return; }
  const disks = devices.filter(d => d.type === 'disk' || d.type === 'part');
  const list = disks.length ? disks.map(d => `
    <label class="device-row${d.in_use ? ' device-busy' : ''}">
      <input type="radio" name="attach-device" value="${esc(d.name)}"${d.in_use ? ' disabled' : ''}>
      <span class="mono">${esc(d.name)}</span>
      <span class="text-muted">${humanSize(d.size)}${d.model ? ' — ' + esc(d.model) : ''}</span>
      ${d.in_use ? `<span class="badge badge-warn">${d.reserved ? 'ZFS reserved' : esc(d.fstype || d.mountpoint || 'in use')}</span>` : ''}
    </label>`).join('') : emptyState('No block devices found.');
  datasetDialog(`Add cache or log device — ${pool}`, 'Add device', `
    <div class="form-group">
      <label class="form-label">Role</label>
      <div class="device-list">
        <label class="device-row"><input type="radio" name="attach-role" value="cache" checked>
          <span><strong>Cache (L2ARC)</strong> — an SSD used as a read cache. It holds no data: losing it costs speed, not files.</span></label>
        <label class="device-row"><input type="radio" name="attach-role" value="log">
          <span><strong>Log (SLOG)</strong> — only helps synchronous writes (NFS, databases).</span></label>
      </div>
    </div>
    <div class="form-group">
      <label class="form-label">Device</label>
      <div class="device-list">${list}</div>
    </div>
    <div class="form-group">
      <label class="form-label"><input type="checkbox" id="attach-confirm">
        I understand the selected device will be erased</label>
    </div>`, async () => {
      const role = document.querySelector('input[name="attach-role"]:checked')?.value;
      const device = document.querySelector('input[name="attach-device"]:checked')?.value;
      if (!device) { toast('Select a device.', 'err'); return; }
      if (!document.getElementById('attach-confirm').checked) {
        toast('Tick that the device will be erased.', 'err'); return;
      }
      const label = role === 'cache' ? 'read cache (L2ARC)' : 'intent log (SLOG)';
      if (!await confirm('Add Device', `Add <span class="mono">${esc(device)}</span> to <strong>${esc(pool)}</strong> as ${label}? Everything on it is destroyed.`, 'Add and erase')) return;
      const res = await apiJSON('POST', `/api/storage/pools/${pool}/devices`, { role, devices: [device] });
      toast(`Attached ${res.attached.join(', ')} as ${role}.`, 'ok');
      closeModal('modal-dataset');
      loadPools();
    });
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
  document.getElementById('pool-usb-group').hidden = true;
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
      ${d.in_use ? '' : `<span class="advanced-only">${vdevGroupSelect(d.name)}</span>`}
      <span class="mono">${esc(d.name)}</span>
      <span class="text-muted">${humanSize(d.size)}${d.model ? ' — ' + esc(d.model) : ''}</span>
      ${d.in_use ? `<span class="badge badge-warn">${d.reserved ? 'ZFS reserved' : esc(d.fstype || d.mountpoint || 'in use')}</span>` : ''}
    </label>`).join('');

  const free = disks.filter(d => !d.in_use);
  for (const id of ['f-pool-cache', 'f-pool-log']) {
    const sel = document.getElementById(id);
    if (!sel) continue;
    sel.innerHTML = '<option value="">None</option>' +
      free.map(d => `<option value="${esc(d.name)}">${esc(d.name)} — ${humanSize(d.size)}</option>`).join('');
  }
}

async function loadUsbKeys() {
  const box = document.getElementById('pool-usb');
  box.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    const [usb, all] = await Promise.all([
      apiJSON('GET', '/api/storage/usb-devices'),
      apiJSON('GET', '/api/storage/devices'),
    ]);
    const info = Object.fromEntries(all.devices.map(d => [d.name, d]));
    if (!usb.devices.length) {
      box.innerHTML = '<span class="text-muted">No USB stick found. Plug one in and tick Encrypt again.</span>';
      return;
    }
    box.innerHTML = usb.devices.map(name => {
      const d = info[name] || {};
      const parent = info[d.parent] || {};
      return `<label class="device-row">
        <input type="checkbox" value="${esc(name)}">
        <span class="mono">${esc(name)}</span>
        <span class="text-muted">${humanSize(d.size)}${parent.model ? ' — ' + esc(parent.model) : ''}${d.fstype ? ' — ' + esc(d.fstype) : ''}</span>
      </label>`;
    }).join('');
  } catch (e) {
    box.innerHTML = errorState(e.message);
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
  const usb = encrypted ? [...document.querySelectorAll('#pool-usb input:checked')].map(i => i.value) : [];
  const layout = document.getElementById('f-pool-layout').value;
  const groups = uiLevel() === 'assisted' ? [] : collectVdevGroups(
    document.getElementById('pool-devices'), layout).filter(g => g.devices.some(d => devices.includes(d)))
    .map(g => ({ ...g, devices: g.devices.filter(d => devices.includes(d)) }));
  const ungrouped = devices.filter(d => !groups.some(g => g.devices.includes(d)));
  const advancedLayout = groups.length && groups.some(g => !(g.class === 'data' && g.devices.length === devices.length));
  if (advancedLayout && ungrouped.length) {
    toast(`Choose a vdev for ${ungrouped.join(', ')}, or untick it.`, 'err'); return;
  }

  if (!devices.length) { toast('Select at least one data device.', 'err'); return; }
  if (!await confirm('Create Pool', `Create <strong>${esc(name)}</strong> on:<br><span class="mono">${devices.map(esc).join('<br>')}</span><br><br>Everything on those devices will be destroyed.`, 'Create and erase')) return;

  const btn = document.getElementById('btn-pool-submit');
  btn.disabled = true;
  try {
    const res = await apiJSON('POST', '/api/storage/pools', {
      name,
      vdev_type: layout,
      devices,
      vdevs: advancedLayout ? groups : [],
      cache_devices: cache ? [cache] : [],
      log_devices: log ? [log] : [],
      encrypted,
      master_passphrase: encrypted ? passphrase : '',
      usb_devices: usb,
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
let _importUnlock = {};

async function openImportForm() {
  const list = document.getElementById('import-pools');
  _importReport = null;
  _importPassphrases = {};
  _importUnlock = {};
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
    _importUnlock = {};
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
  const method = document.querySelector('input[name="unlock-method"]:checked');
  if (method) _importUnlock.method = method.value;
  const master = document.getElementById('f-unlock-passphrase');
  if (master && master.value) _importUnlock.passphrase = master.value;
  if (_importUnlock.method) body.keyslot_unlock = {..._importUnlock};
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
  return r.encryption_roots.some(k => k.blocking);
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
    if (k.unlock_methods?.length && !k.unlocked) {
      const m = k.unlock_methods;
      const pick = (v) => _importUnlock.method === v ? ' checked' : '';
      parts.push(`<div class="form-group">
          <label class="form-label">Unlock ${label} — encrypted by FreedomNAS</label>
          <div class="device-list">
          ${m.includes('usb') ? `<label class="device-row"><input type="radio" name="unlock-method" value="usb"${pick('usb')}>
              USB key plugged into this NAS</label>` : ''}
          ${m.includes('file') ? `<label class="device-row"><input type="radio" name="unlock-method" value="file"${pick('file')}>
              <span>The <span class="mono">freedomnas.key</span> file from this computer</span>
              <input type="file" id="f-unlock-file"></label>` : ''}
          ${m.includes('passphrase') ? `<label class="device-row"><input type="radio" name="unlock-method" value="passphrase"${pick('passphrase')}>
              <span>Master Passphrase</span>
              <input type="password" class="input" id="f-unlock-passphrase" autocomplete="off"></label>` : ''}
          </div>
          ${k.error && asked.keyslot_unlock ? `<span class="form-hint text-err">${esc(k.error)}</span>` : ''}
        </div>`);
    } else if (k.unlock_methods?.length) {
      parts.push(`<div class="alert alert-ok"><span>${label}: unlocked.</span></div>`);
    } else if (k.keyformat !== 'passphrase') {
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

  if (r.config_locked) {
    parts.push('<div class="alert alert-info"><span>This pool comes from FreedomNAS: unlock it, then Check again, to see its shares and users.</span></div>');
  }
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
  document.getElementById('f-unlock-file')?.addEventListener('change', readUnlockFile);
  out.querySelectorAll('[data-share]').forEach(c => c.addEventListener('change', () => renderImportUsers(r)));
  renderImportUsers(r);
  document.getElementById('btn-import-submit').disabled = importBlocked(r);
}

function readUnlockFile(e) {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    const bytes = new Uint8Array(reader.result);
    let text = '';
    bytes.forEach(b => { text += String.fromCharCode(b); });
    _importUnlock.key = btoa(text);
    _importUnlock.method = 'file';
    const radio = document.querySelector('input[name="unlock-method"][value="file"]');
    if (radio) radio.checked = true;
  };
  reader.readAsArrayBuffer(file);
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



// ── Managing datasets ────────────────────────────────────────────────────────
// One dialog, filled for each action. Datasets of the pool the system runs
// from, and .freedomnas, carry no buttons: the API refuses them anyway.

let _datasetRows = [];
let _datasetSubmit = null;

function datasetDialog(title, button, html, submit, danger = false) {
  document.getElementById('dataset-title').textContent = title;
  document.getElementById('dataset-body').innerHTML = html;
  const btn = document.getElementById('btn-dataset-submit');
  btn.textContent = button;
  btn.className = `btn ${danger ? 'btn-danger' : 'btn-primary'}`;
  btn.disabled = false;
  _datasetSubmit = async () => {
    btn.disabled = true;
    try { await submit(); } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; }
  };
  openModal('modal-dataset');
}

function mountpointChoice(current, allowInherit = true) {
  return `<div class="form-group">
      <label class="form-label">Mountpoint</label>
      <div class="device-list">
        ${allowInherit ? `<label class="device-row"><input type="radio" name="ds-mode" value="inherit" checked>
          <span>Inherit from the parent</span> <span class="text-muted mono" id="ds-inherit-hint"></span></label>` : ''}
        <label class="device-row"><input type="radio" name="ds-mode" value="path"${allowInherit ? '' : ' checked'}>
          <span>This path</span>
          <input class="input mono" id="ds-mountpoint" type="text" value="${esc(current || '')}" placeholder="/mnt/data" autocomplete="off"></label>
        <label class="device-row"><input type="radio" name="ds-mode" value="none">
          <span>Not mounted</span> <span class="text-muted">— reachable only through its children</span></label>
      </div>
      <span class="form-hint">Not a system directory, nor one already in use or holding files.</span>
    </div>`;
}

function chosenMountpoint() {
  const mode = document.querySelector('input[name="ds-mode"]:checked')?.value || 'inherit';
  return { mode, mountpoint: mode === 'path' ? document.getElementById('ds-mountpoint').value.trim() : '' };
}

async function openCreateDataset() {
  if (!propCatalogue) {
    try { propCatalogue = (await apiJSON('GET', '/api/storage/properties')).properties; }
    catch (e) { toast(e.message, 'err'); return; }
  }
  const parents = _datasetRows.filter(r => !r.protected);
  if (!parents.length) { toast('No pool to create a dataset in: create or import one first.', 'err'); return; }
  const extras = Object.entries(propCatalogue)
    .filter(([name, spec]) => spec.create_only || name === 'compression' || name === 'quota')
    .map(([name, spec]) => {
      const gate = spec.level === 'expert' ? ' expert-only' : spec.level === 'advanced' ? ' advanced-only' : '';
      const control = spec.type === 'enum'
        ? `<select class="select" data-new-prop="${esc(name)}"><option value="">inherit / default</option>
             ${spec.values.map(v => `<option>${esc(v)}</option>`).join('')}</select>`
        : `<input class="input" type="text" data-new-prop="${esc(name)}" placeholder="e.g. 500G — empty for none">`;
      return `<div class="form-group${gate}"><label class="form-label">${esc(name)}</label>${control}
        ${spec.help ? `<span class="form-hint">${esc(spec.help)}</span>` : ''}</div>`;
    }).join('');
  datasetDialog('Create Dataset', 'Create', `
    <div class="form-group">
      <label class="form-label" for="ds-parent">Inside</label>
      <select class="select mono" id="ds-parent">
        ${parents.map(r => `<option value="${esc(r.name)}" data-mp="${esc(r.mountpoint)}">${esc(r.name)}</option>`).join('')}
      </select>
    </div>
    <div class="form-group">
      <label class="form-label" for="ds-name">Name</label>
      <input class="input" id="ds-name" type="text" placeholder="media" autocomplete="off">
      <span class="form-hint">A letter or digit, then letters, digits, <span class="mono">_ . : -</span></span>
    </div>
    <div class="form-group">
      <label class="form-label" for="ds-owner">Owner</label>
      <select class="select" id="ds-owner"></select>
      <span class="form-hint">Its directory becomes this account's alone, and so do the shares made of it.</span>
    </div>
    ${mountpointChoice('')}
    ${extras}`, async () => {
      const properties = {};
      document.querySelectorAll('[data-new-prop]').forEach(i => { if (i.value.trim()) properties[i.dataset.newProp] = i.value.trim(); });
      const res = await apiJSON('POST', '/api/storage/datasets', {
        parent: document.getElementById('ds-parent').value,
        name: document.getElementById('ds-name').value.trim(),
        mountpoint_mode: chosenMountpoint().mode,
        mountpoint: chosenMountpoint().mountpoint,
        owner: document.getElementById('ds-owner').value,
        properties,
      });
      toast(res.owner ? `Dataset ${res.dataset} created for ${res.owner}.` : `Dataset ${res.dataset} created.`, 'ok');
      if (res.warning) toast(res.warning, 'warn', 8000);
      closeModal('modal-dataset');
      loadDatasets();
    });
  fillOwnerSelect(document.getElementById('ds-owner'));
  const hint = () => {
    const opt = document.getElementById('ds-parent').selectedOptions[0];
    const mp = opt?.dataset.mp || '';
    const name = document.getElementById('ds-name').value.trim() || '<name>';
    document.getElementById('ds-inherit-hint').textContent =
      mp.startsWith('/') ? `${mp.replace(/\/$/, '')}/${name}` : '(not mounted)';
  };
  document.getElementById('ds-parent').addEventListener('change', hint);
  document.getElementById('ds-name').addEventListener('input', hint);
  hint();
}

function sharesList(shares) {
  const all = [...shares.smb.map(s => ['SMB', s]), ...shares.nfs.map(s => ['NFS', s])];
  return all.map(([k, s]) => `<div class="device-row"><span class="badge badge-info">${k}</span>
    <span class="mono">${esc(s.name)}</span><span class="text-muted mono">${esc(s.path)}</span></div>`).join('');
}

function reportShares(res, verb) {
  toast(`${verb}: ${res.dataset} now mounts on ${res.new}.`, res.shares.warnings.length ? 'warn' : 'ok');
  res.shares.warnings.forEach(w => toast(w, 'warn', 8000));
}

async function openMoveDataset(name) {
  let deps;
  try { deps = await apiJSON('GET', `/api/storage/datasets/${name}/dependents`); }
  catch (e) { toast(e.message, 'err'); return; }
  const hasShares = deps.shares.smb.length + deps.shares.nfs.length > 0;
  datasetDialog(`Mountpoint — ${name}`, 'Move', `
    <p class="text-muted">Now: <span class="mono">${esc(deps.mountpoint)}</span> (${esc(deps.mountpoint_source)}).
      Children that inherit it move along.</p>
    ${mountpointChoice(deps.mountpoint.startsWith('/') ? deps.mountpoint : '', name.includes('/'))}
    ${hasShares ? `<div class="form-group"><label class="form-label">Shares under it</label>
      <div class="device-list">${sharesList(deps.shares)}</div>
      <label class="form-label"><input type="checkbox" id="ds-update-shares" checked> Point them at the new place</label></div>` : ''}`,
    async () => {
      const choice = chosenMountpoint();
      const res = await apiJSON('PUT', `/api/storage/datasets/${name}/mountpoint`, {
        mode: choice.mode, mountpoint: choice.mountpoint,
        update_shares: !!document.getElementById('ds-update-shares')?.checked,
      });
      reportShares(res, 'Moved');
      closeModal('modal-dataset');
      loadDatasets();
    });
  if (deps.mountpoint_source === 'local') {
    const path = document.querySelector('input[name="ds-mode"][value="path"]');
    if (path) path.checked = true;
  }
}

async function openRenameDataset(name) {
  let deps;
  try { deps = await apiJSON('GET', `/api/storage/datasets/${name}/dependents`); }
  catch (e) { toast(e.message, 'err'); return; }
  const hasShares = deps.shares.smb.length + deps.shares.nfs.length > 0;
  const moves = deps.mountpoint_source !== 'local';
  datasetDialog(`Rename — ${name}`, 'Rename', `
    <div class="form-group">
      <label class="form-label" for="ds-new-name">New name</label>
      <input class="input mono" id="ds-new-name" type="text" value="${esc(name)}" autocomplete="off">
      <span class="form-hint">Within the same pool. Another parent moves it there.
        ${moves ? 'Its mountpoint is inherited, so it moves with the name.' : 'Its mountpoint is its own and stays where it is.'}</span>
    </div>
    ${deps.policies.length ? `<p class="text-muted">Its snapshot schedules follow the new name.</p>` : ''}
    ${hasShares && moves ? `<div class="form-group"><label class="form-label">Shares under it</label>
      <div class="device-list">${sharesList(deps.shares)}</div>
      <label class="form-label"><input type="checkbox" id="ds-update-shares" checked> Point them at the new place</label></div>` : ''}`,
    async () => {
      const res = await apiJSON('POST', `/api/storage/datasets/${name}/rename`, {
        new_name: document.getElementById('ds-new-name').value.trim(),
        update_shares: !!document.getElementById('ds-update-shares')?.checked,
      });
      reportShares(res, 'Renamed');
      closeModal('modal-dataset');
      loadDatasets();
    });
}

async function openDestroyDataset(name) {
  let deps;
  try { deps = await apiJSON('GET', `/api/storage/datasets/${name}/dependents`); }
  catch (e) { toast(e.message, 'err'); return; }
  const hasShares = deps.shares.smb.length + deps.shares.nfs.length > 0;
  const along = deps.children.length + deps.snapshots.length > 0;
  datasetDialog(`Delete — ${name}`, 'Delete', `
    <div class="alert alert-err"><span>Everything in <span class="mono">${esc(name)}</span> is destroyed. This cannot be undone.</span></div>
    ${along ? `<div class="form-group">
      <label class="form-label"><input type="checkbox" id="ds-recursive">
        Also destroy ${deps.children.length} child dataset(s) and ${deps.snapshots.length} snapshot(s)</label>
      ${deps.children.length ? `<span class="form-hint mono">${deps.children.map(esc).join(', ')}</span>` : ''}</div>` : ''}
    ${hasShares ? `<div class="form-group"><label class="form-label">Shares under it</label>
      <div class="device-list">${sharesList(deps.shares)}</div>
      <label class="form-label"><input type="checkbox" id="ds-remove-shares"> Remove these shares too</label></div>` : ''}
    <div class="form-group">
      <label class="form-label" for="ds-confirm">Type <span class="mono">${esc(name)}</span> to confirm</label>
      <input class="input mono" id="ds-confirm" type="text" autocomplete="off">
    </div>`,
    async () => {
      const q = new URLSearchParams({
        confirm: document.getElementById('ds-confirm').value.trim(),
        recursive: !!document.getElementById('ds-recursive')?.checked,
        remove_shares: !!document.getElementById('ds-remove-shares')?.checked,
      });
      const res = await apiJSON('DELETE', `/api/storage/datasets/${name}?${q}`);
      toast(`Dataset ${res.dataset} deleted.`, res.warnings.length ? 'warn' : 'ok');
      res.warnings.forEach(w => toast(w, 'warn', 8000));
      closeModal('modal-dataset');
      loadDatasets();
    }, true);
}


// ── Exporting and deleting a pool ────────────────────────────────────────────
// Export is undone with Import Pool; Delete is not undone at all. Both list
// what goes with the pool before anything happens.

async function openRemovePool(pool, action) {
  let r;
  try { r = await apiJSON('GET', `/api/storage/pools/${pool}/removal`); }
  catch (e) { toast(e.message, 'err'); return; }
  if (r.protected) { toast(`${pool} cannot be removed: ${r.protected}`, 'err'); return; }
  const destroy = action === 'destroy';
  const hasShares = r.shares.smb.length + r.shares.nfs.length > 0;
  datasetDialog(destroy ? `Delete pool — ${pool}` : `Export — ${pool}`, destroy ? 'Delete pool' : 'Export', `
    ${destroy
      ? `<div class="alert alert-err"><span>The pool and everything on it is destroyed. This cannot be undone.</span></div>`
      : `<div class="alert alert-info"><span>The pool leaves this system and is no longer imported at boot.
          Its data stays on the disks: bring it back with <strong>Import Pool</strong>, here or on another system.</span></div>`}
    <div class="form-group"><label class="form-label">Datasets (${r.datasets.length})</label>
      <span class="form-hint mono">${r.datasets.map(esc).join(', ')}</span></div>
    ${r.encrypted ? `<div class="alert alert-warn"><span>${destroy
      ? 'It is encrypted: its keyslots on this system are removed, and its USB key opens nothing any more.'
      : 'It is encrypted: its keyslots stay on this system, and a copy travels with the pool.'}</span></div>` : ''}
    ${r.policies.length ? `<p class="text-muted">Its snapshot schedules are removed: ${r.policies.map(esc).join(', ')}.</p>` : ''}
    ${hasShares ? `<div class="form-group"><label class="form-label">Shares on it</label>
      <div class="device-list">${sharesList(r.shares)}</div>
      <label class="form-label"><input type="checkbox" id="pool-remove-shares"> Remove these shares too</label></div>` : ''}
    ${destroy ? `<div class="form-group">
      <label class="form-label" for="pool-confirm">Type <span class="mono">${esc(pool)}</span> to confirm</label>
      <input class="input mono" id="pool-confirm" type="text" autocomplete="off"></div>` : ''}`,
    async () => {
      const removeShares = !!document.getElementById('pool-remove-shares')?.checked;
      const res = destroy
        ? await apiJSON('DELETE', `/api/storage/pools/${pool}?${new URLSearchParams({
            confirm: document.getElementById('pool-confirm').value.trim(), remove_shares: removeShares })}`)
        : await apiJSON('POST', `/api/storage/pools/${pool}/export`, { remove_shares: removeShares });
      toast(`Pool ${pool} ${destroy ? 'deleted' : 'exported'}.`, res.warnings.length ? 'warn' : 'ok');
      res.warnings.forEach(w => toast(w, 'warn', 8000));
      closeModal('modal-dataset');
      loadPools();
    }, destroy);
}


// ── Pool detail (Advanced and Expert) ────────────────────────────────────────
// Status and vdev tree of one pool, with what ZFS can do to it after it exists.
// A scrub or resilver in progress refreshes the view every few seconds.

let _detailPool = null;
let _detailTimer = null;
let _ioTimer = null;
let _ioPool = null;
let _ioHistory = [];
let _ioLast = null;

const ROLE_LABELS = {
  data: 'Data vdevs', special: 'Special vdevs (metadata)', dedup: 'Dedup vdevs',
  log: 'Log devices (SLOG)', cache: 'Cache devices (L2ARC)', spare: 'Hot spares',
};

function uiLevel() { return document.body.dataset.ui || 'assisted'; }

function stopPoolDetail() {
  if (_detailTimer) clearTimeout(_detailTimer);
  _detailTimer = null;
  _detailPool = null;
  if (_ioTimer) clearTimeout(_ioTimer);
  _ioTimer = null;
  _ioPool = null;
  _ioHistory = [];
  _ioLast = null;
}

async function openPoolDetail(pool) {
  stopPoolDetail();
  _detailPool = pool;
  const el = document.getElementById('panel-pools');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  await refreshPoolDetail();
}

async function refreshPoolDetail() {
  const pool = _detailPool;
  if (!pool) return;
  const el = document.getElementById('panel-pools');
  let st;
  try {
    st = await apiJSON('GET', `/api/storage/pools/${encodeURIComponent(pool)}/status`);
  } catch (e) {
    el.innerHTML = `<button class="btn btn-ghost btn-sm" id="pool-back">← All pools</button>${errorState(e.message)}`;
    document.getElementById('pool-back').addEventListener('click', backToPools);
    return;
  }
  if (_detailPool !== pool) return;
  el.innerHTML = renderPoolDetail(st);
  bindPoolDetail(st);
  renderPoolIo();
  if (_ioPool !== pool) { _ioPool = pool; poolIoLoop(pool); }
  const busy = st.scan?.state === 'SCANNING' || st.removing ||
    st.groups.some(g => g.vdevs.some(v => hasType(v, ['replacing', 'spare'])));
  if (busy) _detailTimer = setTimeout(refreshPoolDetail, 3000);
}

function hasType(v, types) {
  return types.includes(v.type) || v.children.some(c => hasType(c, types));
}

function backToPools() {
  stopPoolDetail();
  loadPools();
}

function stateBadge(state) {
  const cls = ['ONLINE', 'AVAIL'].includes(state) ? 'ok'
            : ['DEGRADED', 'OFFLINE', 'REMOVED', 'INUSE'].includes(state) ? 'warn'
            : state ? 'err' : 'muted';
  return `<span class="badge badge-${cls}">${esc(state || '—')}</span>`;
}

function scanSummary(scan) {
  if (!scan) return 'No scrub has run on this pool yet.';
  const what = scan.function === 'RESILVER' ? 'Resilver' : 'Scrub';
  if (scan.state === 'SCANNING') {
    return `${what} ${scan.percent ?? 0}% — ${humanSize(scan.done)} of ${humanSize(scan.total)}` +
      (scan.paused ? ' (paused)' : '');
  }
  const when = scan.end_time ? new Date(scan.end_time * 1000).toLocaleString() : '';
  if (scan.state === 'FINISHED') return `${what} finished ${when}, ${scan.errors} error(s).`;
  if (scan.state === 'CANCELED') return `${what} stopped ${when}.`;
  return `${what}: ${scan.state}`;
}

function renderPoolDetail(st) {
  const expert = uiLevel() === 'expert';
  const scanning = st.scan?.state === 'SCANNING' && st.scan.function !== 'RESILVER';
  const pct = st.scan?.state === 'SCANNING' ? (st.scan.percent ?? 0) : null;
  return `
    <div class="d-flex gap-3 mb-4" style="align-items:center;flex-wrap:wrap">
      <button class="btn btn-ghost btn-sm" id="pool-back">← All pools</button>
      <h2 class="pool-name" style="margin:0">${esc(st.name)}</h2>
      ${stateBadge(st.state)}
      ${st.system ? '<span class="badge badge-info">system pool</span>' : ''}
    </div>
    ${st.status ? `<div class="alert alert-warn mb-4"><span><strong>Problem:</strong> ${esc(st.status)}</span></div>` : ''}
    ${st.action ? `<div class="alert alert-info mb-4"><span><strong>What to do:</strong> ${esc(st.action)}</span></div>` : ''}
    <div class="card mb-4">
      <div class="card-header">Scrub and maintenance</div>
      <p class="mt-2">${esc(scanSummary(st.scan))}</p>
      ${pct !== null ? `<div class="progress-wrap mt-2"><div class="progress-bar ok" style="width:${pct}%"></div></div>` : ''}
      <p class="text-muted mt-2">${st.error_count ? `${st.error_count} data error(s): run a scrub, then check the files ZFS lists.` : 'No known data errors.'}</p>
      <div class="d-flex gap-2 mt-3" style="flex-wrap:wrap">
        ${scanning
          ? `<button class="btn btn-secondary btn-sm" data-maint="scrub:pause">${st.scan.paused ? 'Resume scrub' : 'Pause scrub'}</button>
             <button class="btn btn-secondary btn-sm" data-maint="scrub:stop">Stop scrub</button>`
          : `<button class="btn btn-primary btn-sm" data-maint="scrub:start">Start scrub</button>`}
        <button class="btn btn-secondary btn-sm" data-maint="trim:start">Start TRIM</button>
        <button class="btn btn-secondary btn-sm" data-maint="trim:cancel">Stop TRIM</button>
        <button class="btn btn-secondary btn-sm" data-pool-clear>Clear errors</button>
        <button class="btn btn-secondary btn-sm" data-pool-add>Add vdevs</button>
        <button class="btn btn-secondary btn-sm" data-pool-history>History</button>
        ${st.removing ? '<button class="btn btn-secondary btn-sm" data-cancel-remove>Cancel device removal</button>' : ''}
      </div>
      ${expert ? `
      <div class="d-flex gap-2 mt-3" style="flex-wrap:wrap">
        <button class="btn btn-secondary btn-sm" data-pool-props>Pool properties</button>
        <button class="btn btn-secondary btn-sm" data-maint="initialize:start">Initialize free space</button>
        <button class="btn btn-secondary btn-sm" data-maint="initialize:cancel">Stop initializing</button>
        ${st.checkpoint
          ? '<button class="btn btn-secondary btn-sm" data-checkpoint="discard">Discard checkpoint</button>'
          : '<button class="btn btn-secondary btn-sm" data-checkpoint="create">Create checkpoint</button>'}
        ${st.system ? '' : `
          <button class="btn btn-secondary btn-sm" data-pool-task="upgrade">Upgrade features</button>
          <button class="btn btn-secondary btn-sm" data-pool-task="reguid">New GUID</button>`}
      </div>` : ''}
    </div>
    <div class="card mb-4">
      <div class="card-header">Live I/O</div>
      <div id="pool-io" class="mt-2"><span class="text-muted">Measuring…</span></div>
    </div>
    ${st.groups.map(g => `
      <div class="card mb-4">
        <div class="card-header">${esc(ROLE_LABELS[g.role] || g.role)}</div>
        <div class="table-wrap"><table>
          <thead><tr><th>Device</th><th>Type</th><th>State</th><th>Errors R/W/C</th><th>Size</th><th></th></tr></thead>
          <tbody>${g.vdevs.map(v => vdevRows(v, g.role)).join('')}</tbody>
        </table></div>
      </div>`).join('')}`;
}

function vdevRows(v, role) {
  const errors = [v.read_errors, v.write_errors, v.checksum_errors];
  const actions = vdevActions(v, role);
  const row = `
    <tr>
      <td style="padding-left:${12 + 22 * v.depth}px"><span class="mono">${esc(v.path || v.name)}</span></td>
      <td>${esc(v.type)}</td>
      <td>${stateBadge(v.state)}</td>
      <td class="${errors.some(Boolean) ? 'text-err' : 'text-muted'}">${errors.join(' / ')}</td>
      <td>${v.size ? humanSize(v.size) : '—'}</td>
      <td>${actions.length ? `
        <select class="select" data-vdev="${esc(v.path || v.name)}" style="min-width:9rem">
          <option value="">Actions…</option>
          ${actions.map(([value, label]) => `<option value="${value}">${esc(label)}</option>`).join('')}
        </select>` : ''}</td>
    </tr>`;
  return row + v.children.map(c => vdevRows(c, role)).join('');
}

function vdevActions(v, role) {
  const expert = uiLevel() === 'expert';
  const leaf = !v.children.length;
  const list = [];
  if (!leaf) {
    list.push(['clear', 'Clear errors']);
    if (v.depth === 0 && v.type === 'mirror') list.push(['remove', 'Remove from pool']);
    return list;
  }
  if (role === 'spare') return [['remove', 'Remove spare']];
  if (['OFFLINE', 'UNAVAIL', 'REMOVED', 'FAULTED'].includes(v.state)) list.push(['online', 'Bring online']);
  else {
    list.push(['offline', 'Take offline']);
    if (expert) list.push(['offline_temporary', 'Take offline until reboot']);
  }
  if (role !== 'cache') list.push(['replace', 'Replace…']);
  if (role !== 'cache' && (v.depth === 0 || v.parent_type === 'mirror')) list.push(['attach', 'Attach a mirror…']);
  if (['mirror', 'replacing', 'spare'].includes(v.parent_type)) list.push(['detach', 'Detach']);
  list.push(['expand', 'Expand to full size']);
  list.push(['clear', 'Clear errors']);
  if (v.depth === 0) list.push(['remove', 'Remove from pool']);
  return list;
}

function bindPoolDetail(st) {
  const pool = st.name;
  const el = document.getElementById('panel-pools');
  document.getElementById('pool-back').addEventListener('click', backToPools);
  el.querySelectorAll('[data-maint]').forEach(b => b.addEventListener('click', async () => {
    const [task, action] = b.dataset.maint.split(':');
    await poolTask(pool, task, { action });
  }));
  el.querySelector('[data-pool-clear]')?.addEventListener('click', () => poolTask(pool, 'clear', {}));
  el.querySelector('[data-pool-add]')?.addEventListener('click', () => openAddVdevs(pool));
  el.querySelector('[data-pool-history]')?.addEventListener('click', () => openPoolHistory(pool));
  el.querySelector('[data-pool-props]')?.addEventListener('click', () => openPoolProperties(pool));
  el.querySelector('[data-cancel-remove]')?.addEventListener('click', () =>
    poolTask(pool, 'device', { action: 'cancel_remove' }));
  el.querySelectorAll('[data-checkpoint]').forEach(b => b.addEventListener('click', async () => {
    const discard = b.dataset.checkpoint === 'discard';
    if (!await confirm(discard ? 'Discard checkpoint' : 'Create checkpoint', discard
      ? 'The pool can no longer be rewound to the checkpoint.'
      : 'A checkpoint lets the whole pool be rewound to this moment at the next import. While it exists, freed space is not given back and some device operations are refused.',
      discard ? 'Discard' : 'Create')) return;
    await poolTask(pool, 'checkpoint', { discard });
  }));
  el.querySelectorAll('[data-pool-task]').forEach(b => b.addEventListener('click', async () => {
    const task = b.dataset.poolTask;
    const msg = task === 'upgrade'
      ? `Enable every feature this ZFS supports on <strong>${esc(pool)}</strong>? Older ZFS versions and other systems may no longer be able to import it. This cannot be undone.`
      : `Give <strong>${esc(pool)}</strong> a new unique identifier? Only needed when two pools have the same GUID, e.g. after copying disks.`;
    if (!await confirm(task === 'upgrade' ? 'Upgrade pool' : 'New GUID', msg, task === 'upgrade' ? 'Upgrade' : 'Change GUID')) return;
    await poolTask(pool, task, {});
  }));
  el.querySelectorAll('select[data-vdev]').forEach(sel => sel.addEventListener('change', async () => {
    const action = sel.value;
    sel.value = '';
    if (action) await vdevAction(pool, sel.dataset.vdev, action);
  }));
}

async function poolTask(pool, task, body) {
  try {
    await apiJSON('POST', `/api/storage/pools/${encodeURIComponent(pool)}/${task}`, body);
    toast('Done.', 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
  if (_detailPool === pool) {
    if (_detailTimer) clearTimeout(_detailTimer);
    _detailTimer = null;
    refreshPoolDetail();
  }
}

const VDEV_CONFIRM = {
  offline: ['Take offline', 'The pool loses redundancy while the device is offline.'],
  offline_temporary: ['Take offline', 'The device comes back by itself at the next reboot. The pool loses redundancy meanwhile.'],
  detach: ['Detach', 'The device leaves the mirror and can no longer be used to recover data.'],
  remove: ['Remove', 'ZFS first copies the data of a data vdev to the others; this is refused when the pool has RAID-Z vdevs. Cache, log and spare devices hold no data.'],
};

async function vdevAction(pool, device, action) {
  if (action === 'replace' || action === 'attach') { pickDevice(pool, device, action); return; }
  if (action === 'clear') { await poolTask(pool, 'clear', { device }); return; }
  if (VDEV_CONFIRM[action]) {
    const [label, msg] = VDEV_CONFIRM[action];
    if (!await confirm(`${label} ${esc(device)}?`, msg, label)) return;
  }
  await poolTask(pool, 'device', { action, device });
}

async function freeDevices() {
  const devices = (await apiJSON('GET', '/api/storage/devices')).devices;
  return devices.filter(d => (d.type === 'disk' || d.type === 'part') && !d.in_use);
}

async function pickDevice(pool, device, action) {
  let free;
  try { free = await freeDevices(); } catch (e) { toast(e.message, 'err'); return; }
  const replace = action === 'replace';
  const list = free.length ? free.map(d => `
    <label class="device-row">
      <input type="radio" name="pick-device" value="${esc(d.name)}">
      <span class="mono">${esc(d.name)}</span>
      <span class="text-muted">${humanSize(d.size)}${d.model ? ' — ' + esc(d.model) : ''}</span>
    </label>`).join('') : '<span class="text-muted">No free device found.</span>';
  datasetDialog(replace ? `Replace ${device}` : `Attach a mirror to ${device}`,
    replace ? 'Replace' : 'Attach', `
    ${replace ? `<div class="form-group"><label class="form-label">
      <input type="checkbox" id="pick-in-place"> The failed disk was swapped for a new one at the same place</label></div>` : ''}
    <div class="form-group">
      <label class="form-label">New device</label>
      <div class="device-list">${list}</div>
      <span class="form-hint">Everything on it is destroyed. ZFS then copies the data onto it (resilver).</span>
    </div>`, async () => {
      const inPlace = replace && document.getElementById('pick-in-place').checked;
      const chosen = document.querySelector('input[name="pick-device"]:checked')?.value;
      if (!inPlace && !chosen) { toast('Choose a device.', 'err'); return; }
      const target = inPlace ? device : chosen;
      if (!await confirm(replace ? 'Replace device' : 'Attach device',
        `Everything on <span class="mono">${esc(target)}</span> is destroyed.`, replace ? 'Replace' : 'Attach')) return;
      closeModal('modal-dataset');
      await poolTask(pool, 'device', { action, device, new_device: inPlace ? '' : chosen });
    }, true);
}

function vdevGroupSelect(name) {
  return `<select class="select" data-group-for="${esc(name)}" style="width:auto;flex:0 0 auto">
    <option value="">Not used</option>
    <option value="data:1">Data vdev 1</option>
    <option value="data:2">Data vdev 2</option>
    <option value="data:3">Data vdev 3</option>
    <option value="data:4">Data vdev 4</option>
    <option value="special:1">Special (metadata)</option>
    <option value="dedup:1">Dedup table</option>
    <option value="spare:1">Hot spare</option>
  </select>`;
}

const LAYOUT_OPTIONS = `
  <option value="">Single / stripe — no redundancy</option>
  <option value="mirror" selected>Mirror</option>
  <option value="raidz1">RAIDZ1</option>
  <option value="raidz2">RAIDZ2</option>
  <option value="raidz3">RAIDZ3</option>`;

function collectVdevGroups(scope, dataLayout) {
  const groups = new Map();
  scope.querySelectorAll('select[data-group-for]').forEach(sel => {
    if (!sel.value) return;
    if (!groups.has(sel.value)) groups.set(sel.value, []);
    groups.get(sel.value).push(sel.dataset.groupFor);
  });
  return [...groups.entries()].sort().map(([key, devices]) => {
    const cls = key.split(':')[0];
    const type = cls === 'data' ? dataLayout
               : cls === 'spare' ? ''
               : devices.length > 1 ? 'mirror' : '';
    return { class: cls, type, devices };
  });
}

async function openAddVdevs(pool) {
  let free;
  try { free = await freeDevices(); } catch (e) { toast(e.message, 'err'); return; }
  const rows = free.length ? free.map(d => `
    <div class="device-row">
      ${vdevGroupSelect(d.name)}
      <span class="mono">${esc(d.name)}</span>
      <span class="text-muted">${humanSize(d.size)}${d.model ? ' — ' + esc(d.model) : ''}</span>
    </div>`).join('') : '<span class="text-muted">No free device found.</span>';
  datasetDialog(`Add vdevs to ${pool}`, 'Add', `
    <div class="alert alert-warn mb-4"><span>Data vdevs usually cannot be removed again, and never from a pool with RAID-Z vdevs. Match the redundancy of the vdevs already there.</span></div>
    <div class="form-group">
      <label class="form-label" for="add-layout">Layout of the new data vdevs</label>
      <select id="add-layout" class="select">${LAYOUT_OPTIONS}</select>
      <span class="form-hint">Special and dedup vdevs become a mirror when they get two or more devices.</span>
    </div>
    <div class="form-group">
      <label class="form-label">Devices</label>
      <div class="device-list" id="add-vdev-devices">${rows}</div>
    </div>`, async () => {
      const groups = collectVdevGroups(document.getElementById('add-vdev-devices'),
        document.getElementById('add-layout').value);
      if (!groups.length) { toast('Assign at least one device.', 'err'); return; }
      const all = groups.flatMap(g => g.devices);
      if (!await confirm('Add vdevs', `Add to <strong>${esc(pool)}</strong> and erase:<br><span class="mono">${all.map(esc).join('<br>')}</span>`, 'Add and erase')) return;
      closeModal('modal-dataset');
      await poolTask(pool, 'vdevs', { vdevs: groups });
    }, true);
}

async function openPoolHistory(pool) {
  const expert = uiLevel() === 'expert';
  datasetDialog(`History of ${pool}`, 'Close', `
    ${expert ? `<label class="form-label"><input type="checkbox" id="history-internal"> Include internal events</label>` : ''}
    <pre class="mono" id="history-text" style="max-height:24rem;overflow:auto;white-space:pre-wrap;font-size:0.8rem"></pre>`,
    async () => closeModal('modal-dataset'));
  const load = async () => {
    const internal = document.getElementById('history-internal')?.checked || false;
    const box = document.getElementById('history-text');
    box.textContent = 'Loading…';
    try {
      const res = await apiJSON('GET', `/api/storage/pools/${encodeURIComponent(pool)}/history?internal=${internal}`);
      box.textContent = res.lines.slice().reverse().join('\n') || 'Nothing recorded.';
    } catch (e) { box.textContent = e.message; }
  };
  document.getElementById('history-internal')?.addEventListener('change', load);
  load();
}

let poolPropCatalogue = null;

async function openPoolProperties(pool) {
  try {
    if (!poolPropCatalogue) poolPropCatalogue = (await apiJSON('GET', '/api/storage/pool-properties')).properties;
    const current = (await apiJSON('GET', `/api/storage/pools/${encodeURIComponent(pool)}/properties`)).properties;
    datasetDialog(`Properties of ${pool}`, 'Apply', `<div id="pool-props">${
      Object.entries(poolPropCatalogue).map(([name, spec]) => propertyRow(name, spec, current[name])).join('')
    }</div>`, async () => {
      const changed = {};
      document.querySelectorAll('#pool-props [data-prop]').forEach(el => {
        if (el.value !== el.dataset.was) changed[el.dataset.prop] = el.value;
      });
      if (!Object.keys(changed).length) { closeModal('modal-dataset'); return; }
      const res = await apiJSON('PUT', `/api/storage/pools/${encodeURIComponent(pool)}/properties`, changed);
      if (res.failed?.length) toast(`Not applied: ${res.failed.map(f => f.property).join(', ')}`, 'err');
      else { toast(`Applied: ${res.applied.join(', ')}`, 'ok'); closeModal('modal-dataset'); }
    });
  } catch (e) { toast(e.message, 'err'); }
}


// ── Dataset administration (Advanced and Expert) ─────────────────────────────

function apiPath(name) {
  return name.split('/').map(encodeURIComponent).join('/');
}

function datasetMenu(r) {
  const expert = uiLevel() === 'expert';
  const fs = (r.type || 'filesystem') === 'filesystem';
  const options = [];
  if (fs && r.mountpoint !== 'none' && r.mountpoint !== 'legacy') {
    options.push(r.mounted === false ? ['mount', 'Mount'] : ['unmount', 'Unmount']);
  }
  if (fs) options.push(['quotas', 'User and group quotas…']);
  options.push(['snapshots', 'Show snapshots']);
  if (expert && r.origin) options.push(['promote', `Promote (clone of ${r.origin})`]);
  if (expert) options.push(['userprops', 'User properties…']);
  if (expert) options.push(['delegations', 'Delegated permissions…']);
  return `<select class="select" data-ds-menu="${esc(r.name)}" style="width:auto">
    <option value="">More…</option>
    ${options.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}
  </select>`;
}

async function datasetAction(name, action) {
  if (action === 'snapshots') { _snapFilter = name; switchTab('snapshots'); return; }
  if (action === 'quotas') { openQuotas(name); return; }
  if (action === 'userprops') { openUserProperties(name); return; }
  if (action === 'delegations') { openDelegations(name); return; }
  if (action === 'unmount' && !await confirm('Unmount', `Unmount <span class="mono">${esc(name)}</span>? Shares on it stop working until it is mounted again.`, 'Unmount')) return;
  if (action === 'promote' && !await confirm('Promote clone', `<span class="mono">${esc(name)}</span> stops depending on its origin snapshot; the origin becomes a clone of it instead.`, 'Promote')) return;
  try {
    await apiJSON('POST', `/api/storage/datasets/${apiPath(name)}/${action}`, {});
    toast('Done.', 'ok');
    loadDatasets();
  } catch (e) { toast(e.message, 'err'); }
}

async function openCreateVolume() {
  const parents = _datasetRows.filter(r => !r.protected && (r.type || 'filesystem') === 'filesystem');
  if (!parents.length) { toast('Create a pool first.', 'err'); return; }
  datasetDialog('Create Volume', 'Create', `
    <div class="form-group"><label class="form-label" for="vol-parent">Inside</label>
      <select id="vol-parent" class="select">${parents.map(r => `<option>${esc(r.name)}</option>`).join('')}</select></div>
    <div class="form-group"><label class="form-label" for="vol-name">Name</label>
      <input id="vol-name" class="input" type="text" autocomplete="off" placeholder="vm-disk"></div>
    <div class="form-group"><label class="form-label" for="vol-size">Size</label>
      <input id="vol-size" class="input" type="text" autocomplete="off" placeholder="40G">
      <span class="form-hint">A number with K, M, G or T. The volume appears as a block device, e.g. for virtual machines or iSCSI.</span></div>
    <div class="form-group"><label class="form-label"><input type="checkbox" id="vol-sparse">
      Thin provisioned — space is taken only when written</label></div>
    <div class="form-group"><label class="form-label" for="vol-block">Block size</label>
      <select id="vol-block" class="select"><option value="">Default (16K)</option>
        ${['4K', '8K', '16K', '32K', '64K', '128K'].map(b => `<option>${b}</option>`).join('')}</select></div>`,
    async () => {
      const body = {
        parent: document.getElementById('vol-parent').value,
        name: document.getElementById('vol-name').value.trim(),
        size: document.getElementById('vol-size').value.trim(),
        sparse: document.getElementById('vol-sparse').checked,
        blocksize: document.getElementById('vol-block').value,
      };
      const res = await apiJSON('POST', '/api/storage/volumes', body);
      toast(`Volume ${res.volume} created.`, 'ok');
      closeModal('modal-dataset');
      loadDatasets();
    });
}

async function openQuotas(name) {
  datasetDialog(`Quotas on ${name}`, 'Set quota', `
    <div class="form-group"><label class="form-label" for="quota-kind">For</label>
      <select id="quota-kind" class="select"><option value="user">Users</option><option value="group">Groups</option></select></div>
    <div id="quota-list" class="mb-4"></div>
    <div class="form-group"><label class="form-label" for="quota-name">Name</label>
      <input id="quota-name" class="input" type="text" autocomplete="off" placeholder="sarah"></div>
    <div class="form-group"><label class="form-label" for="quota-size">Space limit</label>
      <input id="quota-size" class="input" type="text" autocomplete="off" placeholder="50G or none"></div>
    <div class="form-group expert-only"><label class="form-label" for="quota-objects">File limit (number of files, optional)</label>
      <input id="quota-objects" class="input" type="text" autocomplete="off" placeholder="none"></div>`,
    async () => {
      const body = { kind: document.getElementById('quota-kind').value,
                     name: document.getElementById('quota-name').value.trim() };
      const size = document.getElementById('quota-size').value.trim();
      const objects = document.getElementById('quota-objects').value.trim();
      if (size) body.quota = size;
      if (objects) body.objquota = objects;
      await apiJSON('PUT', `/api/storage/datasets/${apiPath(name)}/quotas`, body);
      toast('Quota set.', 'ok');
      load();
    });
  const load = async () => {
    const kind = document.getElementById('quota-kind').value;
    const box = document.getElementById('quota-list');
    box.innerHTML = '<div class="spinner"></div>';
    try {
      const res = await apiJSON('GET', `/api/storage/datasets/${apiPath(name)}/quotas?kind=${kind}`);
      box.innerHTML = res.entries.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Name</th><th>Used</th><th>Limit</th><th>Files</th></tr></thead>
        <tbody>${res.entries.map(e => `<tr><td>${esc(e.name)}</td><td>${humanSize(e.used)}</td>
          <td>${e.quota ? humanSize(e.quota) : '<span class="text-muted">none</span>'}</td>
          <td>${e.objused ?? '—'}${e.objquota ? ' / ' + e.objquota : ''}</td></tr>`).join('')}</tbody>
      </table></div>` : '<p class="text-muted">Nothing written here yet.</p>';
    } catch (e) { box.innerHTML = errorState(e.message); }
  };
  document.getElementById('quota-kind').addEventListener('change', load);
  load();
}

async function openUserProperties(name) {
  datasetDialog(`User properties of ${name}`, 'Save', `
    <div id="uprop-list" class="mb-4"></div>
    <div class="form-group"><label class="form-label" for="uprop-name">Property</label>
      <input id="uprop-name" class="input mono" type="text" autocomplete="off" placeholder="com.example:note">
      <span class="form-hint">Lowercase, with a colon. Children inherit it like any property.</span></div>
    <div class="form-group"><label class="form-label" for="uprop-value">Value</label>
      <input id="uprop-value" class="input" type="text" autocomplete="off"></div>`,
    async () => {
      const prop = document.getElementById('uprop-name').value.trim();
      await apiJSON('PUT', `/api/storage/datasets/${apiPath(name)}/user-properties`,
        { [prop]: document.getElementById('uprop-value').value });
      toast(`${prop} saved.`, 'ok');
      load();
    });
  const load = async () => {
    const box = document.getElementById('uprop-list');
    try {
      const res = await apiJSON('GET', `/api/storage/datasets/${apiPath(name)}/user-properties`);
      const entries = Object.entries(res.properties);
      box.innerHTML = entries.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Property</th><th>Value</th><th>Source</th><th></th></tr></thead>
        <tbody>${entries.map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td>${esc(v.value)}</td>
          <td class="text-muted">${esc(v.source)}</td>
          <td>${v.source === 'local' ? `<button type="button" class="btn btn-ghost btn-sm" data-uprop-reset="${esc(k)}">Reset</button>` : ''}</td></tr>`).join('')}</tbody>
      </table></div>` : '<p class="text-muted">No user properties.</p>';
      box.querySelectorAll('[data-uprop-reset]').forEach(b => b.addEventListener('click', async () => {
        try {
          await apiJSON('POST', `/api/storage/datasets/${apiPath(name)}/inherit`, { property: b.dataset.upropReset });
          load();
        } catch (e) { toast(e.message, 'err'); }
      }));
    } catch (e) { box.innerHTML = errorState(e.message); }
  };
  load();
}


// ── Snapshot actions (Advanced and Expert) ───────────────────────────────────

async function snapshotTask(name, task, body) {
  return apiJSON('POST', `/api/storage/snapshots/${apiPath(name)}/${task}`, body);
}

async function destroySnapshots(names) {
  if (!names.length) return;
  const recursiveBox = names.length === 1
    ? `<br><br><label><input type="checkbox" id="snap-destroy-rec"> Also the snapshot of the same name on every child dataset</label>` : '';
  if (!await confirm('Destroy snapshots', `Destroy permanently:<br><span class="mono">${names.map(esc).join('<br>')}</span>${recursiveBox}`, 'Destroy')) return;
  const recursive = document.getElementById('snap-destroy-rec')?.checked || false;
  let done = 0;
  for (const name of names) {
    try {
      await apiJSON('DELETE', `/api/storage/snapshots/${apiPath(name)}${recursive ? '?recursive=true' : ''}`);
      done++;
    } catch (e) {
      toast(`${name}: ${e.message}`, 'err');
      break;
    }
  }
  if (done) toast(`${done} snapshot(s) destroyed.`, 'ok');
  loadSnapshots();
}

async function snapshotAction(name, action) {
  const [dataset, snap] = name.split('@');
  try {
    if (action === 'destroy') { await destroySnapshots([name]); return; }
    if (action === 'rollback') {
      const me = _snapRows.find(r => r.name === name);
      const newer = _snapRows.filter(r => r.dataset === dataset && typeof r.creation === 'number'
        && me && r.creation > me.creation).map(r => r.name);
      const msg = `<span class="mono">${esc(dataset)}</span> goes back to how it was at
        <span class="mono">${esc(snap)}</span>. Everything written since is lost.` +
        (newer.length ? `<br><br>These newer snapshots are destroyed too:<br><span class="mono">${newer.map(esc).join('<br>')}</span>` : '');
      if (!await confirm('Roll back', msg, 'Roll back')) return;
      await snapshotTask(name, 'rollback', { destroy_newer: newer.length > 0 });
      toast(`${dataset} rolled back.`, 'ok');
      loadSnapshots();
      return;
    }
    if (action === 'clone') {
      datasetDialog(`Clone ${name}`, 'Clone', `
        <div class="form-group"><label class="form-label" for="clone-target">New dataset</label>
          <input id="clone-target" class="input mono" type="text" autocomplete="off" value="${esc(dataset)}-clone">
          <span class="form-hint">A writable copy that starts as the snapshot. It lives in the same pool and keeps the snapshot alive.</span></div>`,
        async () => {
          const target = document.getElementById('clone-target').value.trim();
          await snapshotTask(name, 'clone', { target });
          toast(`Clone ${target} created.`, 'ok');
          closeModal('modal-dataset');
          loadSnapshots();
        });
      return;
    }
    if (action === 'rename' || action === 'bookmark') {
      const rename = action === 'rename';
      datasetDialog(rename ? `Rename ${name}` : `Bookmark ${name}`, rename ? 'Rename' : 'Create', `
        <div class="form-group"><label class="form-label" for="snap-new-name">${rename ? 'New name' : 'Bookmark name'}</label>
          <input id="snap-new-name" class="input mono" type="text" autocomplete="off" value="${esc(snap)}">
          <span class="form-hint">${rename ? `Becomes ${esc(dataset)}@<em>name</em>.` : 'A bookmark keeps this point as the base of incremental sends after the snapshot is destroyed.'}</span></div>`,
        async () => {
          const value = document.getElementById('snap-new-name').value.trim();
          await snapshotTask(name, action, rename ? { new_name: value } : { name: value });
          toast(rename ? 'Renamed.' : 'Bookmark created.', 'ok');
          closeModal('modal-dataset');
          loadSnapshots();
        });
      return;
    }
    if (action === 'holds') { openHolds(name); return; }
    if (action === 'diff') { openDiff(name); return; }
  } catch (e) { toast(e.message, 'err'); }
}

async function openHolds(name) {
  datasetDialog(`Holds on ${name}`, 'Add hold', `
    <p class="text-muted mb-4">A snapshot with a hold cannot be destroyed until every hold is released.</p>
    <div id="holds-list" class="mb-4"></div>
    <div class="form-group"><label class="form-label" for="hold-tag">New hold</label>
      <input id="hold-tag" class="input mono" type="text" autocomplete="off" placeholder="keep"></div>`,
    async () => {
      await snapshotTask(name, 'hold', { tag: document.getElementById('hold-tag').value.trim() });
      document.getElementById('hold-tag').value = '';
      load();
      loadSnapshots();
    });
  const load = async () => {
    const box = document.getElementById('holds-list');
    try {
      const res = await apiJSON('GET', `/api/storage/snapshots/${apiPath(name)}/holds`);
      box.innerHTML = res.holds.length ? res.holds.map(h => `
        <div class="device-row"><span class="mono">${esc(h.tag)}</span>
          <span class="text-muted">${snapTime(h.time)}</span>
          <button type="button" class="btn btn-ghost btn-sm" data-release="${esc(h.tag)}">Release</button></div>`).join('')
        : '<p class="text-muted">No holds.</p>';
      box.querySelectorAll('[data-release]').forEach(b => b.addEventListener('click', async () => {
        try { await snapshotTask(name, 'release', { tag: b.dataset.release }); load(); loadSnapshots(); }
        catch (e) { toast(e.message, 'err'); }
      }));
    } catch (e) { box.innerHTML = errorState(e.message); }
  };
  load();
}

async function openDiff(name) {
  const [dataset] = name.split('@');
  const me = _snapRows.find(r => r.name === name);
  const later = _snapRows.filter(r => r.dataset === dataset && me && typeof r.creation === 'number'
    && r.creation > me.creation);
  datasetDialog(`Changes since ${name}`, 'Close', `
    <div class="form-group"><label class="form-label" for="diff-to">Compare with</label>
      <select id="diff-to" class="select"><option value="">The dataset as it is now</option>
        ${later.map(r => `<option value="${esc(r.name)}">${esc(r.name)}</option>`).join('')}</select></div>
    <div id="diff-result"></div>`, async () => closeModal('modal-dataset'));
  const load = async () => {
    const to = document.getElementById('diff-to').value;
    const box = document.getElementById('diff-result');
    box.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
    try {
      const res = await apiJSON('GET', `/api/storage/snapshots/${apiPath(name)}/diff${to ? '?to=' + encodeURIComponent(to) : ''}`);
      const badge = { created: 'ok', removed: 'err', modified: 'warn', renamed: 'info' };
      box.innerHTML = res.changes.length ? `
        <p class="text-muted mb-2">${res.total} change(s)${res.truncated ? `, the first ${res.changes.length} shown` : ''}.</p>
        <div class="table-wrap" style="max-height:24rem;overflow:auto"><table>
          <thead><tr><th>Change</th><th>Type</th><th>Path</th></tr></thead>
          <tbody>${res.changes.map(c => `<tr>
            <td><span class="badge badge-${badge[c.change] || 'muted'}">${esc(c.change)}</span></td>
            <td class="text-muted">${esc(c.type)}</td>
            <td class="mono">${esc(c.path)}${c.new_path ? ' → ' + esc(c.new_path) : ''}</td></tr>`).join('')}</tbody>
        </table></div>` : '<p class="text-muted">Nothing changed.</p>';
    } catch (e) { box.innerHTML = errorState(e.message); }
  };
  document.getElementById('diff-to').addEventListener('change', load);
  load();
}



// ── Local replication (Advanced and Expert) ──────────────────────────────────
// zfs send | zfs receive between pools of this NAS, or into and out of a
// stream file. Transfers run as jobs on the NAS; this tab polls their progress.

let _replTimer = null;
let _replSnaps = [];
let _replMarks = [];
const _replRate = {};

const SEND_OPTIONS = [
  ['replicate', 'Replication stream (-R)', 'Children, properties, snapshots and clones', 'advanced', false],
  ['raw', 'Raw (-w)', 'Encrypted data stays encrypted; the key is not needed on the way', 'advanced', false],
  ['compressed', 'Compressed (-c)', 'Send blocks as they are compressed on disk', 'advanced', true],
  ['large_blocks', 'Large blocks (-L)', 'Keep records bigger than 128 KiB as they are', 'advanced', true],
  ['embed', 'Embedded data (-e)', '', 'expert', false],
  ['props', 'Properties (-p)', 'Include the dataset properties', 'expert', false],
  ['holds', 'Holds (-h)', '', 'expert', false],
  ['skip_missing', 'Skip missing snapshots (-s)', 'With -R only', 'expert', false],
];

function stopReplicationPoll() {
  if (_replTimer) clearTimeout(_replTimer);
  _replTimer = null;
}

async function loadReplication() {
  stopReplicationPoll();
  const el = document.getElementById('panel-replication');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    const [snaps, marks, ds] = await Promise.all([
      apiJSON('GET', '/api/storage/snapshots'),
      apiJSON('GET', '/api/storage/bookmarks').catch(() => ({ bookmarks: [] })),
      apiJSON('GET', '/api/storage/datasets'),
    ]);
    _replSnaps = snaps.snapshots || [];
    _replMarks = marks.bookmarks || [];
    el.innerHTML = renderReplicationForm(ds.datasets || []);
    bindReplication();
    refreshTransfers();
  } catch (e) {
    el.innerHTML = errorState(e.message);
  }
}

function renderReplicationForm(datasets) {
  const expert = uiLevel() === 'expert';
  const targets = datasets.filter(d => !d.protected).map(d => d.name);
  return `
    <div class="card mb-4">
      <div class="card-header">Send a snapshot</div>
      <div class="d-flex flex-col gap-3 mt-3">
      <div class="form-group"><label class="form-label" for="repl-snap">Snapshot</label>
        <select id="repl-snap" class="select">${_replSnaps.map(s =>
          `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join('') || '<option value="">No snapshots</option>'}</select></div>
      <div class="form-group"><label class="form-label" for="repl-base">Incremental from</label>
        <select id="repl-base" class="select"></select>
        <span class="form-hint">Only what changed since the base is sent; the target must already hold the base.</span></div>
      <div class="form-group"><label class="form-label"><input type="checkbox" id="repl-inter"> Include the snapshots in between (-I)</label></div>
      <div class="form-group"><label class="form-label">Send to</label>
        <div class="device-list">
          <label class="device-row"><input type="radio" name="repl-mode" value="dataset" checked> <span>A dataset on this NAS</span></label>
          <label class="device-row"><input type="radio" name="repl-mode" value="file"> <span>A stream file</span></label>
        </div></div>
      <div class="form-group" id="repl-target-group"><label class="form-label" for="repl-target">Target dataset</label>
        <input id="repl-target" class="input mono" type="text" list="repl-targets" autocomplete="off" placeholder="backup/documents">
        <datalist id="repl-targets">${targets.map(t => `<option value="${esc(t)}">`).join('')}</datalist>
        <span class="form-hint">A dataset that does not exist yet is created by the transfer. Not on the system's pool.</span></div>
      <div class="form-group" id="repl-file-group" hidden><label class="form-label" for="repl-file">Stream file</label>
        <input id="repl-file" class="input mono" type="text" autocomplete="off" placeholder="/tank/backup/documents.zfs">
        <span class="form-hint">On a dataset, not a system directory. An existing file is never overwritten.</span></div>
      <div class="form-group"><label class="form-label">Stream options</label>
        <div class="device-list">${SEND_OPTIONS.filter(o => o[3] === 'advanced' || expert).map(([k, label, hint, , on]) => `
          <label class="device-row"><input type="checkbox" data-send="${k}"${on ? ' checked' : ''}>
            <span>${esc(label)}</span><span class="text-muted">${esc(hint)}</span></label>`).join('')}</div></div>
      <div class="form-group" id="repl-recv-group"><label class="form-label">Receiving side</label>
        <div class="device-list">
          <label class="device-row"><input type="checkbox" id="repl-nomount" checked> <span>Do not mount what is received (-u)</span></label>
          <label class="device-row"><input type="checkbox" id="repl-resumable" checked> <span>Resumable if interrupted (-s)</span></label>
          <label class="device-row"><input type="checkbox" id="repl-nomp" checked> <span>Do not copy the mountpoint</span>
            <span class="text-muted">so the copy never mounts over the original</span></label>
          ${expert ? `<label class="device-row"><input type="checkbox" id="repl-force"> <span>Roll the target back if it changed (-F)</span>
            <span class="text-muted">destroys snapshots the source does not have</span></label>` : ''}
        </div></div>
      <div class="d-flex gap-2" style="align-items:center;flex-wrap:wrap">
        <button class="btn btn-secondary btn-sm" id="repl-estimate">Estimate size</button>
        <span class="text-muted" id="repl-estimate-out"></span>
        <button class="btn btn-primary btn-sm" id="repl-start">Start</button>
      </div>
      </div>
    </div>
    <div class="card mb-4">
      <div class="card-header">Transfers</div>
      <div id="repl-jobs" class="mt-3"></div>
    </div>
    <div class="card mb-4" id="repl-resumable-card" hidden>
      <div class="card-header">Interrupted receives</div>
      <p class="text-muted mt-2">A resumable transfer that stopped half-way keeps what it received; resume it or throw it away.</p>
      <div id="repl-resumable-list"></div>
    </div>
    <div class="card mb-4">
      <div class="card-header">Receive from a stream file</div>
      <div class="d-flex flex-col gap-3 mt-3">
      <div class="form-group"><label class="form-label" for="repl-in-file">Stream file</label>
        <input id="repl-in-file" class="input mono" type="text" autocomplete="off" placeholder="/tank/backup/documents.zfs"></div>
      <div class="form-group"><label class="form-label" for="repl-in-target">Target dataset</label>
        <input id="repl-in-target" class="input mono" type="text" list="repl-targets" autocomplete="off"></div>
      <div><button class="btn btn-primary btn-sm" id="repl-receive">Receive</button></div>
      </div>
    </div>`;
}

function fillReplBase() {
  const snap = document.getElementById('repl-snap').value;
  const me = _replSnaps.find(s => s.name === snap);
  const ds = snap.split('@')[0];
  const older = _replSnaps.filter(s => s.dataset === ds && s.name !== snap && me &&
    typeof s.creation === 'number' && s.creation < me.creation);
  const marks = _replMarks.filter(m => m.dataset === ds);
  document.getElementById('repl-base').innerHTML = '<option value="">Nothing — send everything</option>' +
    older.map(s => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join('') +
    marks.map(m => `<option value="${esc(m.name)}">${esc(m.name)} (bookmark)</option>`).join('');
}

function replicationRequest() {
  const body = { snapshot: document.getElementById('repl-snap').value };
  const base = document.getElementById('repl-base').value;
  if (base) {
    body.from = base;
    body.intermediates = document.getElementById('repl-inter').checked && !base.includes('#');
  }
  document.querySelectorAll('[data-send]').forEach(c => { body[c.dataset.send] = c.checked; });
  const toFile = document.querySelector('input[name="repl-mode"]:checked').value === 'file';
  if (toFile) {
    body.file = document.getElementById('repl-file').value.trim();
    return ['send-to-file', body];
  }
  body.target = document.getElementById('repl-target').value.trim();
  body.no_mount = document.getElementById('repl-nomount').checked;
  body.resumable = document.getElementById('repl-resumable').checked;
  body.exclude_mountpoint = document.getElementById('repl-nomp').checked;
  body.force = document.getElementById('repl-force')?.checked || false;
  return ['replicate', body];
}

function bindReplication() {
  document.getElementById('repl-snap').addEventListener('change', fillReplBase);
  fillReplBase();
  document.querySelectorAll('input[name="repl-mode"]').forEach(r => r.addEventListener('change', () => {
    const toFile = document.querySelector('input[name="repl-mode"]:checked').value === 'file';
    document.getElementById('repl-target-group').hidden = toFile;
    document.getElementById('repl-recv-group').hidden = toFile;
    document.getElementById('repl-file-group').hidden = !toFile;
  }));
  document.getElementById('repl-estimate').addEventListener('click', async () => {
    const out = document.getElementById('repl-estimate-out');
    out.textContent = 'Estimating…';
    try {
      const [, body] = replicationRequest();
      const res = await apiJSON('POST', '/api/storage/replication/estimate', body);
      out.textContent = res.size ? `About ${humanSize(res.size)}` : 'Unknown size';
    } catch (e) { out.textContent = e.message; }
  });
  document.getElementById('repl-start').addEventListener('click', async () => {
    const [kind, body] = replicationRequest();
    if (body.force && !await confirm('Roll back the target', `With -F, <span class="mono">${esc(body.target)}</span> loses the snapshots and changes the source does not have.`, 'Start')) return;
    try {
      await apiJSON('POST', `/api/storage/replication/${kind}`, body);
      toast('Transfer started.', 'ok');
      refreshTransfers();
    } catch (e) { toast(e.message, 'err'); }
  });
  document.getElementById('repl-receive').addEventListener('click', async () => {
    const body = { file: document.getElementById('repl-in-file').value.trim(),
                   target: document.getElementById('repl-in-target').value.trim() };
    try {
      await apiJSON('POST', '/api/storage/replication/receive-from-file', body);
      toast('Receive started.', 'ok');
      refreshTransfers();
    } catch (e) { toast(e.message, 'err'); }
  });
}

async function refreshTransfers() {
  stopReplicationPoll();
  if (_activeTab !== 'replication') return;
  const box = document.getElementById('repl-jobs');
  if (!box) return;
  let jobs = [], resumable = [];
  try {
    [jobs, resumable] = await Promise.all([
      apiJSON('GET', '/api/storage/replication/jobs').then(r => r.jobs),
      apiJSON('GET', '/api/storage/replication/resumable').then(r => r.datasets).catch(() => []),
    ]);
  } catch (e) { box.innerHTML = errorState(e.message); return; }
  if (_activeTab !== 'replication') return;
  const badge = { running: 'info', done: 'ok', failed: 'err', cancelled: 'warn' };
  box.innerHTML = jobs.length ? `<div class="table-wrap"><table>
    <thead><tr><th>From</th><th>To</th><th>State</th><th>Progress</th><th></th></tr></thead>
    <tbody>${jobs.map(j => {
      const pct = j.total ? Math.min(100, Math.round(100 * j.done / j.total)) : null;
      const prev = _replRate[j.id];
      const now = Date.now() / 1000;
      const rate = prev && j.state === 'running' && now > prev.t ? (j.done - prev.done) / (now - prev.t) : null;
      _replRate[j.id] = { t: now, done: j.done };
      return `<tr>
        <td class="mono">${esc(j.source)}${j.from ? `<br><span class="text-muted">since ${esc(j.from)}</span>` : ''}</td>
        <td class="mono">${esc(j.target)}</td>
        <td><span class="badge badge-${badge[j.state] || 'muted'}">${esc(j.state)}</span>
          ${j.error ? `<div class="text-err mt-1" style="max-width:28rem;white-space:pre-wrap">${esc(j.error.split('Stderr:').pop().trim())}</div>` : ''}</td>
        <td style="min-width:12rem">${pct !== null ? `<div class="progress-wrap"><div class="progress-bar ${j.state === 'failed' ? 'err' : 'ok'}" style="width:${pct}%"></div></div>` : ''}
          <span class="text-muted">${j.done ? humanSize(j.done) : ''}${j.total ? ' / ' + humanSize(j.total) : ''}${rate > 0 ? ` · ${humanSize(rate)}/s` : ''}</span></td>
        <td>${j.state === 'running' ? `<button class="btn btn-ghost btn-sm" data-cancel-job="${j.id}">Cancel</button>` : ''}</td>
      </tr>`; }).join('')}</tbody></table></div>` : '<p class="text-muted">No transfers since the WebUI started.</p>';
  box.querySelectorAll('[data-cancel-job]').forEach(b => b.addEventListener('click', async () => {
    if (!await confirm('Cancel transfer', 'Stop this transfer? A resumable one can be resumed later from where it stopped.', 'Cancel transfer')) return;
    try { await apiJSON('POST', `/api/storage/replication/jobs/${b.dataset.cancelJob}/cancel`, {}); }
    catch (e) { toast(e.message, 'err'); }
    refreshTransfers();
  }));

  const card = document.getElementById('repl-resumable-card');
  card.hidden = !resumable.length;
  document.getElementById('repl-resumable-list').innerHTML = resumable.map(d => `
    <div class="device-row"><span class="mono">${esc(d)}</span>
      <button class="btn btn-secondary btn-sm" data-resume="${esc(d)}">Resume</button>
      <button class="btn btn-ghost btn-sm" data-discard="${esc(d)}">Discard</button></div>`).join('');
  card.querySelectorAll('[data-resume]').forEach(b => b.addEventListener('click', async () => {
    try { await apiJSON('POST', '/api/storage/replication/resume', { target: b.dataset.resume }); toast('Resumed.', 'ok'); }
    catch (e) { toast(e.message, 'err'); }
    refreshTransfers();
  }));
  card.querySelectorAll('[data-discard]').forEach(b => b.addEventListener('click', async () => {
    if (!await confirm('Discard partial receive', `Throw away what was received into <span class="mono">${esc(b.dataset.discard)}</span> so far?`, 'Discard')) return;
    try { await apiJSON('POST', '/api/storage/replication/abort', { target: b.dataset.discard }); toast('Discarded.', 'ok'); }
    catch (e) { toast(e.message, 'err'); }
    refreshTransfers();
  }));

  if (jobs.some(j => j.state === 'running')) _replTimer = setTimeout(refreshTransfers, 2000);
}



// ── Live I/O, ARC, events, delegated permissions ─────────────────────────────

async function poolIoLoop(pool) {
  _ioTimer = null;
  if (_detailPool !== pool || _ioPool !== pool) return;
  try {
    const res = await apiJSON('GET', `/api/storage/pools/${encodeURIComponent(pool)}/iostat`);
    if (_detailPool !== pool) return;
    _ioLast = res.devices;
    const total = res.devices[0];
    if (total) {
      _ioHistory.push([total.read_bytes, total.write_bytes]);
      if (_ioHistory.length > 60) _ioHistory.shift();
    }
    renderPoolIo();
  } catch (e) {
    const box = document.getElementById('pool-io');
    if (box) box.innerHTML = errorState(e.message);
  }
  if (_detailPool === pool && _ioPool === pool) _ioTimer = setTimeout(() => poolIoLoop(pool), 2000);
}

function ioSparkline() {
  if (_ioHistory.length < 2) return '';
  const w = 600, h = 80, step = w / 59, n = _ioHistory.length;
  const top = Math.max(1, ..._ioHistory.flat());
  const line = idx => _ioHistory.map((v, i) =>
    `${(w - (n - 1 - i) * step).toFixed(1)},${(h - 4 - (h - 8) * v[idx] / top).toFixed(1)}`).join(' ');
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:80px">
      <polyline points="${line(0)}" fill="none" stroke="var(--info)" stroke-width="2" vector-effect="non-scaling-stroke"/>
      <polyline points="${line(1)}" fill="none" stroke="var(--warn)" stroke-width="2" vector-effect="non-scaling-stroke"/>
    </svg>
    <div class="text-muted" style="font-size:0.8rem"><span style="color:var(--info)">●</span> read
      <span style="color:var(--warn)">●</span> write · peak ${humanSize(top)}/s</div>`;
}

function renderPoolIo() {
  const box = document.getElementById('pool-io');
  if (!box || !_ioLast) return;
  const rate = n => n ? `${humanSize(n)}/s` : '0';
  box.innerHTML = ioSparkline() + `<div class="table-wrap mt-2"><table>
    <thead><tr><th>Device</th><th>Read ops/s</th><th>Write ops/s</th><th>Read</th><th>Write</th></tr></thead>
    <tbody>${_ioLast.map((d, i) => `<tr${i === 0 ? ' style="font-weight:600"' : ''}>
      <td class="mono">${esc(d.name)}</td><td>${d.read_ops}</td><td>${d.write_ops}</td>
      <td>${rate(d.read_bytes)}</td><td>${rate(d.write_bytes)}</td></tr>`).join('')}</tbody>
  </table></div>`;
}

async function loadArc() {
  const box = document.getElementById('arc-card');
  if (!box) return;
  try {
    const a = await apiJSON('GET', '/api/storage/arc');
    const pct = a.max ? Math.round(100 * a.size / a.max) : 0;
    box.innerHTML = `<div class="pool-card mb-4">
      <div class="pool-header"><div><div class="pool-name">ARC — read cache in RAM</div>
        <div class="pool-detail">${a.hit_ratio !== null ? `${a.hit_ratio}% of reads served from RAM since boot` : ''}</div></div></div>
      <div class="progress-wrap" title="${pct}% of the maximum"><div class="progress-bar ok" style="width:${pct}%"></div></div>
      <div class="pool-stats">
        <div class="pool-stat-item"><span class="pool-stat-label">Size</span><span class="pool-stat-value">${humanSize(a.size)}</span></div>
        <div class="pool-stat-item"><span class="pool-stat-label">Target</span><span class="pool-stat-value">${humanSize(a.target)}</span></div>
        <div class="pool-stat-item"><span class="pool-stat-label">Maximum</span><span class="pool-stat-value">${humanSize(a.max)}</span></div>
        <div class="pool-stat-item"><span class="pool-stat-label">L2ARC</span><span class="pool-stat-value">${a.l2_size ? humanSize(a.l2_size) + (a.l2_hit_ratio !== null ? ` · ${a.l2_hit_ratio}% hits` : '') : 'none'}</span></div>
      </div></div>`;
  } catch { box.innerHTML = ''; }
}

let _events = [];

async function loadEvents() {
  const el = document.getElementById('panel-events');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    _events = (await apiJSON('GET', '/api/storage/events')).events;
    renderEvents(el);
  } catch (e) { el.innerHTML = errorState(e.message); }
}

function renderEvents(el) {
  const q = (document.getElementById('events-filter')?.value || '').toLowerCase();
  const errorsOnly = document.getElementById('events-errors')?.checked || false;
  const hideHistory = document.getElementById('events-history')?.checked ?? true;
  const shown = _events.filter(e => (!errorsOnly || e.error) && (!hideHistory || e.short !== 'history_event') &&
    (!q || `${e.class} ${Object.values(e.fields).join(' ')}`.toLowerCase().includes(q)));
  el.innerHTML = `
    <div class="section-header">
      <span class="d-flex gap-3" style="align-items:center">
        <input class="input" id="events-filter" type="search" placeholder="Filter" value="${esc(q)}" style="max-width:18rem">
        <label style="white-space:nowrap"><input type="checkbox" id="events-errors"${errorsOnly ? ' checked' : ''}> Errors only</label>
        <label style="white-space:nowrap"><input type="checkbox" id="events-history"${hideHistory ? ' checked' : ''}> Hide command history</label>
      </span>
      <span><button class="btn btn-secondary btn-sm" id="events-reload">Reload</button>
        <button class="btn btn-danger btn-sm" id="events-clear">Clear all</button></span>
    </div>
    <p class="text-muted mb-4">What the ZFS kernel module reported since boot: pool changes, scrubs, device errors (newest first, ${shown.length} of ${_events.length}).</p>
    ${shown.length ? shown.map(e => `
      <details class="card mb-2" style="padding:10px 16px">
        <summary><span class="badge badge-${e.error ? 'err' : 'muted'}">${esc(e.short)}</span>
          <span class="mono text-muted">${esc(e.time)}</span> ${e.pool ? `<span class="mono">${esc(e.pool)}</span>` : ''}</summary>
        <pre class="mono mt-2" style="white-space:pre-wrap;font-size:0.8rem">${esc(Object.entries(e.fields)
          .filter(([k]) => !['class', 'time', 'eid', 'version'].includes(k))
          .map(([k, v]) => `${k} = ${v}`).join('\n'))}</pre>
      </details>`).join('') : emptyState('No events.')}`;
  const filter = el.querySelector('#events-filter');
  filter.addEventListener('input', () => {
    renderEvents(el);
    const again = el.querySelector('#events-filter');
    again.focus();
    again.setSelectionRange(again.value.length, again.value.length);
  });
  el.querySelector('#events-errors').addEventListener('change', () => renderEvents(el));
  el.querySelector('#events-history').addEventListener('change', () => renderEvents(el));
  el.querySelector('#events-reload').addEventListener('click', loadEvents);
  el.querySelector('#events-clear').addEventListener('click', async () => {
    if (!await confirm('Clear events', 'Empty the ZFS event log for every pool?', 'Clear')) return;
    try { await apiJSON('POST', '/api/storage/events/clear', {}); loadEvents(); }
    catch (err) { toast(err.message, 'err'); }
  });
}

const DELEGABLE = ['create', 'destroy', 'mount', 'snapshot', 'rollback', 'clone', 'promote', 'rename',
  'send', 'send:raw', 'receive', 'hold', 'release', 'bookmark', 'diff', 'share', 'load-key',
  'change-key', 'userprop', 'userquota', 'groupquota', 'allow'];

async function openDelegations(name) {
  datasetDialog(`Delegated permissions on ${name}`, 'Grant', `
    <p class="text-muted mb-4">Let ordinary users run zfs commands on this dataset (zfs allow). The pool's <span class="mono">delegation</span> property must be on.</p>
    <div id="deleg-list" class="mb-4"></div>
    <div class="form-group"><label class="form-label" for="deleg-who">Grant to</label>
      <select id="deleg-who" class="select">
        <option value="user">Users</option><option value="group">Groups</option>
        <option value="everyone">Everyone</option><option value="create">Whoever creates a dataset below</option>
        <option value="set">Define a permission set</option></select></div>
    <div class="form-group" id="deleg-names-group"><label class="form-label" for="deleg-names">Names, comma separated</label>
      <input id="deleg-names" class="input" type="text" autocomplete="off" placeholder="sarah,backup"></div>
    <div class="form-group" id="deleg-set-group" hidden><label class="form-label" for="deleg-set">Set name</label>
      <input id="deleg-set" class="input mono" type="text" autocomplete="off" value="@"></div>
    <div class="form-group" id="deleg-scope-group"><label class="form-label" for="deleg-scope">Applies to</label>
      <select id="deleg-scope" class="select"><option value="both">This dataset and the ones below</option>
        <option value="local">Only this dataset</option><option value="descendent">Only the ones below</option></select></div>
    <div class="form-group"><label class="form-label">Permissions</label>
      <div class="d-flex gap-3" style="flex-wrap:wrap">${DELEGABLE.map(p =>
        `<label class="mono" style="font-size:0.85rem"><input type="checkbox" data-perm="${p}"> ${p}</label>`).join('')}</div></div>
    <div class="form-group"><label class="form-label" for="deleg-extra">More permissions, properties or @sets</label>
      <input id="deleg-extra" class="input mono" type="text" autocomplete="off" placeholder="compression,@backup"></div>`,
    async () => {
      const perms = [...document.querySelectorAll('[data-perm]:checked')].map(c => c.dataset.perm)
        .concat(document.getElementById('deleg-extra').value.split(',').map(x => x.trim()).filter(Boolean));
      await apiJSON('POST', `/api/storage/delegations/${apiPath(name)}/grant`, delegationRequest(perms));
      toast('Permissions granted.', 'ok');
      load();
    });
  const whoSel = document.getElementById('deleg-who');
  const syncWho = () => {
    const who = whoSel.value;
    document.getElementById('deleg-names-group').hidden = !['user', 'group'].includes(who);
    document.getElementById('deleg-set-group').hidden = who !== 'set';
    document.getElementById('deleg-scope-group').hidden = ['create', 'set'].includes(who);
  };
  whoSel.addEventListener('change', syncWho);
  syncWho();
  const load = async () => {
    const box = document.getElementById('deleg-list');
    try {
      const res = await apiJSON('GET', `/api/storage/delegations/${apiPath(name)}/list`);
      box.innerHTML = res.delegations.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Section</th><th>Who</th><th>Permissions</th><th></th></tr></thead>
        <tbody>${res.delegations.map((d, i) => `<tr><td class="text-muted">${esc(d.section)}</td>
          <td>${esc(d.who === 'set' ? d.name : d.who === 'create' ? 'creator' : d.who === 'everyone' ? 'everyone' : `${d.who} ${d.name}`)}</td>
          <td class="mono">${esc(d.perms.join(', '))}</td>
          <td><button type="button" class="btn btn-ghost btn-sm" data-revoke="${i}">Revoke</button></td></tr>`).join('')}</tbody>
      </table></div>` : '<p class="text-muted">Nothing is delegated here.</p>';
      box.querySelectorAll('[data-revoke]').forEach(b => b.addEventListener('click', async () => {
        const d = res.delegations[Number(b.dataset.revoke)];
        const body = { who: d.who, scope: d.scope, perms: d.perms };
        if (d.who === 'user' || d.who === 'group') body.names = d.name;
        if (d.who === 'set') body.set = d.name;
        try {
          await apiJSON('POST', `/api/storage/delegations/${apiPath(name)}/revoke`, body);
          load();
        } catch (e) { toast(e.message, 'err'); }
      }));
    } catch (e) { box.innerHTML = errorState(e.message); }
  };
  load();
}

function delegationRequest(perms) {
  return {
    who: document.getElementById('deleg-who').value,
    names: document.getElementById('deleg-names').value,
    set: document.getElementById('deleg-set').value.trim(),
    scope: document.getElementById('deleg-scope').value,
    perms,
  };
}
