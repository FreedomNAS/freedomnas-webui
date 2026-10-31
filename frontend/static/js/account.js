'use strict';

document.addEventListener('DOMContentLoaded', async () => {
  if (!initShared({ limited: true })) return;

  let me;
  try {
    me = await apiJSON('GET', '/api/account/me');
  } catch (e) {
    toast(`Could not load your account: ${e.message}`, 'err');
    return;
  }
  renderAccount(me);
  renderShares(me);
  renderApps(me);
  renderCertificate(me);

  document.getElementById('form-password').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = document.getElementById('btn-password');
    const current = document.getElementById('f-current').value;
    const fresh = document.getElementById('f-new').value;
    if (fresh !== document.getElementById('f-new2').value) {
      toast('The new passwords differ.', 'warn');
      return;
    }
    setLoading(btn, true, 'Changing…');
    try {
      await apiJSON('POST', '/api/account/password',
        { username: me.username, password: current, new_password: fresh });
      Auth.set(me.username, fresh);
      e.target.reset();
      toast('Password changed.', 'ok');
    } catch (err) {
      toast(`Error: ${err.message}`, 'err');
    } finally {
      setLoading(btn, false);
    }
  });
});

function renderAccount(me) {
  document.getElementById('me-name').textContent = me.username;
  document.getElementById('me-groups').textContent = (me.groups || []).join(', ') || '—';
  document.getElementById('me-nas').textContent = me.hostname || location.hostname;
  const edition = document.getElementById('sidebar-edition');
  if (edition) edition.textContent = me.edition ? `${me.edition} edition` : '';
  if (me.admin) {
    document.getElementById('nav-dashboard').hidden = false;
    document.getElementById('account-intro').textContent =
      'Your account, your shares and the apps of this NAS. The rest of the WebUI is in the menu.';
  }
}

function renderShares(me) {
  const el = document.getElementById('me-shares');
  const host = location.hostname;
  const smb = me.shares?.smb || [];
  const nfs = me.shares?.nfs || [];
  if (!smb.length && !nfs.length) {
    el.innerHTML = `<div class="card text-muted text-sm">No share is open to your account yet. Ask an administrator of this NAS for one.</div>`;
    return;
  }
  const rows = [
    ...smb.map(s => `
      <tr>
        <td class="font-bold">${esc(s.name)}</td>
        <td>SMB</td>
        <td class="mono">\\\\${esc(host)}\\${esc(s.name)}</td>
        <td>${s.read_only ? `<span class="badge badge-muted">Read Only</span>` : `<span class="badge badge-ok">Read/Write</span>`}</td>
      </tr>`),
    ...nfs.map(s => `
      <tr>
        <td class="font-bold">${esc(s.name)}</td>
        <td>NFS</td>
        <td class="mono">${esc(host)}:${esc(s.path)}</td>
        <td><span class="badge badge-ok">Read/Write</span></td>
      </tr>`),
  ];
  el.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead><tr><th>Name</th><th>Kind</th><th>Address</th><th>Access</th></tr></thead>
        <tbody>${rows.join('')}</tbody>
      </table>
    </div>
    <span class="form-hint" style="display:block;margin-top:8px">
      SMB: on Windows type the address in File Explorer, on macOS in Finder → Go → Connect to Server
      (as smb://${esc(host)}/name), and sign in with your user name and password.
    </span>`;
}

function linkHref(link) {
  const base = link.url || `${link.scheme}://${location.hostname}:${link.port}/`;
  return base + String(link.path || '/').replace(/^\//, '');
}

function renderApps(me) {
  const el = document.getElementById('me-apps');
  const links = me.links || [];
  if (!links.length) {
    el.innerHTML = `<div class="card text-muted text-sm">No app is running on this NAS.</div>`;
    return;
  }
  el.innerHTML = `
    <div class="card d-flex gap-3" style="flex-wrap:wrap">
      ${links.map(l => `
        <a class="btn btn-secondary" href="${esc(linkHref(l))}" target="_blank" rel="noopener">${esc(l.title || l.name)}</a>`).join('')}
    </div>`;
}

function renderCertificate(me) {
  if (!me.ca_fingerprint) return;
  document.getElementById('me-ca').textContent = me.ca_fingerprint;
  document.getElementById('me-certificate').hidden = false;
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
