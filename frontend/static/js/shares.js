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
    fillMembers(document.getElementById('f-share-members'), []);
    showKind();
    openModal('modal-share');
  });

  bindModalClose('modal-share');
  bindModalClose('modal-members');
  document.querySelectorAll('input[name="f-share-kind"]').forEach(r => r.addEventListener('change', showKind));
  document.getElementById('f-share-everyone').addEventListener('change', showKind);
  document.getElementById('f-share-name').addEventListener('input', () => {
    const g = document.getElementById('f-share-group');
    if (!g.dataset.touched) g.value = groupFromName(document.getElementById('f-share-name').value);
  });
  document.getElementById('f-share-group').addEventListener('input', (e) => { e.target.dataset.touched = '1'; });
  document.getElementById('form-members').addEventListener('submit', saveMembers);

  document.getElementById('panel-smb').addEventListener('click', (e) => {
    const members = e.target.closest('[data-members]');
    if (members) { editMembers(members.dataset.members); return; }
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

let _smbShares = [];

function shareKind() {
  return document.getElementById('f-share-type').value === 'smb'
    && document.getElementById('f-kind-group').checked ? 'group' : 'owner';
}

function showKind() {
  const group = shareKind() === 'group';
  document.getElementById('group-fields').hidden = !group;
  document.getElementById('owner-field').hidden = group;
  document.getElementById('f-share-members').hidden = document.getElementById('f-share-everyone').checked;
  const g = document.getElementById('f-share-group');
  delete g.dataset.touched;
  g.value = groupFromName(document.getElementById('f-share-name').value);
}

function groupFromName(name) {
  return name.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^[^a-z_]+/, '').replace(/-+$/, '').slice(0, 31);
}

async function fillMembers(el, checked) {
  el.replaceChildren();
  let names = [];
  try { names = (await apiJSON('GET', '/api/users/')).users.map(u => u.username); }
  catch (e) { el.textContent = e.message; return; }
  names.forEach(n => {
    const row = document.createElement('label');
    row.className = 'checkbox-group';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.value = n;
    box.id = `${el.id}-${n}`;
    box.checked = checked.includes(n);
    const text = document.createElement('label');
    text.htmlFor = box.id;
    text.textContent = n;
    row.append(box, text);
    el.append(row);
  });
}

function checkedMembers(el) {
  return [...el.querySelectorAll('input[type="checkbox"]:checked')].map(b => b.value);
}

async function editMembers(name) {
  const share = _smbShares.find(s => s.name === name);
  if (!share) return;
  document.getElementById('members-share').textContent = share.name;
  document.getElementById('form-members').dataset.share = share.name;
  document.getElementById('m-everyone').checked = !!share.everyone;
  await fillMembers(document.getElementById('m-members'), share.members || []);
  openModal('modal-members');
}

async function saveMembers(e) {
  e.preventDefault();
  const name = e.target.dataset.share;
  const btn = document.getElementById('btn-members-submit');
  setLoading(btn, true, 'Saving…');
  try {
    await apiJSON('PUT', `/api/shares/smb/${encodeURIComponent(name)}/members`, {
      members: checkedMembers(document.getElementById('m-members')),
      everyone: document.getElementById('m-everyone').checked,
    });
    toast(`Members of "${name}" saved.`, 'ok');
    closeModal('modal-members');
    loadSmb();
  } catch (err) { toast(`Error: ${err.message}`, 'err'); }
  finally { setLoading(btn, false); }
}

async function loadSmb() {
  const el = document.getElementById('panel-smb');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    const d = await apiJSON('GET', '/api/shares/smb');
    _smbShares = d.shares || [];
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
                <td class="muted">${s.group
                  ? `<span class="badge badge-muted">Group ${esc(s.group)}</span> ${esc((s.members || []).join(', '))}${s.everyone ? ' (and every new account)' : ''}`
                  : esc((s.valid_users || []).join(', '))}</td>
                <td>${s.read_only ? `<span class="badge badge-muted">Read Only</span>` : `<span class="badge badge-ok">Read/Write</span>`}</td>
                <td class="col-actions">
                  ${s.group ? `<button class="btn btn-secondary btn-sm" data-members="${esc(s.name)}">Members</button>` : ''}
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
  if (shareKind() === 'group') {
    const everyone = document.getElementById('f-share-everyone').checked;
    try {
      const res = await apiJSON('POST', '/api/shares/smb', {
        name, path, read_only: ro, group: document.getElementById('f-share-group').value.trim(), everyone,
        members: everyone ? [] : checkedMembers(document.getElementById('f-share-members')),
      });
      toast(`SMB share "${name}" created for the group ${res.group}.`, 'ok');
    } catch (e) {
      toast(`Error: ${e.message}`, 'err');
      throw e;
    }
    return;
  }
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
  const opts   = document.getElementById('f-nfs-options').value.trim();
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
