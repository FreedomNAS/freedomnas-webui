'use strict';

const JOB_TITLES = { install: 'Installing', remove: 'Removing', refresh: 'Checking the repository' };

let _plugins = null;
let _restartNeeded = false;
let _jobNext = 0;
let _jobTimer = null;
let _removing = null;

document.addEventListener('DOMContentLoaded', () => {
  if (!initShared()) return;
  bindModalClose('modal-plugin-remove');
  document.getElementById('plugins-list').addEventListener('click', onPluginAction);
  document.getElementById('btn-plugins-refresh').addEventListener('click', () => startJob('POST', '/api/plugins/refresh'));
  document.getElementById('btn-plugin-remove-ok').addEventListener('click', () => {
    closeModal('modal-plugin-remove');
    if (_removing) startJob('DELETE', `/api/plugins/${encodeURIComponent(_removing)}`);
  });
  loadPlugins(true);
});

async function loadPlugins(attachJob = false) {
  let d;
  try {
    d = await apiJSON('GET', '/api/plugins/');
  } catch (e) {
    document.getElementById('plugins-list').innerHTML =
      `<div class="card"><p class="text-muted">Could not list the modules: ${esc(e.message)}</p></div>`;
    return;
  }
  _plugins = d.modules || [];
  _restartNeeded = !!d.restart_needed;
  document.getElementById('plugins-unsupported').hidden = d.supported !== false;
  renderPlugins(d.job);
  if (attachJob && d.job && d.job.state === 'running') {
    _jobNext = 0;
    showJob(d.job);
    pollJob();
  }
}

function pluginState(p) {
  if (p.installed && !p.loaded) return { badge: 'badge-warn', text: 'Restart the WebUI to use it' };
  if (!p.installed && p.loaded && p.package) return { badge: 'badge-warn', text: 'Removed: restart the WebUI' };
  if (p.installed && p.available && p.available !== p.installed) return { badge: 'badge-info', text: `Update available: ${p.available}` };
  if (p.installed || p.loaded) return { badge: 'badge-ok', text: 'Installed' };
  return { badge: 'badge-muted', text: 'Available' };
}

function renderPlugins(job) {
  const list = document.getElementById('plugins-list');
  const busy = job && job.state === 'running';
  document.getElementById('btn-plugins-refresh').disabled = busy;
  if (!_plugins.length) {
    list.innerHTML = `<div class="card"><div class="empty-state">
      <h3>No modules found</h3>
      <p>The repository offers no modules for this edition yet. Check again later with “Check for new modules”.</p>
    </div></div>`;
    return;
  }
  list.innerHTML = _plugins.map(p => {
    const id = esc(p.id);
    const state = pluginState(p);
    const version = p.installed || p.available;
    const actions = [];
    if (p.available && !p.installed) {
      actions.push(`<button class="btn btn-primary btn-sm" data-plugin="${id}" data-action="install" ${busy ? 'disabled' : ''}>Install</button>`);
    }
    if (p.installed) {
      actions.push(`<button class="btn btn-danger btn-sm advanced-only" data-plugin="${id}" data-action="remove" ${busy ? 'disabled' : ''}>Remove</button>`);
    }
    if (_restartNeeded && (p.installed !== null) !== p.loaded && p.package) {
      actions.push(`<button class="btn btn-secondary btn-sm" data-action="restart">Restart WebUI</button>`);
    }
    return `
    <div class="card">
      <div class="card-header">
        <span class="card-title">${id}</span>
        <span class="badge ${state.badge}">${esc(state.text)}</span>
      </div>
      <p class="text-sm" style="margin-bottom:10px">${esc(p.description) || '<span class="text-muted">No description</span>'}</p>
      <p class="text-sm text-muted" style="margin-bottom:12px">
        ${p.package ? `<span class="font-mono">${esc(p.package)}</span>` : 'Not installed from a package'}${version ? ` · ${esc(version)}` : ''}
      </p>
      <div class="d-flex gap-2">${actions.join('')}</div>
    </div>`;
  }).join('');
}

function onPluginAction(e) {
  const btn = e.target.closest('button[data-action]');
  if (!btn) return;
  const id = btn.dataset.plugin;
  if (btn.dataset.action === 'restart') {
    restartWebUI();
  } else if (btn.dataset.action === 'install') {
    startJob('POST', `/api/plugins/${encodeURIComponent(id)}/install`);
  } else if (btn.dataset.action === 'remove') {
    const p = _plugins.find(x => x.id === id);
    _removing = id;
    document.getElementById('plugin-remove-title').textContent = `Remove ${id}`;
    document.getElementById('plugin-remove-package').textContent = p?.package || id;
    openModal('modal-plugin-remove');
  }
}

async function startJob(method, path) {
  let d;
  try {
    d = await apiJSON(method, path);
  } catch (e) {
    toast(`Error: ${e.message}`, 'err', 6000);
    return;
  }
  _jobNext = 0;
  document.getElementById('plugins-job-output').replaceChildren();
  showJob(d.job);
  renderPlugins(d.job);
  pollJob();
}

function showJob(job) {
  document.getElementById('plugins-job').hidden = false;
  const what = JOB_TITLES[job.kind] || 'Package manager';
  document.getElementById('plugins-job-title').textContent = job.module ? `${what} ${job.module}` : what;
  const state = document.getElementById('plugins-job-state');
  state.textContent = job.state === 'running' ? 'running…'
    : job.state === 'done' ? 'finished' : `failed — ${job.error || ''}`;
}

function appendLines(lines) {
  const out = document.getElementById('plugins-job-output');
  const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
  for (const line of lines) {
    const div = document.createElement('div');
    div.textContent = line;
    if (line.startsWith('$ ')) div.className = 't-info';
    out.appendChild(div);
  }
  if (atBottom) out.scrollTop = out.scrollHeight;
}

async function pollJob() {
  clearTimeout(_jobTimer);
  let job;
  try {
    job = await apiJSON('GET', `/api/plugins/job?since=${_jobNext}`);
  } catch {
    _jobTimer = setTimeout(pollJob, 3000);
    return;
  }
  appendLines(job.lines);
  _jobNext = job.next;
  showJob(job);
  if (job.state === 'running') {
    _jobTimer = setTimeout(pollJob, 1000);
    return;
  }
  const out = document.getElementById('plugins-job-output');
  const end = document.createElement('div');
  end.className = job.state === 'done' ? 't-ok' : 't-err';
  end.textContent = job.state === 'done' ? 'Done.' : `Failed: ${job.error || ''}`;
  out.appendChild(end);
  out.scrollTop = out.scrollHeight;
  if (job.state === 'done' && job.kind === 'install') toast(`${job.module} is installed. Restart the WebUI to use it.`, 'ok', 8000);
  if (job.state === 'done' && job.kind === 'remove') toast(`${job.module} is removed. Restart the WebUI to finish.`, 'ok', 8000);
  await loadPlugins();
  Notices.refresh();
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
