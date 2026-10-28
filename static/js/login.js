/* login.html — kept in a file rather than inline so the Content-Security-Policy
   can forbid inline script (see _set_security_headers in app.py). */
/* ---------- Welcome screen -> Login form transition ---------- */
const welcomeScreen = document.getElementById('welcomeScreen');
const loginCard = document.getElementById('loginCard');

function showLoginCard() {
  welcomeScreen.classList.add('welcome-fade-out');
  setTimeout(() => {
    welcomeScreen.style.display = 'none';
    loginCard.style.display = '';
    requestAnimationFrame(() => loginCard.classList.add('login-fade-in'));
  }, 450);
}
document.getElementById('enterBtn').addEventListener('click', showLoginCard);
document.getElementById('backToWelcomeBtn').addEventListener('click', () => {
  loginCard.classList.remove('login-fade-in');
  loginCard.style.display = 'none';
  welcomeScreen.style.display = '';
  welcomeScreen.classList.remove('welcome-fade-out');
});

/* ---------- Staff / Student toggle ---------- */
const staffBtn = document.getElementById('staffModeBtn');
const studentBtn = document.getElementById('studentModeBtn');
const staffFields = document.getElementById('staffFields');
const studentFields = document.getElementById('studentFields');
let roleMode = 'staff';

staffBtn.addEventListener('click', () => {
  roleMode = 'staff';
  staffBtn.classList.add('active'); studentBtn.classList.remove('active');
  staffFields.style.display = ''; studentFields.style.display = 'none';
});
studentBtn.addEventListener('click', () => {
  roleMode = 'student';
  studentBtn.classList.add('active'); staffBtn.classList.remove('active');
  staffFields.style.display = 'none'; studentFields.style.display = '';
});

const pinEnterBlock = document.getElementById('pinEnterBlock');
const pinCreateBlock = document.getElementById('pinCreateBlock');

// If the student edits admission number or DOB after a PIN step was
// revealed, that PIN step was for whatever they'd typed before — hide
// it again so they don't submit a PIN against a different account.
['admissionNo', 'dobPassword'].forEach(id => {
  document.getElementById(id).addEventListener('input', () => {
    pinEnterBlock.style.display = 'none';
    pinCreateBlock.style.display = 'none';
    document.getElementById('forgotPinBlock').style.display = 'none';
    document.getElementById('loginInfo').style.display = 'none';
    document.getElementById('loginSubmitBtn').style.display = '';
  });
});

document.getElementById('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById('loginError');
  errorEl.textContent = '';

  const payload = { role_mode: roleMode };
  if (roleMode === 'staff') {
    payload.username = document.getElementById('username').value.trim();
    payload.password = document.getElementById('password').value;
  } else {
    payload.username = document.getElementById('admissionNo').value.trim();
    payload.password = document.getElementById('dobPassword').value.trim();
    if (pinEnterBlock.style.display !== 'none') {
      payload.pin = document.getElementById('studentPin').value.trim();
    }
    if (pinCreateBlock.style.display !== 'none') {
      payload.setup_code = document.getElementById('setupCode').value.trim();
      payload.new_pin = document.getElementById('newPin').value.trim();
      payload.confirm_pin = document.getElementById('confirmPin').value.trim();
    }
  }

  try {
    const res = await fetch('/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': document.body.dataset.csrf || '' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.error || 'Sign in failed.';
      // A wrong PIN shouldn't send the student back to square one —
      // keep the PIN box open, clear only the wrong PIN they typed,
      // and refocus it so they can just retype and hit Sign In again.
      if (data.need_pin) {
        pinEnterBlock.style.display = '';
        pinCreateBlock.style.display = 'none';
        const pinInput = document.getElementById('studentPin');
        pinInput.value = '';
        pinInput.focus();
      }
      // Same for the set-up step: keep it open so only the wrong part is retyped.
      if (data.need_setup_code) {
        pinCreateBlock.style.display = '';
        pinEnterBlock.style.display = 'none';
      }
      return;
    }

    if (data.need_setup_code) {
      pinCreateBlock.style.display = '';
      pinEnterBlock.style.display = 'none';
      document.getElementById('setupCode').focus();
      return;
    }
    if (data.need_pin) {
      pinEnterBlock.style.display = '';
      pinCreateBlock.style.display = 'none';
      document.getElementById('studentPin').focus();
      return;
    }
    window.location.href = data.must_change_password ? '/change-password' : '/';
  } catch (err) {
    errorEl.textContent = 'Could not reach the server. Please try again.';
  }
});

/* ---------- Coming back from a failed Microsoft sign-in ----------
   The server has already put the (fixed) message in #loginError; skip the
   welcome screen and show the Student tab so the student sees it. */
if (document.getElementById('loginError').textContent.trim()) {
  welcomeScreen.style.display = 'none';
  loginCard.style.display = '';
  loginCard.classList.add('login-fade-in');
  studentBtn.click();
}

/* ---------- Forgot PIN? (self-service request, admin/librarian approves) ---------- */
const forgotPinLink = document.getElementById('forgotPinLink');
const forgotPinBlock = document.getElementById('forgotPinBlock');
const loginInfo = document.getElementById('loginInfo');
const loginSubmitBtn = document.getElementById('loginSubmitBtn');

forgotPinLink.addEventListener('click', (e) => {
  e.preventDefault();
  pinEnterBlock.style.display = 'none';
  forgotPinBlock.style.display = '';
  loginSubmitBtn.style.display = 'none';
  document.getElementById('loginError').textContent = '';
});

document.getElementById('forgotPinCancel').addEventListener('click', () => {
  forgotPinBlock.style.display = 'none';
  pinEnterBlock.style.display = '';
  loginSubmitBtn.style.display = '';
  loginInfo.style.display = 'none';
});

document.getElementById('forgotPinSubmit').addEventListener('click', async () => {
  const errorEl = document.getElementById('loginError');
  errorEl.textContent = '';
  loginInfo.style.display = 'none';
  try {
    const res = await fetch('/api/pin-reset-request', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': document.body.dataset.csrf || '' },
      body: JSON.stringify({
        admission_no: document.getElementById('admissionNo').value.trim(),
        dob: document.getElementById('dobPassword').value.trim(),
      }),
    });
    const data = await res.json();
    if (!res.ok) { errorEl.textContent = data.error || 'Could not submit request.'; return; }
    loginInfo.textContent = data.message;
    loginInfo.style.display = '';
    forgotPinBlock.style.display = 'none';
  } catch (err) {
    errorEl.textContent = 'Could not reach the server. Please try again.';
  }
});
