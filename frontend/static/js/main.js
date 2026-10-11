/* FreedomNAS — Shared utilities (loaded on every authenticated page) */

'use strict';

// ── Auth ────────────────────────────────────────────────────────────────────
const Auth = {
  key: 'fnas_auth',
  userKey: 'fnas_user',

  get()     { return sessionStorage.getItem(this.key); },
  getUser() { return sessionStorage.getItem(this.userKey) || 'admin'; },
  set(u, p) {
    sessionStorage.setItem(this.key, btoa(`${u}:${p}`));
    sessionStorage.setItem(this.userKey, u);
  },
  clear() {
    sessionStorage.removeItem(this.key);
    sessionStorage.removeItem(this.userKey);
  },
  isLoggedIn() { return !!this.get(); },
};

// ── API ─────────────────────────────────────────────────────────────────────
async function api(method, path, body = null) {
  const headers = { 'Content-Type': 'application/json' };
  const auth = Auth.get();
  if (auth) headers['Authorization'] = `Basic ${auth}`;

  const opts = { method, headers };
  if (body !== null) opts.body = JSON.stringify(body);

  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new Error(`Network error: ${e.message}`);
  }

  if (res.status === 401) {
    Auth.clear();
    const next = encodeURIComponent(location.pathname);
    location.href = `/auth/?next=${next}`;
    throw new Error('Unauthorized');
  }

  if (res.status === 403) {
    throw new Error('This account is not an administrator');
  }

  if (res.status === 503) {
    location.href = '/setup/';
    throw new Error('Not initialized');
  }

  return res;
}

