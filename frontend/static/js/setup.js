'use strict';

const MIN_PASSWORD = 8;
const MIN_PASSPHRASE = 12;
const MIN_DISKS = { '': 1, mirror: 2, raidz1: 3, raidz2: 4 };

document.addEventListener('DOMContentLoaded', async () => {
  const theme = localStorage.getItem('fnas_theme') || 'dark';
  document.documentElement.setAttribute('data-theme', theme);

  const auth = sessionStorage.getItem('fnas_auth');
  const login = sessionStorage.getItem('fnas_user') || 'admin';
  if (!auth) { location.href = '/auth/?next=/setup/'; return; }

  async function api(method, path, body) {
    const opts = { method, headers: { 'Authorization': `Basic ${auth}` } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    if (res.status === 401) {
      sessionStorage.removeItem('fnas_auth');
      location.href = '/auth/?next=/setup/';
      throw new Error('signed out');
    }
    return res;
  }

  try {
    const res = await fetch('/api/setup/status');
    const d = await res.json();
    if (d.initialized) { location.href = '/dashboard/'; return; }
  } catch { /* proceed */ }

  document.getElementById('f-admin-name').placeholder = login;
  document.getElementById('admin-name-hint').textContent = `Leave empty to keep "${login}".`;
  document.getElementById('remove-default-label').textContent = `Remove the account "${login}"`;
  fillTimezones(document.getElementById('f-timezone'),
                Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');

  let pools = [];
  let systemPool = null;
  let devicesLoaded = false;

  const steps = [1, 2, 3, 4, 5].reduce((acc, n) => ({ ...acc, [n]: document.getElementById(`step-${n}`) }), {});
  showStep(1);

  const adminName = document.getElementById('f-admin-name');
  adminName.addEventListener('input', () => {
    const name = adminName.value.trim();
    document.getElementById('remove-default-group').hidden = !name || name === login;
  });

  document.getElementById('btn-next-1').addEventListener('click', () => {
    const name = adminName.value.trim();
    const pass = document.getElementById('f-admin-pass').value;
    const pass2 = document.getElementById('f-admin-pass2').value;
    let ok = true;
    if (name && !/^[a-z_][a-z0-9_-]{0,31}$/.test(name)) {
      fieldErr('f-admin-name', 'Lowercase letters, digits, _ and -, starting with a letter.'); ok = false;
    } else clearErr('f-admin-name');
    if (pass.length < MIN_PASSWORD) {
      fieldErr('f-admin-pass', `At least ${MIN_PASSWORD} characters.`); ok = false;
    } else if (btoa(`${login}:${pass}`) === auth) {
      fieldErr('f-admin-pass', 'Choose a new password, not the one you signed in with.'); ok = false;
    } else clearErr('f-admin-pass');
    if (pass2 !== pass) { fieldErr('f-admin-pass2', 'The passwords differ.'); ok = false; }
    else clearErr('f-admin-pass2');
    if (ok) showStep(2);
  });

  document.getElementById('btn-back-2').addEventListener('click', () => showStep(1));
  document.getElementById('btn-next-2').addEventListener('click', async () => {
    const host = document.getElementById('f-hostname').value.trim();
    if (!isValidHostname(host)) { fieldErr('f-hostname', 'Letters, digits and hyphens only.'); return; }
    clearErr('f-hostname');
    showStep(3);
    if (!devicesLoaded) await loadDevices();
  });

  document.getElementById('f-create-pool').addEventListener('change', e => {
    document.getElementById('pool-form').hidden = !e.target.checked;
  });
  document.getElementById('f-pool-encrypt').addEventListener('change', e => {
    document.getElementById('pool-passphrase-group').hidden = !e.target.checked;
  });

  document.getElementById('btn-back-3').addEventListener('click', () => showStep(2));
  document.getElementById('btn-next-3').addEventListener('click', async () => {
    if (creatingPool() && !checkPool()) return;
    await loadPools();
    showStep(4);
  });

  document.getElementById('btn-back-4').addEventListener('click', () => showStep(3));
  document.getElementById('btn-next-4').addEventListener('click', () => {
    if (!selectedStatePool()) { toast('Choose where the configuration is kept.'); return; }
    buildSummary();
    showStep(5);
  });

  document.getElementById('btn-back-5').addEventListener('click', () => showStep(4));
  document.getElementById('btn-initialize').addEventListener('click', initialize);
  document.getElementById('btn-go-auth').addEventListener('click', () => {
    sessionStorage.removeItem('fnas_auth');
    sessionStorage.removeItem('fnas_user');
    location.href = '/auth/';
  });

  function showStep(n) {
    Object.values(steps).forEach(s => s && (s.hidden = true));
    if (steps[n]) steps[n].hidden = false;
    document.querySelectorAll('.wizard-footer').forEach(f => f.hidden = f.id !== `footer-${n}`);
    document.querySelectorAll('.wizard-step').forEach(el => {
      const sn = parseInt(el.dataset.step);
      el.classList.toggle('active', sn === n);
      el.classList.toggle('done', sn < n);
    });
  }

  async function loadDevices() {
    const box = document.getElementById('pool-devices');
    try {
      const d = await (await api('GET', '/api/setup/devices')).json();
      const disks = (d.devices || []).filter(isDataDisk);
      box.replaceChildren(...disks.map(x => {
        const row = document.createElement('label');
        row.className = 'device-row' + (x.in_use ? ' device-busy' : '');
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.value = x.name;
        input.disabled = !!x.in_use;
        const text = document.createElement('span');
        text.textContent = [x.name, x.model, x.size ? formatBytes(x.size) : '', x.in_use ? '— in use' : '']
          .filter(Boolean).join('  ');
        row.append(input, text);
        return row;
      }));
      if (!disks.length) box.textContent = 'No disks found.';
      devicesLoaded = true;
    } catch (e) {
      box.textContent = `Could not list the disks: ${e.message}`;
    }
  }

  async function loadPools() {
    try {
      const d = await (await api('GET', '/api/setup/pools')).json();
      pools = (d.pools || []).map(p => p.name);
      systemPool = d.system_pool;
    } catch { pools = []; }
    const choices = [...pools];
    const newPool = creatingPool() ? poolName() : null;
    if (newPool && !choices.includes(newPool)) choices.push(newPool);
    const preferred = newPool || choices.find(p => p !== systemPool) || systemPool || choices[0];
    const box = document.getElementById('state-pools');
    box.replaceChildren(...choices.map(name => {
      const row = document.createElement('label');
      row.className = 'device-row';
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = 'state-pool';
      input.value = name;
      input.checked = name === preferred;
      const text = document.createElement('span');
      const role = name === systemPool ? 'system disk'
        : name === newPool ? 'new data pool' : 'data pool';
      text.textContent = `${name} — ${role}${name === preferred && name !== systemPool ? ' (recommended)' : ''}`;
      row.append(input, text);
      return row;
    }));
  }

  function creatingPool() { return document.getElementById('f-create-pool').checked; }
  function poolName() { return document.getElementById('f-pool-name').value.trim(); }
  function poolDevices() {
    return [...document.querySelectorAll('#pool-devices input:checked')].map(i => i.value);
  }
  function selectedStatePool() {
    return document.querySelector('input[name="state-pool"]:checked')?.value || '';
  }

  function checkPool() {
    const name = poolName();
    const layout = document.getElementById('f-pool-layout').value;
    if (!/^[A-Za-z][A-Za-z0-9_.:-]*$/.test(name)) {
      fieldErr('f-pool-name', 'Starts with a letter; letters, digits, _ . : -'); return false;
    }
    clearErr('f-pool-name');
    if (poolDevices().length < MIN_DISKS[layout]) {
      toast(`This layout needs at least ${MIN_DISKS[layout]} disk(s).`); return false;
    }
    if (document.getElementById('f-pool-encrypt').checked &&
        document.getElementById('f-pool-passphrase').value.length < MIN_PASSPHRASE) {
      fieldErr('f-pool-passphrase', `At least ${MIN_PASSPHRASE} characters.`); return false;
    }
    clearErr('f-pool-passphrase');
    return true;
  }

  function payload() {
    const body = {
      hostname: document.getElementById('f-hostname').value.trim(),
      timezone: document.getElementById('f-timezone').value.trim() || 'UTC',
      admin: {
        username: adminName.value.trim(),
        password: document.getElementById('f-admin-pass').value,
        remove_default: document.getElementById('f-remove-default').checked,
      },
      state_pool: selectedStatePool(),
    };
    if (creatingPool()) {
      body.pool = {
        name: poolName(),
        vdev_type: document.getElementById('f-pool-layout').value,
        devices: poolDevices(),
        encrypted: document.getElementById('f-pool-encrypt').checked,
        master_passphrase: document.getElementById('f-pool-passphrase').value,
      };
    }
    return body;
  }

  function buildSummary() {
    const b = payload();
    const admin = b.admin.username || login;
    const rows = [
      ['Administrator', admin + (b.admin.username && b.admin.username !== login
        ? (b.admin.remove_default ? ` ("${login}" removed)` : ` ("${login}" kept)`) : '')],
      ['Hostname', b.hostname],
      ['Timezone', b.timezone],
      ['Data pool', b.pool ? `${b.pool.name} — ${b.pool.vdev_type || 'single/stripe'} on ${b.pool.devices.join(', ')}${b.pool.encrypted ? ', encrypted' : ''}` : 'none'],
      ['Configuration on', b.state_pool],
    ];
    document.getElementById('summary').replaceChildren(...rows.map(([k, v]) => {
      const tr = document.createElement('tr');
      const th = document.createElement('td');
      th.className = 'text-muted';
      th.style.cssText = 'padding:6px 0;width:140px';
      th.textContent = k;
      const td = document.createElement('td');
      td.textContent = v;
      tr.append(th, td);
      return tr;
    }));
  }

  async function initialize() {
    const btn = document.getElementById('btn-initialize');
    const err = document.getElementById('init-error');
    btn.disabled = true;
    btn.textContent = 'Applying…';
    err.hidden = true;
    try {
      const res = await api('POST', '/api/setup/initialize', payload());
      if (res.ok) {
        steps[5].hidden = true;
        document.getElementById('footer-5').hidden = true;
        document.getElementById('step-done').hidden = false;
        return;
      }
      const d = await res.json();
      err.textContent = d.detail || 'Setup failed.';
      err.hidden = false;
    } catch (e) {
      err.textContent = `Error: ${e.message}`;
      err.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Apply';
    }
  }
});

function isDataDisk(d) {
  if (d.type && d.type !== 'disk') return false;
  if (/\/(fd|sr|loop|zram|ram)\d/.test(d.name)) return false;
  return !d.size || d.size >= 1e9;
}

function isValidHostname(h) {
  return /^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$/.test(h);
}

function formatBytes(n) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${units[i]}`;
}

function toast(msg) {
  const stack = document.getElementById('toast-stack');
  const el = document.createElement('div');
  el.className = 'toast toast-err';
  el.textContent = msg;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function fieldErr(id, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.borderColor = 'var(--err)';
  let hint = el.parentElement.querySelector('.field-err');
  if (!hint) { hint = document.createElement('span'); hint.className = 'form-error field-err'; el.parentElement.appendChild(hint); }
  hint.textContent = msg;
}

function clearErr(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.borderColor = '';
  el.parentElement.querySelector('.field-err')?.remove();
}

function fillTimezones(select, selected) {
  let zones = [];
  try { zones = Intl.supportedValuesOf('timeZone'); } catch { /* older browser */ }
  if (!zones.includes('UTC')) zones = ['UTC', ...zones];
  if (selected && !zones.includes(selected)) zones = [selected, ...zones];
  select.replaceChildren(...zones.map(z => new Option(z, z, false, z === selected)));
}
