'use strict';

const NET_MSG = 'fnas_netmsg';
let saved = {};
let entries = {};
let present = [];
let rollbackSeconds = 90;
let pendingTimer = null;

document.addEventListener('DOMContentLoaded', async () => {
  await confirmFromNewAddress();
  if (!initShared()) return;

  const msg = sessionStorage.getItem(NET_MSG);
  if (msg) { sessionStorage.removeItem(NET_MSG); const [type, text] = JSON.parse(msg); toast(text, type, 8000); }

  await loadInterfaces();

  document.getElementById('iface-list').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-iface]');
    if (btn) openEditIP(btn.dataset.iface, btn.dataset.addr);
    const del = e.target.closest('[data-remove]');
    if (del) removeVirtual(del.dataset.remove);
  });

  document.getElementById('btn-add-virtual').addEventListener('click', openVirtual);
  document.getElementById('f-v-type').addEventListener('change', syncVirtual);
  document.getElementById('v-ports').addEventListener('change', syncVirtual);
  document.getElementById('form-virtual').addEventListener('submit', createVirtual);
  bindModalClose('modal-virtual');

  document.getElementById('f-ip-method').addEventListener('change', syncForm);
  document.getElementById('f-ip6-method').addEventListener('change', syncForm);
  document.getElementById('form-ip').addEventListener('submit', applyConfig);
  document.getElementById('btn-net-keep').addEventListener('click', keep);
  document.getElementById('btn-net-revert').addEventListener('click', revert);
  document.getElementById('btn-banner-keep').addEventListener('click', keep);
  document.getElementById('btn-banner-revert').addEventListener('click', revert);

  bindModalClose('modal-ip');
  bindModalClose('modal-wg-peer');
  document.getElementById('wg-body').addEventListener('click', wgClick);
  document.getElementById('btn-wg-remove').addEventListener('click', wgRemove);
  document.getElementById('btn-wg-download').addEventListener('click', wgDownload);
  await loadWireGuard();
});

async function confirmFromNewAddress() {
  const token = new URLSearchParams(location.search).get('netconfirm');
  if (!token) return;
  history.replaceState(null, '', location.pathname);
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    let res, data = {};
    try {
      res = await fetch('/api/network/confirm-token', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }),
      });
      data = await res.json().catch(() => ({}));
    } catch (e) {
      await sleep(1000);
      continue;
    }
    if (res.ok) {
      sessionStorage.setItem(NET_MSG, JSON.stringify(['ok', 'New network settings confirmed from the new address.']));
      return;
    }
    if (!/still being applied/.test(data.detail || '')) {
      sessionStorage.setItem(NET_MSG, JSON.stringify(['err', `Not confirmed: ${data.detail || res.status}`]));
      return;
    }
    await sleep(1000);
  }
  sessionStorage.setItem(NET_MSG, JSON.stringify(['err', 'Not confirmed: the new settings did not come up in time.']));
}

async function loadInterfaces() {
  const el = document.getElementById('iface-list');
  el.innerHTML = '<div class="page-loading"><div class="spinner"></div></div>';

  try {
    const [d, c] = await Promise.all([
      apiJSON('GET', '/api/network/interfaces'),
      apiJSON('GET', '/api/network/config'),
    ]);
    saved = c.interfaces || {};
    entries = Object.fromEntries((c.entries || []).map(e => [e.logical_name, e]));
    rollbackSeconds = c.rollback_seconds || 90;
    document.querySelectorAll('.rollback-seconds').forEach(s => { s.textContent = rollbackSeconds; });
    showBanner(c.pending);
    const ifaces = Array.isArray(d.interfaces) ? d.interfaces : Object.values(d.interfaces || {});
    present = ifaces.map(i => i.ifname || i.name).filter(Boolean);
    if (!ifaces.length) { el.innerHTML = emptyIface(); return; }
    el.innerHTML = ifaces.map(renderIface).join('');
  } catch (e) {
    el.innerHTML = `<div class="alert alert-err">${esc(e.message)}</div>`;
  }
}

function showBanner(p) {
  const banner = document.getElementById('net-pending-banner');
  if (!p || !p.pending || p.state !== 'waiting') { banner.hidden = true; return; }
  document.getElementById('net-pending-text').textContent =
    `New settings on ${p.interface} are waiting for confirmation: undone in ${p.seconds_left} s unless you keep them.`;
  banner.hidden = false;
}

