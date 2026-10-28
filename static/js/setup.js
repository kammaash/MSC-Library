/* setup.html — kept in a file rather than inline so the Content-Security-Policy
   can forbid inline script (see _set_security_headers in app.py). */
document.getElementById('setupForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById('setupError');
  errorEl.textContent = '';

  const logoFile = document.getElementById('schoolLogo').files[0];
  if (logoFile && logoFile.size > 5 * 1024 * 1024) {
    errorEl.textContent = 'Logo file is too large — please use one under 5 MB.';
    return;
  }

  const formData = new FormData();
  formData.append('setup_token', document.getElementById('setupToken').value);
  formData.append('school_name', document.getElementById('schoolName').value.trim());
  formData.append('school_address', document.getElementById('schoolAddress').value.trim());
  formData.append('admin_name', document.getElementById('adminName').value.trim());
  formData.append('admin_username', document.getElementById('adminUsername').value.trim());
  formData.append('admin_password', document.getElementById('adminPassword').value);
  formData.append('confirm_password', document.getElementById('confirmPassword').value);
  if (logoFile) formData.append('school_logo', logoFile);

  try {
    const res = await fetch('/setup', {
      method: 'POST',
      body: formData,
      headers: { 'X-CSRF-Token': document.body.dataset.csrf || '' },
    });
    const data = await res.json();
    if (!res.ok) { errorEl.textContent = data.error || 'Setup failed. Please try again.'; return; }
    window.location.href = '/login';
  } catch (err) {
    errorEl.textContent = 'Could not reach the server. Please try again.';
  }
});
