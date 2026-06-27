'use strict';

document.addEventListener('DOMContentLoaded', async () => {
  if (!initShared()) return;

  await loadInterfaces();

  document.getElementById('form-ip')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn   = document.getElementById('btn-ip-submit');
    const iface = document.getElementById('f-ip-iface').value;
    const ip    = document.getElementById('f-ip-addr').value.trim();
    const cidr  = parseInt(document.getElementById('f-ip-cidr').value.trim());
    const gw    = document.getElementById('f-ip-gw').value.trim();
    const dns   = document.getElementById('f-ip-dns').value.trim();

    if (!ip || !cidr || !gw) { toast('IP address, prefix length, and gateway are required.', 'warn'); return; }

    setLoading(btn, true, 'Applying…');
    try {
      await apiJSON('POST', `/api/network/interfaces/${encodeURIComponent(iface)}/ip`, {
        ip, cidr, gateway: gw, dns: dns.split(/[\s,]+/).filter(Boolean),
      });
      toast('Network configuration applied.', 'ok');
      closeModal('modal-ip');
      await loadInterfaces();
    } catch (e) {
      toast(`Error: ${e.message}`, 'err');
    } finally {
      setLoading(btn, false);
    }
  });

  bindModalClose('modal-ip');
});

async function loadInterfaces() {
  const el = document.getElementById('iface-list');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';

  try {
    const d = await apiJSON('GET', '/api/network/interfaces');
    const ifaces = Array.isArray(d.interfaces) ? d.interfaces : Object.values(d.interfaces || {});
    if (!ifaces.length) { el.innerHTML = emptyIface(); return; }
    el.innerHTML = ifaces.map(renderIface).join('');
  } catch (e) {
    el.innerHTML = `<div class="alert alert-err">${esc(e.message)}</div>`;
  }
}

function renderIface(i) {
  const name   = i.name || i.ifname || i.interface || Object.keys(i)[0] || 'unknown';
  const addr   = (i.addr_info || i.inet || []).map(a => `${a.local || a.address}/${a.prefixlen || a.prefix || ''}`).filter(Boolean);
  const mac    = i.address || i.mac || '';
  const isUp   = (i.operstate || i.flags || '').toLowerCase().includes('up');
  const isLo   = name === 'lo' || name.startsWith('lo');

  return `
    <div class="card mb-4">
      <div class="d-flex items-center justify-between mb-4">
        <div class="d-flex items-center gap-3">
          <div class="card-icon ${isUp ? 'text-ok' : 'text-muted'}" style="background:${isUp ? 'var(--ok-bg)' : 'var(--in-bg)'}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="7" width="20" height="15" rx="2"/><path d="M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2"/><line x1="12" y1="12" x2="12" y2="16"/></svg>
          </div>
          <div>
            <div class="font-bold">${esc(name)}</div>
            ${mac ? `<div class="text-sm text-muted font-mono">${esc(mac)}</div>` : ''}
          </div>
        </div>
        <div class="d-flex items-center gap-3">
          <span class="badge ${isUp ? 'badge-ok' : 'badge-muted'}">${isUp ? 'UP' : 'DOWN'}</span>
          ${!isLo ? `
            <button class="btn btn-secondary btn-sm advanced-only"
              onclick="openEditIP('${esc(name)}', '${esc(addr[0] || '')}')">
              Edit IP
            </button>` : ''}
        </div>
      </div>
      ${addr.length ? `
        <div class="d-flex flex-col gap-2">
          ${addr.map(a => `<div class="d-flex items-center gap-2"><span class="badge badge-info font-mono">${esc(a)}</span></div>`).join('')}
        </div>` : '<div class="text-sm text-muted">No IPv4 address assigned</div>'}
    </div>`;
}

function openEditIP(name, currentAddr) {
  const [ip, cidr] = (currentAddr || '/').split('/');
  document.getElementById('f-ip-iface').value = name;
  document.getElementById('modal-ip-title').textContent = `Configure IP — ${name}`;
  document.getElementById('f-ip-addr').value  = ip || '';
  document.getElementById('f-ip-cidr').value  = cidr || '24';
  document.getElementById('f-ip-gw').value    = '';
  document.getElementById('f-ip-dns').value   = '';
  openModal('modal-ip');
}

function emptyIface() {
  return `<div class="empty-state">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="7" width="20" height="15" rx="2"/><path d="M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2"/></svg>
    <h3>No interfaces found</h3>
  </div>`;
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