function renderIface(i) {
  const name   = i.name || i.ifname || i.interface || Object.keys(i)[0] || 'unknown';
  const addr   = (i.addr_info || i.inet || []).map(a => `${a.local || a.address}/${a.prefixlen || a.prefix || ''}`).filter(Boolean);
  const v4     = (i.addr_info || []).find(a => a.family === 'inet');
  const mac    = i.address || i.mac || '';
  const isUp   = (i.operstate || i.flags || '').toLowerCase().includes('up');
  const isLo   = name === 'lo' || name.startsWith('lo');
  const cfg    = saved[name];
  const entry  = entries[name] || {};
  const method = entry.master ? '' : (cfg && cfg.method ? (cfg.method === 'static' ? 'Static' : 'DHCP') : '');
  const kind   = describeKind(entry);
  const isVirtual = ['bridge', 'bond', 'vlan'].includes(entry.type);

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
            ${kind ? `<div class="text-sm text-muted">${esc(kind)}</div>` : ''}
          </div>
        </div>
        <div class="d-flex items-center gap-3">
          ${method ? `<span class="badge badge-info">${method}</span>` : ''}
          ${cfg && cfg.mtu ? `<span class="badge badge-muted expert-only">MTU ${esc(cfg.mtu)}</span>` : ''}
          <span class="badge ${isUp ? 'badge-ok' : 'badge-muted'}">${isUp ? 'UP' : 'DOWN'}</span>
          ${!isLo && !entry.master && entry.type !== 'wireguard' ? `
            <button class="btn btn-secondary btn-sm advanced-only"
              data-iface="${esc(name)}" data-addr="${esc(v4 ? `${v4.local}/${v4.prefixlen}` : '')}">
              Configure
            </button>` : ''}
          ${isVirtual ? `
            <button class="btn btn-danger btn-sm advanced-only" data-remove="${esc(name)}">Remove</button>` : ''}
        </div>
      </div>
      ${addr.length ? `
        <div class="d-flex flex-col gap-2">
          ${addr.map(a => `<div class="d-flex items-center gap-2"><span class="badge badge-info font-mono">${esc(a)}</span></div>`).join('')}
        </div>` : '<div class="text-sm text-muted">No IPv4 address assigned</div>'}
    </div>`;
}

function describeKind(entry) {
  if (entry.master) return `Port of ${entry.master}`;
  if (entry.type === 'bridge') return `Bridge — ports: ${(entry.ports || []).join(', ')}`;
  if (entry.type === 'bond') return `Bond (${entry.mode === 'failover' ? 'failover' : 'LACP'}) — ports: ${(entry.ports || []).join(', ')}`;
  if (entry.type === 'vlan') return `VLAN ${entry.vlan_id} on ${entry.parent}`;
  if (entry.type === 'wireguard') return 'WireGuard VPN';
  return '';
}

function freeCards() {
  return present.filter(n => n.startsWith('fnas_lan') && !(entries[n] || {}).master);
}

function openVirtual() {
  const cards = freeCards();
  document.getElementById('v-ports').innerHTML = cards.length
    ? cards.map(n => `<label class="d-flex items-center gap-2"><input type="checkbox" value="${esc(n)}"> <span class="font-mono">${esc(n)}</span>${saved[n] && saved[n].method === 'static' ? ` <span class="text-sm text-muted">${esc(saved[n].ip)}</span>` : ''}</label>`).join('')
    : '<span class="text-sm text-muted">No free network card</span>';
  const parents = present.filter(n => (n.startsWith('fnas_lan') || n.startsWith('fnas_bond') || n.startsWith('fnas_bridge')) && !(entries[n] || {}).master);
  document.getElementById('f-v-parent').innerHTML = parents.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
  setValue('f-v-type', 'bridge');
  setValue('f-v-mode', 'lacp');
  setValue('f-v-id', '');
  document.getElementById('f-v-stp').checked = false;
  syncVirtual();
  openModal('modal-virtual');
}

function chosenPorts() {
  return Array.from(document.querySelectorAll('#v-ports input:checked')).map(i => i.value);
}

function syncVirtual() {
  const type = getValue('f-v-type');
  document.getElementById('v-ports-group').hidden = type === 'vlan';
  document.getElementById('v-mode-group').hidden = type !== 'bond';
  document.getElementById('v-stp-group').hidden = type !== 'bridge';
  document.getElementById('v-vlan-group').hidden = type !== 'vlan';
  document.getElementById('v-ports-hint').textContent = type === 'bond'
    ? 'At least two. The bond uses the MAC address of the first one'
    : 'The bridge uses the MAC address of the first one, so a DHCP reservation on your router keeps working';
  const first = chosenPorts()[0];
  document.getElementById('v-address-note').textContent = type === 'vlan'
    ? 'The VLAN gets its address by DHCP; change it afterwards with Configure.'
    : first
      ? `The new interface takes over the address settings of ${first}; the cards become its ports and have no address of their own.`
      : 'Choose the network cards.';
}

async function createVirtual(e) {
  e.preventDefault();
  const btn = document.getElementById('btn-v-submit');
  const type = getValue('f-v-type');
  const ports = chosenPorts();
  const body = { type };
  if (type === 'vlan') {
    body.parent = getValue('f-v-parent');
    body.vlan_id = getValue('f-v-id');
    body.config = { method: 'dhcp' };
  } else {
    body.ports = ports;
    body.mode = getValue('f-v-mode');
    body.stp = document.getElementById('f-v-stp').checked;
    body.config = (ports[0] && saved[ports[0]]) || { method: 'dhcp' };
  }
  setLoading(btn, true, 'Creating…');
  let started;
  try {
    started = await apiJSON('POST', '/api/network/virtual', body);
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
    return;
  } finally {
    setLoading(btn, false);
  }
  closeModal('modal-virtual');
  followPending(null, false);
}

async function removeVirtual(name) {
  const entry = entries[name] || {};
  const ports = esc((entry.ports || []).join(', '));
  const safe = esc(name);
  const ok = await confirm(`Remove ${safe}?`,
    ports ? `${ports} go back to getting an address by DHCP. If you are connected through ${safe}, the address may change.`
          : `Anything using ${safe} loses its connection.`, 'Remove');
  if (!ok) return;
  try {
    await apiJSON('DELETE', `/api/network/virtual/${encodeURIComponent(name)}`);
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
    return;
  }
  followPending(null, true);
}

function setValue(id, v) { document.getElementById(id).value = v ?? ''; }
function getValue(id) { return document.getElementById(id).value.trim(); }

function openEditIP(name, currentAddr) {
  const [ip, cidr] = (currentAddr || '/').split('/');
  const cfg = saved[name] || {};
  setValue('f-ip-iface', name);
  setValue('f-ip-current', ip);
  document.getElementById('modal-ip-title').textContent = `Configure ${name}`;
  setValue('f-ip-method', cfg.method || 'dhcp');
  setValue('f-ip-addr', cfg.ip || ip);
  setValue('f-ip-cidr', cfg.cidr || cidr || '24');
  setValue('f-ip-gw', cfg.gateway);
  setValue('f-ip-dns', [].concat(cfg.dns || []).join(', '));
  setValue('f-ip-search', [].concat(cfg.dns_search || []).join(', '));
  setValue('f-ip6-method', cfg.ipv6_method || 'auto');
  setValue('f-ip6-addr', cfg.ipv6);
  setValue('f-ip6-prefix', cfg.ipv6_prefix || '64');
  setValue('f-ip6-gw', cfg.ipv6_gateway);
  setValue('f-ip-mtu', cfg.mtu);
  setValue('f-ip-routes', (cfg.routes || []).map(r => `${r.destination} ${r.gateway}`).join('\n'));
  syncForm();
  openModal('modal-ip');
}

function syncForm() {
  const isStatic = getValue('f-ip-method') === 'static';
  document.getElementById('ipv4-static').hidden = !isStatic;
  document.getElementById('ipv6-static').hidden = getValue('f-ip6-method') !== 'static';
  document.getElementById('f-ip-dns-hint').textContent = isStatic
    ? 'IPv4 or IPv6 addresses, separated by commas. At least one is needed'
    : 'IPv4 or IPv6 addresses, separated by commas. Empty: the ones DHCP gives';
}

async function applyConfig(e) {
  e.preventDefault();
  const btn = document.getElementById('btn-ip-submit');
  const iface = getValue('f-ip-iface');
  const body = {
    method: getValue('f-ip-method'),
    ip: getValue('f-ip-addr'),
    cidr: getValue('f-ip-cidr'),
    gateway: getValue('f-ip-gw'),
    dns: getValue('f-ip-dns'),
    dns_search: getValue('f-ip-search'),
    ipv6_method: getValue('f-ip6-method'),
    ipv6: getValue('f-ip6-addr'),
    ipv6_prefix: getValue('f-ip6-prefix'),
    ipv6_gateway: getValue('f-ip6-gw'),
    mtu: getValue('f-ip-mtu'),
    routes: getValue('f-ip-routes'),
  };

  setLoading(btn, true, 'Applying…');
  let started;
  try {
    started = await apiJSON('PUT', `/api/network/interfaces/${encodeURIComponent(iface)}`, body);
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
    return;
  } finally {
    setLoading(btn, false);
  }
  closeModal('modal-ip');

  const moving = body.method === 'static' && body.ip !== getValue('f-ip-current');
  const target = moving
    ? `${location.protocol}//${body.ip}${location.port ? `:${location.port}` : ''}/network/?netconfirm=${encodeURIComponent(started.token)}`
    : null;
  followPending(target, body.method === 'dhcp');
}

