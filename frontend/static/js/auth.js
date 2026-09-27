'use strict';

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
      const headers = { 'Authorization': `Basic ${btoa(`${u}:${p}`)}` };
      let res = await fetch('/api/system/info', { headers });
      if (res.status === 503) res = await fetch('/api/setup/whoami', { headers });

      if (res.status === 401) {
        showError('Invalid username or password.');
        pass.value = '';
        pass.focus();
        return;
      }

      if (res.status === 403) {
        showError('This account is not an administrator.');
        pass.value = '';
        return;
      }

      if (res.ok) {
        sessionStorage.setItem('fnas_auth', btoa(`${u}:${p}`));
        sessionStorage.setItem('fnas_user', u);
        redirectAfterLogin();
        return;
      }

      showError(`Unexpected response (${res.status}). Try again.`);
    } catch (ex) {
      showError(`Connection error: ${ex.message}`);
    } finally {
      setLoginLoading(false);
    }
  });

  function showError(msg) {
    err.textContent = msg;
    err.hidden = false;
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
