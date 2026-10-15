'use strict';

document.addEventListener('DOMContentLoaded', async () => {
  if (!initShared()) return;

  await loadUsers();

  document.getElementById('btn-add-user').addEventListener('click', () => {
    document.getElementById('form-user').reset();
    openModal('modal-user');
  });

  bindModalClose('modal-user');
  bindModalClose('modal-passwd');

  document.getElementById('user-list').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-user]');
    if (!btn) return;
    if (btn.dataset.action === 'password') changePassword(btn.dataset.user);
    if (btn.dataset.action === 'remove') deleteUser(btn.dataset.user);
    if (btn.dataset.action === 'admin') setAdmin(btn.dataset.user, btn.dataset.admin === 'true');
  });

  document.getElementById('form-passwd').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn      = document.getElementById('btn-passwd-submit');
    const username = document.getElementById('p-username').value;
    const password = document.getElementById('p-password').value;
    const confirm2 = document.getElementById('p-password2').value;

    if (password.length < 8) { toast('Password must be at least 8 characters.', 'warn'); return; }
    if (password !== confirm2) { toast('The passwords do not match.', 'warn'); return; }

    setLoading(btn, true, 'Saving…');
    try {
      await apiJSON('POST', `/api/users/${encodeURIComponent(username)}/password`, { password });
      toast(`Password updated for "${username}".`, 'ok');
      closeModal('modal-passwd');
    } catch (e) {
      toast(`Error: ${e.message}`, 'err');
    } finally {
      setLoading(btn, false);
    }
  });

  document.getElementById('form-user').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn      = document.getElementById('btn-user-submit');
    const username = document.getElementById('f-username').value.trim();
    const password = document.getElementById('f-password').value;
    const confirm2 = document.getElementById('f-password2').value;
    const groups   = Array.from(document.querySelectorAll('.f-group:checked')).map(c => c.value);
    const shell    = document.getElementById('f-shell').value.trim() || '/bin/bash';
    const uid      = document.getElementById('f-uid')?.value.trim() || '';
    const gid      = document.getElementById('f-gid')?.value.trim() || '';

    if (!username) { toast('Username is required.', 'warn'); return; }
    if (!/^[a-z_][a-z0-9_-]{0,31}$/.test(username)) {
      toast('Invalid username (lowercase letters, digits, underscore, hyphen).', 'warn');
      return;
    }
    if (password.length < 8) { toast('Password must be at least 8 characters.', 'warn'); return; }
    if (password !== confirm2) { toast('The passwords do not match.', 'warn'); return; }

    const body = { username, password, groups, shell };
    if (uid) body.uid = parseInt(uid);
    if (gid) body.gid = parseInt(gid);

    setLoading(btn, true, 'Creating…');
    try {
      await apiJSON('POST', '/api/users/', body);
      toast(`User "${username}" created.`, 'ok');
      closeModal('modal-user');
      await loadUsers();
    } catch (e) {
      toast(`Error: ${e.message}`, 'err');
    } finally {
      setLoading(btn, false);
    }
  });
});

async function loadUsers() {
  const el = document.getElementById('user-list');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';
  try {
    const d = await apiJSON('GET', '/api/users/');
    if (!d.users?.length) { el.innerHTML = emptyUsers(); return; }
    el.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Username</th>
            <th>Groups</th>
            <th>Shell</th>
            <th class="col-actions"></th>
          </tr></thead>
          <tbody>
            ${d.users.map(renderUser).join('')}
          </tbody>
        </table>
      </div>`;
  } catch (e) {
    el.innerHTML = `<div class="alert alert-err">${esc(e.message)}</div>`;
  }
}

function renderUser(u) {
  const groups = (u.groups || []).map(g =>
    `<span class="badge badge-muted" style="margin-right:3px">${esc(g)}</span>`
  ).join('');
  const isSelf = u.username === Auth.getUser();
  return `
    <tr>
      <td class="font-bold">${esc(u.username)} ${isSelf ? '<span class="badge badge-info" style="margin-left:4px">you</span>' : ''}${u.admin ? '<span class="badge badge-ok" style="margin-left:4px">admin</span>' : ''}</td>
      <td>${groups || '<span class="text-muted">—</span>'}</td>
      <td class="mono muted">${esc(u.shell || '/bin/bash')}</td>
      <td class="col-actions">
        <button class="btn btn-secondary btn-sm" data-action="password" data-user="${esc(u.username)}">Password</button>
        ${u.admin
          ? (!isSelf ? `<button class="btn btn-secondary btn-sm advanced-only" data-action="admin" data-admin="false" data-user="${esc(u.username)}">Remove administrator</button>` : '')
          : `<button class="btn btn-secondary btn-sm advanced-only" data-action="admin" data-admin="true" data-user="${esc(u.username)}">Make administrator</button>`}
        ${!isSelf ? `<button class="btn btn-danger btn-sm advanced-only" data-action="remove" data-user="${esc(u.username)}">Remove</button>` : ''}
      </td>
    </tr>`;
}

function changePassword(username) {
  document.getElementById('form-passwd').reset();
  document.getElementById('p-username').value = username;
  openModal('modal-passwd');
}

async function setAdmin(username, admin) {
  const title = admin ? 'Make Administrator' : 'Remove Administrator';
  const text = admin
    ? `Make <strong>${esc(username)}</strong> an administrator? They will be able to manage this system and use sudo.`
    : `Take the administrator role from <strong>${esc(username)}</strong>? They keep their account, files and shares.`;
  if (!await confirm(title, text, admin ? 'Make administrator' : 'Remove role')) return;
  try {
    await apiJSON('POST', `/api/users/${encodeURIComponent(username)}/admin`, { admin });
    toast(admin ? `"${username}" is now an administrator.` : `"${username}" is no longer an administrator.`, 'ok');
    await loadUsers();
  } catch (e) {
    toast(`Error: ${e.message}`, 'err');
  }
}

async function deleteUser(username) {
  if (!await confirm('Remove User', `Remove user <strong>${esc(username)}</strong>? This cannot be undone.`, 'Remove')) return;
  try {
    await apiJSON('DELETE', `/api/users/${encodeURIComponent(username)}`);
    toast(`User "${username}" removed.`, 'ok');
    await loadUsers();
  } catch (e) {
    toast(`Error: ${e.message}`, 'err');
  }
}

function emptyUsers() {
  return `<div class="empty-state">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/></svg>
    <h3>No users found</h3>
    <p>Add users to manage system access.</p>
  </div>`;
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