function followPending(target, toDhcp) {
  const status = document.getElementById('net-pending-status');
  const link = document.getElementById('net-pending-link');
  const keepBtn = document.getElementById('btn-net-keep');
  keepBtn.disabled = true;
  link.hidden = true;
  status.textContent = 'Applying…';
  openModal('modal-net-pending');

  let secondsLeft = rollbackSeconds;
  let lost = 0;
  let busy = false;
  clearInterval(pendingTimer);
  pendingTimer = setInterval(async () => {
    secondsLeft = Math.max(0, secondsLeft - 1);
    if (busy) return;
    busy = true;
    let p;
    try {
      p = await pollPending();
      lost = 0;
    } catch (err) {
      lost += 1;
      if (target && lost >= 2) { location.href = target; return; }
      status.textContent = `The connection to this address was lost. The settings are undone by themselves in ${secondsLeft} s unless they are confirmed from the new address.`;
      if (toDhcp) {
        link.hidden = false;
        link.textContent = 'The new address comes from DHCP: look it up on your router or on the console, and open the Network page there to keep the settings.';
      }
      if (secondsLeft === 0) { clearInterval(pendingTimer); status.textContent = 'The settings were undone. Reload this page.'; }
      return;
    } finally {
      busy = false;
    }
    if (!p.pending) {
      clearInterval(pendingTimer);
      status.textContent = p.expired ? 'Nobody confirmed in time: the previous settings are back.' : 'No change is pending.';
      keepBtn.disabled = true;
      return;
    }
    secondsLeft = p.seconds_left;
    if (p.state === 'failed') {
      clearInterval(pendingTimer);
      status.textContent = `The settings could not be applied and were undone: ${p.error}`;
      return;
    }
    if (p.state === 'applying') { status.textContent = 'Applying…'; return; }
    if (target) {
      clearInterval(pendingTimer);
      status.textContent = 'Moving to the new address…';
      link.hidden = false;
      link.innerHTML = `If the page does not move by itself, open <a href="${esc(target)}">${esc(target.split('/network/')[0])}</a>.`;
      location.href = target;
      return;
    }
    status.textContent = `The new settings are active. Keep them within ${secondsLeft} s, or they are undone.`;
    keepBtn.disabled = false;
  }, 1000);
}

