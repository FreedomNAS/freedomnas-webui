'use strict';

let _ws     = null;
let _running = false;

document.addEventListener('DOMContentLoaded', async () => {
  if (!initShared()) return;

  await loadUpdateStatus();

  document.getElementById('btn-run-updates').addEventListener('click', runUpdates);
  document.getElementById('btn-configure').addEventListener('click', () => openModal('modal-configure'));
  bindModalClose('modal-configure');

  document.getElementById('form-configure').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn   = document.getElementById('btn-cfg-submit');
    const steps = Array.from(document.querySelectorAll('.f-step:checked')).map(c => c.value);
    const snap  = document.getElementById('f-auto-snapshot').checked;
    setLoading(btn, true, 'Saving…');
    try {
      await apiJSON('POST', '/api/updates/configure', { enabled_steps: steps, auto_snapshot: snap });
      toast('Update configuration saved.', 'ok');
      closeModal('modal-configure');
      await loadUpdateStatus();
    } catch (e) {
      toast(`Error: ${e.message}`, 'err');
    } finally {
      setLoading(btn, false);
    }
  });
});

async function loadUpdateStatus() {
  try {
    const d = await apiJSON('GET', '/api/updates/status');
    const count = d.pending_updates ?? 0;

    const countEl = document.getElementById('update-count');
    if (countEl) countEl.textContent = count;

    const msgEl = document.getElementById('update-msg');
    if (msgEl) {
      if (count === 0) {
        msgEl.className = 'badge badge-ok';
        msgEl.textContent = 'System is up to date';
      } else {
        msgEl.className = 'badge badge-warn';
        msgEl.textContent = `${count} package${count !== 1 ? 's' : ''} pending`;
      }
    }

    const snapEl = document.getElementById('f-auto-snapshot');
    if (snapEl) snapEl.checked = d.auto_snapshot !== false;
  } catch (e) {
    toast(`Could not check updates: ${e.message}`, 'warn');
  }
}

function runUpdates() {
  if (_running) return;
  _running = true;

  const btn     = document.getElementById('btn-run-updates');
  const termEl  = document.getElementById('terminal-body');
  const termWrap = document.getElementById('terminal-wrap');

  btn.disabled = true;
  btn.textContent = 'Updating…';
  termWrap.hidden = false;
  termEl.innerHTML = '<span class="t-info">Connecting…</span>\n';

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  _ws = new WebSocket(`${proto}://${location.host}/ws/updates/terminal`);

  _ws.onopen = () => {
    appendLine(termEl, 'info', '▶ Update process started');
  };

  _ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }

    if (msg.exit_code !== undefined) {
      const ok = msg.exit_code === 0;
      appendLine(termEl, ok ? 'ok' : 'err',
        ok ? `\n✓ Update completed successfully (exit 0)` : `\n✗ Update exited with code ${msg.exit_code}`);
      done(ok);
      return;
    }

    if (msg.line !== undefined) {
      const cls = msg.stream === 'stderr' ? 'err' : classifyLine(msg.line);
      appendLine(termEl, cls, msg.line);
    }
  };

  _ws.onerror = () => {
    appendLine(termEl, 'err', '✗ WebSocket connection error');
    done(false);
  };

  _ws.onclose = () => {
    if (_running) {
      appendLine(termEl, 'warn', 'Connection closed unexpectedly');
      done(false);
    }
  };

  function done(ok) {
    _running = false;
    _ws = null;
    btn.disabled = false;
    btn.textContent = 'Run Updates';
    if (ok) loadUpdateStatus();
  }
}

function appendLine(el, cls, text) {
  const span = document.createElement('span');
  span.className = `t-${cls}`;
  span.textContent = text + '\n';
  el.appendChild(span);
  el.scrollTop = el.scrollHeight;
}

function classifyLine(line) {
  const l = line.toLowerCase();
  if (l.includes('error') || l.includes('failed')) return 'err';
  if (l.includes('warn'))  return 'warn';
  if (l.includes('✓') || l.includes('success') || l.includes('done') || l.includes('up to date')) return 'ok';
  if (l.startsWith('==>') || l.startsWith('--') || l.startsWith('[')) return 'info';
  return '';
}