async function apiJSON(method, path, body = null) {
  const res = await api(method, path, body);
  const ct = res.headers.get('content-type') || '';
  if (!ct.includes('application/json')) {
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return {};
  }
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

// ── Owners ───────────────────────────────────────────────────────────────────
// The accounts a dataset or a share can be given to, into a <select>. With
// `automatic`, a first empty choice leaves the decision to the server.
async function fillOwnerSelect(select, automatic = null) {
  select.replaceChildren();
  if (automatic) select.add(new Option(automatic, ''));
  let names = [];
  try { names = (await apiJSON('GET', '/api/users/')).users.map(u => u.username); }
  catch { /* the signed-in account is listed below all the same */ }
  const me = Auth.getUser();
  if (!names.includes(me)) names.unshift(me);
  names.forEach(n => select.add(new Option(n, n)));
  select.value = automatic ? '' : me;
}

// ── Toast ────────────────────────────────────────────────────────────────────
const Icons = {
  ok:   `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>`,
  err:  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
  warn: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
  info: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`,
};

function toast(msg, type = 'info', duration = 4000) {
  let stack = document.getElementById('toast-stack');
  if (!stack) {
    stack = document.createElement('div');
    stack.id = 'toast-stack';
    document.body.appendChild(stack);
  }
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.innerHTML = `${Icons[type] || Icons.info}<span></span>`;
  t.querySelector('span').textContent = msg;
  stack.appendChild(t);
  setTimeout(() => {
    t.classList.add('out');
    t.addEventListener('animationend', () => t.remove(), { once: true });
  }, duration);
}

// ── Confirm modal ─────────────────────────────────────────────────────────────
function confirm(title, msg, dangerLabel = 'Confirm') {
  return new Promise((resolve) => {
    const existing = document.getElementById('global-confirm');
    if (existing) existing.remove();

    const el = document.createElement('div');
    el.id = 'global-confirm';
    el.className = 'modal-backdrop';
    el.innerHTML = `
      <div class="modal" style="max-width:380px">
        <div class="modal-header">
          <span class="modal-title">${title}</span>
        </div>
        <div class="modal-body"><p style="font-size:0.875rem">${msg}</p></div>
        <div class="modal-footer">
          <button class="btn btn-secondary" id="gc-cancel">Cancel</button>
          <button class="btn btn-danger"    id="gc-ok">${dangerLabel}</button>
        </div>
      </div>`;
    document.body.appendChild(el);
    requestAnimationFrame(() => el.classList.add('open'));

    const close = (val) => {
      el.classList.remove('open');
      el.addEventListener('transitionend', () => el.remove(), { once: true });
      resolve(val);
    };

    document.getElementById('gc-cancel').onclick = () => close(false);
    document.getElementById('gc-ok').onclick     = () => close(true);
    el.addEventListener('click', (e) => { if (e.target === el) close(false); });
  });
}

// ── Theme ─────────────────────────────────────────────────────────────────────
const Theme = {
  key: 'fnas_theme',
  current() { return localStorage.getItem(this.key) || 'dark'; },
  apply(t)  { document.documentElement.setAttribute('data-theme', t); },
  toggle()  {
    const next = this.current() === 'dark' ? 'light' : 'dark';
    localStorage.setItem(this.key, next);
    this.apply(next);
    updateThemeIcon();
  },
};

function updateThemeIcon() {
  const btn = document.getElementById('btn-theme');
  if (!btn) return;
  const isDark = Theme.current() === 'dark';
  btn.title = isDark ? 'Switch to light theme' : 'Switch to dark theme';
  btn.innerHTML = isDark
    ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>`
    : `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/></svg>`;
}

// ── UI Level ─────────────────────────────────────────────────────────────────
const UILevel = {
  key: 'fnas_ui_level',
  LEVELS: ['assisted', 'advanced', 'expert'],
  current() { return localStorage.getItem(this.key) || 'assisted'; },
  set(level) {
    if (!this.LEVELS.includes(level)) return;
    localStorage.setItem(this.key, level);
    document.body.setAttribute('data-ui', level);
    this.updatePicker();
  },
  apply() { this.set(this.current()); },
  updatePicker() {
    const sel = document.getElementById('ui-level-select');
    if (sel) sel.value = this.current();
  },
};

// ── Sidebar ──────────────────────────────────────────────────────────────────
const Sidebar = {
  key: 'fnas_sidebar',
  // Must match the max-width of the mobile @media block in main.css.
  mobileQuery: window.matchMedia('(max-width: 640px)'),
  isMobile() { return this.mobileQuery.matches; },
  isCollapsed() { return localStorage.getItem(this.key) === '1'; },
  toggle() {
    // On phones the sidebar is an off-canvas drawer: open/close it without
    // touching the desktop collapsed preference.
    if (this.isMobile()) { this.setMobileOpen(!this.isMobileOpen()); return; }
    const collapsed = !this.isCollapsed();
    localStorage.setItem(this.key, collapsed ? '1' : '0');
    this.apply();
  },
  isMobileOpen() {
    return document.querySelector('.sidebar')?.classList.contains('mobile-open') ?? false;
  },
  setMobileOpen(open) {
    document.querySelector('.sidebar')?.classList.toggle('mobile-open', open);
    const backdrop = document.getElementById('sidebar-backdrop');
    if (backdrop) backdrop.hidden = !open;
  },
  init() {
    this.apply();
    document.getElementById('sidebar-backdrop')?.addEventListener('click', () => this.setMobileOpen(false));
    document.addEventListener('keydown', e => { if (e.key === 'Escape') this.setMobileOpen(false); });
    // Leaving the mobile width (rotation, "desktop site") must not leave the drawer open.
    this.mobileQuery.addEventListener('change', () => this.setMobileOpen(false));
  },
  apply() {
    const sidebar = document.querySelector('.sidebar');
    const main    = document.querySelector('.main-area');
    if (!sidebar || !main) return;
    if (this.isCollapsed()) {
      sidebar.classList.add('collapsed');
      main.classList.add('sidebar-collapsed');
    } else {
      sidebar.classList.remove('collapsed');
      main.classList.remove('sidebar-collapsed');
    }
  },
};

// ── Active nav ────────────────────────────────────────────────────────────────
function markActiveNav() {
  const path = location.pathname.replace(/\/$/, '') || '/';
  document.querySelectorAll('.nav-item[href]').forEach(a => {
    const href = a.getAttribute('href').replace(/\/$/, '') || '/';
    a.classList.toggle('active', href === path || (href !== '' && path.startsWith(href)));
  });
}

// ── Edition badge ────────────────────────────────────────────────────────────
async function loadEditionBadge() {
  const el = document.getElementById('sidebar-edition');
  if (!el) return;
  try {
    const d = await apiJSON('GET', '/api/system/info');
    el.textContent = `${d.edition || ''} edition`;
    saveDocsContext(d);
  } catch { /* ignore */ }
}

// ── Documentation ─────────────────────────────────────────────────────────────
function saveDocsContext(info) {
  const context = {
    version: info.version || null,
    edition: info.edition || null,
    init_system: info.init_system || null,
    firmware: info.firmware || null,
  };
  try { localStorage.setItem('fnas_docs_context', JSON.stringify(context)); } catch { /* ignore */ }
}

const DOCS_PAGES = {
  '/dashboard/': 'user-guide/dashboard/',
  '/storage/':   'user-guide/storage/',
  '/shares/':    'user-guide/shares/',
  '/network/':   'user-guide/network/',
  '/users/':     'user-guide/users/',
  '/updates/':   'user-guide/updates/',
  '/services/':  'user-guide/services/',
  '/system/':    'user-guide/system/',
  '/setup/':     'getting-started/first-run/',
};

function setHelpLink() {
  const btn = document.getElementById('btn-help');
  if (!btn) return;
  const page = DOCS_PAGES[location.pathname] || DOCS_PAGES[`${location.pathname}/`];
  btn.href = `/docs/${page || ''}`;
}

// ── User badge ────────────────────────────────────────────────────────────────
function loadUserBadge() {
  const u = Auth.getUser();
  const el = document.getElementById('topbar-username');
  if (el) el.textContent = u;
  const av = document.getElementById('topbar-avatar');
  if (av) av.textContent = u.slice(0, 1).toUpperCase();
}

// ── Notices: restart the WebUI, reboot the system ────────────────────────────
// Shown on every page until acted on: FreedomNAS was updated and the WebUI
// still runs the old code, or a new kernel waits for a reboot. Neither
// happens by itself, so running jobs can finish first.
const Notices = {
  interval: 120000,
  _timer: null,

  box() {
    let box = document.getElementById('global-notices');
    if (!box) {
      const content = document.querySelector('.page-content');
      if (!content) return null;
      box = document.createElement('div');
      box.id = 'global-notices';
      content.prepend(box);
    }
    return box;
  },

  async refresh() {
    const box = this.box();
    if (!box) return;
    const [webui, boot] = await Promise.all([
      apiJSON('GET', '/api/system/webui').catch(() => null),
      apiJSON('GET', '/api/system/boot').catch(() => null),
    ]);
    box.replaceChildren();
    if (webui && webui.restart_needed) {
      box.appendChild(this.notice('alert-info',
        'FreedomNAS was updated.',
        'Restart the WebUI to use the new version; the page reloads by itself.',
        'Restart WebUI', () => restartWebUI()));
    }
    if (boot && boot.reboot_needed) {
      box.appendChild(this.notice('alert-warn',
        'A new kernel is installed.',
        `Reboot to start using ${boot.kernel.installed} (running ${boot.kernel.running}).`,
        'Reboot now', () => rebootSystem()));
    }
    clearTimeout(this._timer);
    this._timer = setTimeout(() => this.refresh(), this.interval);
  },

  notice(kind, title, text, label, action) {
    const el = document.createElement('div');
    el.className = `alert ${kind} mb-4`;
    el.setAttribute('role', 'status');
    el.innerHTML = `<div><strong></strong> <span></span>
      <div class="d-flex gap-2" style="margin-top:8px"><button class="btn btn-secondary btn-sm"></button></div></div>`;
    el.querySelector('strong').textContent = title;
    el.querySelector('span').textContent = text;
    const btn = el.querySelector('button');
    btn.textContent = label;
    btn.addEventListener('click', action);
    return el;
  },
};

function busyText(busy) {
  const parts = [];
  if (busy.replication) parts.push(`${busy.replication} replication job${busy.replication !== 1 ? 's' : ''}`);
  return parts.join(', ');
}

async function restartWebUI() {
  let state;
  try { state = await apiJSON('GET', '/api/system/webui'); }
  catch (e) { toast(`Error: ${e.message}`, 'err'); return; }
  if (state.busy.updates) {
    toast('An update is running: restart the WebUI once it has finished.', 'warn', 6000);
    return;
  }
  if (state.busy.plugins) {
    toast('A module is being installed or removed: restart the WebUI once it has finished.', 'warn', 6000);
    return;
  }
  const running = busyText(state.busy);
  const msg = running
    ? `${running} still running will be stopped. Restart the WebUI anyway?`
    : 'The WebUI restarts and this page reloads when it is back. Jobs started from other pages are interrupted.';
  if (!await confirm('Restart WebUI', msg, 'Restart')) return;

  try {
    await apiJSON('POST', '/api/system/webui/restart', { force: !!running });
  } catch (e) {
    toast(`Error: ${e.message}`, 'err');
    return;
  }
  waitForWebUI(state.started);
}

function waitForWebUI(previous) {
  const el = document.createElement('div');
  el.className = 'modal-backdrop open';
  el.innerHTML = `<div class="modal" style="max-width:380px">
    <div class="modal-body"><p style="font-size:0.875rem"><span class="spinner"></span>
    <span id="restart-wait-text">Restarting the WebUI…</span></p></div></div>`;
  document.body.appendChild(el);
  const deadline = Date.now() + 120000;
  const poll = async () => {
    try {
      const d = await apiJSON('GET', '/api/system/webui');
      if (d.started !== previous) { location.reload(); return; }
    } catch { /* down while it restarts */ }
    if (Date.now() > deadline) {
      el.querySelector('#restart-wait-text').textContent =
        'The WebUI has not come back after two minutes. Reload the page in a moment, or check the system console.';
      return;
    }
    setTimeout(poll, 1500);
  };
  setTimeout(poll, 1500);
}

async function rebootSystem() {
  if (!await confirm('Reboot System', 'The system will reboot. All active sessions will be disconnected.', 'Reboot')) return;
  try {
    await apiJSON('POST', '/api/system/reboot');
    toast('Reboot initiated. This page will be unavailable for a few minutes.', 'warn', 8000);
  } catch (e) {
    toast(`Error: ${e.message}`, 'err');
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
function initShared() {
  if (!Auth.isLoggedIn()) {
    Auth.clear();
    const next = encodeURIComponent(location.pathname);
    location.href = `/auth/?next=${next}`;
    return false;
  }

  Theme.apply(Theme.current());
  UILevel.apply();
  Sidebar.init();
  markActiveNav();
  setHelpLink();
  loadUserBadge();
  loadEditionBadge();
  updateThemeIcon();
  Notices.refresh();

  document.getElementById('btn-theme')?.addEventListener('click', () => Theme.toggle());
  document.getElementById('btn-sidebar-toggle')?.addEventListener('click', () => Sidebar.toggle());
  document.getElementById('btn-logout')?.addEventListener('click', () => {
    Auth.clear();
    location.href = '/auth/';
  });

  const uiSel = document.getElementById('ui-level-select');
  if (uiSel) {
    uiSel.addEventListener('change', () => UILevel.set(uiSel.value));
    UILevel.updatePicker();
  }

  return true;
}

// ── Modal helper ──────────────────────────────────────────────────────────────
function openModal(id) {
  const m = document.getElementById(id);
  if (m) { m.classList.add('open'); m.querySelector('.input')?.focus(); }
}

function closeModal(id) {
  const m = document.getElementById(id);
  if (m) m.classList.remove('open');
}

function bindModalClose(id) {
  const m = document.getElementById(id);
  if (!m) return;
  m.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => closeModal(id)));
  m.addEventListener('click', (e) => { if (e.target === m) closeModal(id); });
}

// ── Spinner helper ────────────────────────────────────────────────────────────
function setLoading(btn, loading, label) {
  if (!btn) return;
  if (loading) {
    btn._origHTML = btn.innerHTML;
    btn.innerHTML = `<span class="spinner"></span>${label ? `<span>${label}</span>` : ''}`;
    btn.disabled = true;
  } else {
    btn.innerHTML = btn._origHTML || btn.innerHTML;
    btn.disabled = false;
  }
}
