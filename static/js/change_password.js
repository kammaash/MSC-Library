/* change_password.html — kept in a file rather than inline so the Content-Security-Policy
   can forbid inline script (see _set_security_headers in app.py). */
document.getElementById('changePwForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById('changePwError');
  errorEl.textContent = '';

  const payload = {
    current_password: document.getElementById('currentPassword').value,
    new_password: document.getElementById('newPassword').value,
    confirm_password: document.getElementById('confirmNewPassword').value,
  };

  try {
    const res = await fetch('/api/account/change-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': document.body.dataset.csrf || '' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) { errorEl.textContent = data.error || 'Could not change password.'; return; }
    window.location.href = '/';
  } catch (err) {
    errorEl.textContent = 'Could not reach the server. Please try again.';
  }
});

document.getElementById('logoutBtn').addEventListener('click', async () => {
  try { await fetch('/logout', { method: 'POST', headers: { 'X-CSRF-Token': document.body.dataset.csrf || '' } }); } catch (err) {}
  window.location.href = '/login';
});