async function pollPending() {
  const res = await fetch('/api/network/pending', {
    headers: { Authorization: `Basic ${Auth.get()}` },
    signal: AbortSignal.timeout(3000),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function keep() {
  try {
    await apiJSON('POST', '/api/network/confirm');
    clearInterval(pendingTimer);
    closeModal('modal-net-pending');
    toast('Network settings kept.', 'ok');
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  }
  await loadInterfaces();
}

async function revert() {
  try {
    await apiJSON('POST', '/api/network/revert');
    clearInterval(pendingTimer);
    closeModal('modal-net-pending');
    toast('Network change undone.', 'ok');
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  }
  setTimeout(loadInterfaces, 2000);
}

let wgDevice = null;

async function loadWireGuard() {
  const body = document.getElementById('wg-body');
  let d;
  try {
    d = await apiJSON('GET', '/api/network/wireguard');
  } catch (err) {
    body.innerHTML = `<div class="alert alert-err">${esc(err.message)}</div>`;
    return;
  }
  document.getElementById('btn-wg-remove').hidden = !d.server;
  if (!d.server) {
    body.innerHTML = `
      <div class="form-row">
        <div class="form-group" style="flex:3">
          <label class="form-label" for="f-wg-endpoint">Public address</label>
          <input id="f-wg-endpoint" class="input" type="text" placeholder="myhome.example.org or 203.0.113.7">
        </div>
        <div class="form-group" style="flex:1">
          <label class="form-label" for="f-wg-port">Port (UDP)</label>
          <input id="f-wg-port" class="input" type="number" value="51820" min="1024" max="65535">
        </div>
      </div>
      <div class="form-row">
        <div class="form-group" style="flex:1">
          <label class="form-label" for="f-wg-address">VPN network</label>
          <input id="f-wg-address" class="input" type="text" value="10.10.0.1/24">
        </div>
        <div class="form-group" style="flex:1">
          <label class="form-label" for="f-wg-dns">DNS for the devices</label>
          <input id="f-wg-dns" class="input" type="text" placeholder="optional, e.g. 192.168.1.1">
        </div>
      </div>
      <span class="form-hint">On your router, forward this UDP port to the NAS. The devices reach the NAS itself (shares, WebUI) at its VPN address.</span>
      <div class="d-flex" style="margin-top:12px"><button class="btn btn-primary" data-wg="create">Create VPN</button></div>`;
    return;
  }
  const s = d.server;
  const ago = (t) => t ? `${Math.max(0, Math.round(Date.now() / 1000 - t))} s ago` : 'never';
  body.innerHTML = `
    <div class="text-sm" style="margin-bottom:12px">
      <div>NAS address in the VPN: <span class="font-mono">${esc(s.address)}</span> · port <span class="font-mono">${esc(s.listen_port)}</span> · public address <span class="font-mono">${esc(s.endpoint)}</span></div>
      <div class="text-muted">Public key: <span class="font-mono">${esc(s.public_key)}</span></div>
    </div>
    ${d.peers.length ? `<div class="table-wrap"><table><thead><tr><th>Device</th><th>Address</th><th>Last contact</th><th>Received / sent</th><th></th></tr></thead><tbody>
      ${d.peers.map(p => `<tr><td>${esc(p.name)}</td><td class="font-mono">${esc(p.address)}</td><td>${ago(p.latest_handshake)}</td>
        <td>${esc(p.rx)} / ${esc(p.tx)} B</td><td><button class="btn btn-danger btn-sm" data-wg="remove" data-name="${esc(p.name)}">Remove</button></td></tr>`).join('')}
    </tbody></table></div>` : '<div class="text-sm text-muted">No device yet.</div>'}
    <div class="d-flex gap-2" style="margin-top:12px">
      <input id="f-wg-peer" class="input" type="text" placeholder="Device name, e.g. Phone" style="max-width:260px">
      <button class="btn btn-primary" data-wg="add">Add device</button>
    </div>`;
}

async function wgClick(e) {
  const btn = e.target.closest('[data-wg]');
  if (!btn) return;
  const action = btn.dataset.wg;
  try {
    if (action === 'create') {
      setLoading(btn, true, 'Creating…');
      await apiJSON('POST', '/api/network/wireguard', {
        endpoint: getValue('f-wg-endpoint'), listen_port: getValue('f-wg-port'),
        address: getValue('f-wg-address'), dns: getValue('f-wg-dns'),
      });
      toast('WireGuard VPN created.', 'ok');
    } else if (action === 'add') {
      setLoading(btn, true, 'Adding…');
      const r = await apiJSON('POST', '/api/network/wireguard/peers', { name: getValue('f-wg-peer') });
      wgDevice = r;
      document.getElementById('wg-peer-title').textContent = `New device: ${r.peer.name}`;
      document.getElementById('wg-peer-qr').innerHTML = r.qr_svg || '<span class="text-sm">QR code not available: use the file.</span>';
      document.getElementById('wg-peer-conf').textContent = r.config;
      openModal('modal-wg-peer');
    } else if (action === 'remove') {
      if (!await confirm(`Remove ${esc(btn.dataset.name)}?`, 'The device can no longer connect.', 'Remove')) return;
      await apiJSON('DELETE', `/api/network/wireguard/peers/${encodeURIComponent(btn.dataset.name)}`);
      toast('Device removed.', 'ok');
    }
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  } finally {
    setLoading(btn, false);
  }
  await loadWireGuard();
  await loadInterfaces();
}

async function wgRemove() {
  if (!await confirm('Remove the WireGuard VPN?', 'Every device loses access. Creating it again makes a new key: the devices need new configurations.', 'Remove')) return;
  try {
    await apiJSON('DELETE', '/api/network/wireguard');
    toast('WireGuard VPN removed.', 'ok');
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  }
  await loadWireGuard();
  await loadInterfaces();
}

function wgDownload() {
  if (!wgDevice) return;
  const blob = new Blob([wgDevice.config], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${wgDevice.peer.name.replace(/[^A-Za-z0-9._-]+/g, '_')}.conf`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function emptyIface() {
  return `<div class="empty-state">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="7" width="20" height="15" rx="2"/><path d="M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2"/></svg>
    <h3>No interfaces found</h3>
  </div>`;
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
