'use strict';

document.getElementById('password-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const error = document.getElementById('password-error');
  const done = document.getElementById('password-done');
  const btn = document.getElementById('btn-change');
  const username = document.getElementById('field-user').value.trim();
  const password = document.getElementById('field-current').value;
  const fresh = document.getElementById('field-new').value;
  error.hidden = true;
  done.hidden = true;
  if (fresh !== document.getElementById('field-new2').value) {
    error.textContent = 'The new passwords differ.';
    error.hidden = false;
    return;
  }
  btn.disabled = true;
  try {
    const res = await fetch('/api/account/password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, new_password: fresh }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `Error ${res.status}`);
    e.target.reset();
    done.textContent = `The password of ${username} is changed.`;
    done.hidden = false;
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    btn.disabled = false;
  }
});
