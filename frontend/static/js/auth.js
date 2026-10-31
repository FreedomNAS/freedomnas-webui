'use strict';

const PASSWORD_EXPIRED = 'Password expired';

document.addEventListener('DOMContentLoaded', () => {
  const theme = localStorage.getItem('fnas_theme') || 'dark';
  document.documentElement.setAttribute('data-theme', theme);

  if (sessionStorage.getItem('fnas_auth')) {
    redirectAfterLogin();
    return;
  }

  const form  = document.getElementById('auth-form');
  const user  = document.getElementById('field-user');
  const pass  = document.getElementById('field-pass');
  const err   = document.getElementById('auth-error');
  const btn   = document.getElementById('btn-login');
  const change = document.getElementById('change-form');
  const changeErr = document.getElementById('change-error');
  const changeBtn = document.getElementById('btn-change');
  let temporary = null;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    err.hidden = true;
    setLoginLoading(true);

    const u = user.value.trim();
    const p = pass.value;
    if (!u || !p) {
      showError('Please enter username and password.');
      setLoginLoading(false);
      return;
    }

    try {
      await signIn(u, p);
    } catch (ex) {
      showError(`Connection error: ${ex.message}`);
    } finally {
      setLoginLoading(false);
    }
  });

  change.addEventListener('submit', async (e) => {
    e.preventDefault();
    changeErr.hidden = true;
    const fresh = document.getElementById('field-new').value;
    if (fresh !== document.getElementById('field-new2').value) {
      showChangeError('The new passwords differ.');
      return;
    }
    changeBtn.disabled = true;
    try {
      const res = await fetch('/api/account/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: temporary.user, password: temporary.pass, new_password: fresh }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        showChangeError(body.detail || `Error ${res.status}`);
        return;
      }
      await signIn(temporary.user, fresh);
    } catch (ex) {
      showChangeError(`Connection error: ${ex.message}`);
    } finally {
      changeBtn.disabled = false;
    }
  });

  async function signIn(u, p) {
    const headers = { 'Authorization': `Basic ${btoa(`${u}:${p}`)}` };
    let res = await fetch('/api/system/info', { headers });
    if (res.status === 503) res = await fetch('/api/setup/whoami', { headers });

    if (res.status === 401) {
      const body = await res.json().catch(() => ({}));
      if (body.detail === PASSWORD_EXPIRED) {
        askNewPassword(u, p);
        return;
      }
      showError('Invalid username or password.');
      pass.value = '';
      pass.focus();
      return;
    }

    if (res.status === 403) {
      res = await fetch('/api/account/me', { headers });
      if (res.ok) {
        remember(u, p);
        location.href = '/account/';
        return;
      }
      showError(`Unexpected response (${res.status}). Try again.`);
      return;
    }

    if (res.ok) {
      remember(u, p);
      redirectAfterLogin();
      return;
    }

    showError(`Unexpected response (${res.status}). Try again.`);
  }

  function askNewPassword(u, p) {
    temporary = { user: u, pass: p };
    document.getElementById('change-user').textContent = u;
    form.hidden = true;
    change.hidden = false;
    document.getElementById('field-new').focus();
  }

  function remember(u, p) {
    sessionStorage.setItem('fnas_auth', btoa(`${u}:${p}`));
    sessionStorage.setItem('fnas_user', u);
  }

  function showError(msg) {
    err.textContent = msg;
    err.hidden = false;
  }

  function showChangeError(msg) {
    changeErr.textContent = msg;
    changeErr.hidden = false;
  }

  function setLoginLoading(on) {
    btn.disabled = on;
    btn.textContent = on ? 'Signing in…' : 'Sign In';
  }

  user.focus();
});

async function redirectAfterLogin() {
  const params = new URLSearchParams(location.search);
  let next = params.get('next') || '/dashboard/';

  try {
    const res = await fetch('/api/setup/status');
    const d = await res.json();
    if (!d.initialized) next = '/setup/';
  } catch { /* go to dashboard anyway */ }

  location.href = next;
}
