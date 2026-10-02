'use strict';

let _sharesTab = 'smb';

document.addEventListener('DOMContentLoaded', async () => {
  if (!initShared()) return;

  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  document.getElementById('btn-add-share').addEventListener('click', () => {
    const type = _sharesTab === 'smb' ? 'smb' : 'nfs';
    document.getElementById('f-share-type').value = type;
    document.querySelectorAll('.smb-only').forEach(el => el.hidden = type !== 'smb');
    document.querySelectorAll('.nfs-only').forEach(el => el.hidden = type !== 'nfs');
    document.getElementById('modal-title-type').textContent = type.toUpperCase();
    document.getElementById('form-share').reset();
    fillOwnerSelect(document.getElementById('f-share-owner'),
      'Automatic — whoever the dataset was given to, or you');
    openModal('modal-share');
  });

  bindModalClose('modal-share');

  document.getElementById('panel-smb').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-share]');
    if (btn) deleteSmb(btn.dataset.share);
  });
  document.getElementById('panel-nfs').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-share]');
    if (btn) deleteNfs(btn.dataset.share);
  });

  document.getElementById('form-share').addEventListener('submit', async (e) => {
    e.preventDefault();
    const type = document.getElementById('f-share-type').value;
    const btn  = document.getElementById('btn-share-submit');
    setLoading(btn, true, 'Creating…');
    try {
      if (type === 'smb') await createSmb();
      else await createNfs();
      closeModal('modal-share');
      switchTab(_sharesTab);
    } catch { /* errors handled inside */ }
    finally { setLoading(btn, false); }
  });

  switchTab('smb');
});

function switchTab(tab) {
  _sharesTab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `panel-${tab}`));
  if (tab === 'smb') loadSmb();
  else loadNfs();
}

async function loadSmb() {
  const el = document.getElementById('panel-smb');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    const d = await apiJSON('GET', '/api/shares/smb');
    if (!d.shares?.length) { el.innerHTML = emptyShares('SMB'); return; }
    el.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Path</th><th>Users</th><th>Read Only</th><th class="col-actions"></th></tr></thead>
          <tbody>
            ${d.shares.map(s => `
              <tr>
                <td class="font-bold">${esc(s.name)}</td>
                <td class="mono muted">${esc(s.path)}</td>
                <td class="muted">${esc((s.valid_users || []).join(', '))}</td>
                <td>${s.read_only ? `<span class="badge badge-muted">Read Only</span>` : `<span class="badge badge-ok">Read/Write</span>`}</td>
                <td class="col-actions">
                  <button class="btn btn-danger btn-sm" data-share="${esc(s.name)}">Remove</button>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  } catch (e) { el.innerHTML = errState(e.message); }
}

async function loadNfs() {
  const el = document.getElementById('panel-nfs');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    const d = await apiJSON('GET', '/api/shares/nfs');
    if (!d.shares?.length) { el.innerHTML = emptyShares('NFS'); return; }
    el.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Path</th><th>Allowed Hosts</th><th>Options</th><th class="col-actions"></th></tr></thead>
          <tbody>
            ${d.shares.map(s => `
              <tr>
                <td class="font-bold">${esc(s.name)}</td>
                <td class="mono muted">${esc(s.path)}</td>
                <td class="muted">${esc(s.allowed_hosts)}</td>
                <td class="mono text-sm muted">${esc(s.options || '')}</td>
                <td class="col-actions">
                  <button class="btn btn-danger btn-sm" data-share="${esc(s.name)}">Remove</button>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  } catch (e) { el.innerHTML = errState(e.message); }
}

async function createSmb() {
  const name  = document.getElementById('f-share-name').value.trim();
  const path  = document.getElementById('f-share-path').value.trim();
  const ro    = document.getElementById('f-share-ro').checked;
  const owner = document.getElementById('f-share-owner').value;
  if (!name || !path) { toast('Name and path are required.', 'warn'); throw new Error('validation'); }
  try {
    const res = await apiJSON('POST', '/api/shares/smb', { name, path, read_only: ro, owner });
    toast(`SMB share "${name}" created for ${res.owner}.`, 'ok');
    if (res.warning) toast(res.warning, 'warn', 8000);
  } catch (e) {
    toast(`Error: ${e.message}`, 'err');
    throw e;
  }
}

async function createNfs() {
  const name   = document.getElementById('f-share-name').value.trim();
  const path   = document.getElementById('f-share-path').value.trim();
  const hosts  = document.getElementById('f-nfs-hosts').value.trim();
  const opts   = document.getElementById('f-nfs-options').value.trim() || 'rw,sync,no_subtree_check';
  const owner  = document.getElementById('f-share-owner').value;
  if (!name || !path || !hosts) { toast('Name, path, and allowed hosts are required.', 'warn'); throw new Error('validation'); }
  try {
    const res = await apiJSON('POST', '/api/shares/nfs', { name, path, allowed_hosts: hosts, options: opts, owner });
    toast(`NFS share "${name}" created for ${res.owner}.`, 'ok');
    if (res.warning) toast(res.warning, 'warn', 8000);
  } catch (e) {
    toast(`Error: ${e.message}`, 'err');
    throw e;
  }
}

async function deleteSmb(name) {
  if (!await confirm('Remove SMB Share', `Remove share <strong>${esc(name)}</strong>? The directory itself is not deleted.`, 'Remove')) return;
  try {
    await apiJSON('DELETE', `/api/shares/smb/${encodeURIComponent(name)}`);
    toast(`Share "${name}" removed.`, 'ok');
    loadSmb();
  } catch (e) { toast(`Error: ${e.message}`, 'err'); }
}

async function deleteNfs(name) {
  if (!await confirm('Remove NFS Share', `Remove NFS export <strong>${esc(name)}</strong>?`, 'Remove')) return;
  try {
    await apiJSON('DELETE', `/api/shares/nfs/${encodeURIComponent(name)}`);
    toast(`NFS share "${name}" removed.`, 'ok');
    loadNfs();
  } catch (e) { toast(`Error: ${e.message}`, 'err'); }
}

function emptyShares(type) {
  return `<div class="empty-state">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>
    <h3>No ${type} shares</h3>
    <p>Click "Add Share" to create your first ${type} share.</p>
  </div>`;
}

function errState(msg) {
  return `<div class="alert alert-err">${esc(msg)}</div>`;
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
