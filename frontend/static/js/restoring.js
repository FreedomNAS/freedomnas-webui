'use strict';

const BADGES = {
  waiting: ['badge-muted', 'waiting'],
  running: ['badge-info', 'running'],
  done: ['badge-ok', 'done'],
  failed: ['badge-err', 'failed'],
};

function renderStep(step) {
  const li = document.createElement('li');
  li.style.margin = '4px 0';
  const [cls, word] = BADGES[step.state] || BADGES.waiting;
  const badge = document.createElement('span');
  badge.className = `badge ${cls}`;
  badge.textContent = word;
  badge.style.marginRight = '8px';
  li.append(badge, document.createTextNode(step.label));
  if (step.error) {
    const err = document.createElement('pre');
    err.style.cssText = 'white-space:pre-wrap;margin:4px 0 0;font-size:0.75rem;color:var(--err)';
    err.textContent = step.error;
    li.append(err);
  }
  return li;
}

function render(data) {
  const steps = data.steps || [];
  const ended = steps.filter((s) => s.state === 'done' || s.state === 'failed').length;
  const running = steps.findIndex((s) => s.state === 'running');
  const bar = document.getElementById('restore-bar');
  bar.style.width = steps.length ? `${Math.round((ended / steps.length) * 100)}%` : '0%';
  bar.className = `progress-bar${data.failed ? ' err' : ''}`;
  document.getElementById('restore-count').textContent = steps.length
    ? (running >= 0 ? `Step ${running + 1} of ${steps.length}: ${steps[running].label}` : `${ended} of ${steps.length} steps`)
    : 'Preparing the steps…';
  const list = document.getElementById('restore-steps');
  list.replaceChildren(...steps.map(renderStep));
  if (data.finished && data.failed) {
    document.getElementById('restore-title').textContent = 'Restore finished with errors';
    const shown = steps.map((s) => s.error).join('\n');
    document.getElementById('restore-errors').textContent =
      (data.errors || []).filter((e) => !shown.includes(e)).join('\n');
    document.getElementById('restore-failed').hidden = false;
    document.getElementById('btn-continue').hidden = false;
  }
}

async function poll() {
  try {
    const res = await fetch('/api/restore/progress', { cache: 'no-store' });
    const data = await res.json();
    if (!data.active) {
      window.location.replace('/');
      return;
    }
    if (data.restarting) {
      document.getElementById('restore-count').textContent = 'Restore done: starting the WebUI again…';
      document.getElementById('restore-bar').style.width = '100%';
      setTimeout(poll, 2000);
      return;
    }
    render(data);
    if (data.finished) return;
  } catch (_) {
    setTimeout(poll, 2000);
    return;
  }
  setTimeout(poll, 2000);
}

document.getElementById('btn-continue').addEventListener('click', async () => {
  const btn = document.getElementById('btn-continue');
  btn.disabled = true;
  try {
    await fetch('/api/restore/continue', { method: 'POST' });
  } finally {
    window.location.replace('/');
  }
});

poll();
