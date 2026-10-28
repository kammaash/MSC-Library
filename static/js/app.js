const ROLE = document.body.dataset.role;
const IS_ADMIN = ROLE === 'admin';
const IS_STAFF = ROLE === 'admin' || ROLE === 'librarian';
const IS_STUDENT = ROLE === 'student';
const IS_VIEWER = ROLE === 'viewer';

document.getElementById('todayDate').textContent = new Date().toLocaleDateString('en-IN', {
  weekday: 'long', day: '2-digit', month: 'long', year: 'numeric'
});

/* ---------- role-based nav visibility ---------- */
document.querySelectorAll('.tab.role-staff').forEach(t => { if (!IS_STAFF && !IS_VIEWER) t.style.display = 'none'; });
document.querySelectorAll('.tab.role-admin').forEach(t => { if (!IS_ADMIN) t.style.display = 'none'; });
document.querySelectorAll('.tab.role-student').forEach(t => { if (!IS_STUDENT) t.style.display = 'none'; });
if (IS_VIEWER) {
  // viewer: only Home, Online Books, Reports
  document.querySelectorAll('.tab').forEach(t => {
    const v = t.dataset.view;
    t.style.display = ['home', 'online', 'reports'].includes(v) ? '' : 'none';
  });
}
// Students see Home, Digital Library, My Library, and Achievements —
// everything else (Catalog, Members, Issue & Return, Registers, Reports,
// Manage Users) is staff-only and already hidden by .role-staff/.role-admin
// above; nothing further to restrict here.

/* ---------- tab switching ---------- */
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    if (tab.style.display === 'none') return;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('view-' + tab.dataset.view).classList.add('active');
    loadView(tab.dataset.view);
  });
});

// Digital Library now has its own dedicated button in the header (separate
// from the regular menu, per request) instead of living in the tab row.
document.getElementById('digitalLibraryQuickBtn').addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-online').classList.add('active');
  loadView('online');
});

function toast(msg, bad = false) {
  const t = document.getElementById('toast');
  t.textContent = msg + (bad ? ' (tap to dismiss)' : '');
  t.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(t._hideTimer);
  t._hideTimer = setTimeout(() => t.className = 'toast' + (bad ? ' bad' : ''), bad ? 12000 : 2400);
  t.onclick = () => { clearTimeout(t._hideTimer); t.className = 'toast' + (bad ? ' bad' : ''); };
}

async function api(path, opts = {}) {
  opts.headers = Object.assign(
    { 'Content-Type': 'application/json', 'X-CSRF-Token': document.body.dataset.csrf || '' },
    opts.headers || {}
  );
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-json (file download) */ }
  if (!res.ok) {
    throw new Error((data && data.error) || 'Request failed');
  }
  return data;
}

document.getElementById('logoutBtn').addEventListener('click', async () => {
  await api('/logout', { method: 'POST' });
  window.location.href = '/login';
});

/* ---------- modal helpers ---------- */
function showModal(html) {
  document.getElementById('modalBody').innerHTML = html;
  document.getElementById('modalOverlay').classList.add('show');
}
function closeModal() { document.getElementById('modalOverlay').classList.remove('show'); }
document.getElementById('modalOverlay').addEventListener('click', e => {
  if (e.target.id === 'modalOverlay') closeModal();
});

function escapeHtml(s) {
  return String(s ?? '').replace(/[<>&'"]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', "'": '&#39;', '"': '&quot;' }[c]));
}

/**
 * Uploads a FormData body with a real progress callback — fetch() has no
 * way to report upload progress, which matters a lot for large files
 * (a 400 MB video can take several minutes on real school Wi-Fi); without
 * any visible progress, it's very easy to assume the page has frozen and
 * navigate away mid-upload, which cancels it and looks exactly like "the
 * file never made it in."
 */
function xhrUpload(url, method, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, url);
    xhr.setRequestHeader('X-CSRF-Token', document.body.dataset.csrf || '');
    xhr.upload.addEventListener('progress', e => {
      if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100));
    });
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (parseErr) { /* non-JSON response */ }
      resolve({ status: xhr.status, ok: xhr.status >= 200 && xhr.status < 300, data });
    };
    xhr.onerror = () => reject(new Error('Could not reach the server — check the network connection and try again.'));
    xhr.ontimeout = () => reject(new Error('The upload timed out. On a slow connection, try again closer to the server/router.'));
    xhr.timeout = 30 * 60 * 1000; // 30 minutes — generous for a large file on slow Wi-Fi
    xhr.send(formData);
  });
}
function statTag(status) { return `<span class="tag ${status}">${status}</span>`; }

/* ---------- date helpers: DB/backend always uses ISO (yyyy-mm-dd);
   the UI shows/accepts dd-mm-yyyy so staff can't misread a US-style date ---------- */
function isoToDDMMYYYY(iso) {
  if (!iso) return '';
  const parts = iso.split('-');
  if (parts.length !== 3) return '';
  const [y, m, d] = parts;
  return `${d}-${m}-${y}`;
}
function ddmmyyyyToISO(s) {
  if (!s) return '';
  const m = s.trim().match(/^(\d{1,2})-(\d{1,2})-(\d{4})$/);
  if (!m) return null; // null = invalid format (caller should show an error)
  const [, d, mo, y] = m;
  const iso = `${y}-${mo.padStart(2, '0')}-${d.padStart(2, '0')}`;
  const check = new Date(iso);
  if (isNaN(check.getTime())) return null;
  return iso;
}

/* ---------- view loader ---------- */
function loadView(view) {
  if (view === 'home') loadHome();
  if (view === 'dashboard') loadDashboard();
  if (view === 'books') loadBooks();
  if (view === 'members') loadMembers();
  if (view === 'circulation') loadCirculation();
  if (view === 'online') { loadOnlineBooks(); if (IS_STUDENT) trackDigitalLibraryVisit(); }
  if (view === 'qrlibrary') loadQrLibrary();
  if (view === 'registers') loadRegister();
  if (view === 'users') loadUsers();
  if (view === 'mylibrary') loadMyLibrary();
  if (view === 'achievements') loadAchievements();
}

/* =====================================================================
   HOME
===================================================================== */
async function loadHome() {
  try {
    const d = await api('/api/dashboard');
    document.getElementById('homeStats').innerHTML = [
      { n: d.total_books, l: 'Total Books' },
      { n: d.distinct_titles, l: 'Distinct Titles' },
      { n: d.members, l: 'Registered Members' },
      { n: d.issued, l: 'Currently Issued' },
      { n: d.overdue, l: 'Overdue Right Now', alert: d.overdue > 0 },
    ].map(s => `<div class="stat-card ${s.alert ? 'alert' : ''}"><div class="stat-num">${s.n}</div><div class="stat-lbl">${s.l}</div></div>`).join('');
  } catch (e) { /* viewer/student may not have dashboard access; ignore */ }

  try {
    const books = await api('/api/books');
    const picks = [...books].sort(() => Math.random() - 0.5).slice(0, 4);
    document.getElementById('homeCatalogHighlights').innerHTML = picks.map(b => `
      <div class="online-card">
        <h4>📗 ${escapeHtml(b.title)}</h4>
        <p>${escapeHtml(b.author || '')}${b.author ? ' · ' : ''}${escapeHtml(b.subject || '')}<br>
          <span class="hint">${statTag(b.status)} · Rack ${escapeHtml(b.rack_no || '—')}</span></p>
      </div>`).join('') || `<p class="hint">No books in the catalog yet.</p>`;
  } catch (e) { document.getElementById('homeCatalogHighlights').innerHTML = ''; }

  try {
    const ebooks = await api('/api/online-books');
    const picks = [...ebooks].slice(-4).reverse();
    document.getElementById('homeDigitalHighlights').innerHTML = picks.map(b => `
      <div class="online-card">
        <h4>${FILE_TYPE_ICON[b.file_type] || '📁'} ${escapeHtml(b.title)}</h4>
        <p>${escapeHtml(b.author || '')}${b.author ? ' · ' : ''}${escapeHtml(b.subject || '')}<br>
          <span class="hint">${FILE_TYPE_LABELS[b.file_type] || (b.file_type || '').toUpperCase()}</span></p>
      </div>`).join('') || `<p class="hint">Nothing in the Digital Library yet — check back soon.</p>`;
  } catch (e) { document.getElementById('homeDigitalHighlights').innerHTML = ''; }
}

/* =====================================================================
   DASHBOARD
===================================================================== */
let subjectChartObj, monthlyChartObj;
async function loadDashboard() {
  const d = await api('/api/dashboard');
  document.getElementById('dashStats').innerHTML = [
    { n: d.total_books, l: 'Total Books' },
    { n: d.distinct_titles, l: 'Distinct Titles' },
    { n: d.members, l: 'Members' },
    { n: d.issued, l: 'Issued' },
    { n: d.overdue, l: 'Overdue', alert: d.overdue > 0 },
    { n: d.lost_books, l: 'Lost Books' },
    { n: '₹' + d.fine_due, l: 'Fine Due' },
    { n: '₹' + d.fine_collected, l: 'Fine Collected' },
  ].map(s => `<div class="stat-card ${s.alert ? 'alert' : ''}"><div class="stat-num">${s.n}</div><div class="stat-lbl">${s.l}</div></div>`).join('');

  document.getElementById('stockSummary').innerHTML = [
    { n: d.total_books, l: 'Total Copies Owned' },
    { n: d.available_now, l: 'Present on Shelf (Available)' },
    { n: d.issued, l: 'Currently Issued' },
    { n: d.total_returns_ever, l: 'Total Returns (all-time)' },
    { n: d.lost_books, l: 'Lost', alert: d.lost_books > 0 },
  ].map(s => `<div class="stat-card ${s.alert ? 'alert' : ''}"><div class="stat-num">${s.n}</div><div class="stat-lbl">${s.l}</div></div>`).join('');

  const subjCtx = document.getElementById('subjectChart');
  const subjLabels = Object.keys(d.subject_counts);
  const subjData = Object.values(d.subject_counts);
  if (subjectChartObj) subjectChartObj.destroy();
  subjectChartObj = new Chart(subjCtx, {
    type: 'pie',
    data: { labels: subjLabels, datasets: [{ data: subjData, backgroundColor: ['#2456C4', '#1D8A5E', '#B8860B', '#6E2A35', '#7A4FBF', '#C4502B', '#1B6E8C'] }] },
  });

  const monthLabels = Object.keys(d.monthly_issues).sort();
  const monthData = monthLabels.map(k => d.monthly_issues[k]);
  if (monthlyChartObj) monthlyChartObj.destroy();
  monthlyChartObj = new Chart(document.getElementById('monthlyChart'), {
    type: 'bar',
    data: { labels: monthLabels, datasets: [{ label: 'Books Issued', data: monthData, backgroundColor: '#2456C4' }] },
    options: { plugins: { legend: { display: false } } },
  });

  document.getElementById('topBooksBody').innerHTML = d.top_books.map(b =>
    `<tr><td>${escapeHtml(b.title)}</td><td>${b.count}</td></tr>`).join('') ||
    `<tr><td colspan="2" style="text-align:center;color:#9a9180;">No issue history yet.</td></tr>`;
}

/* =====================================================================
   CATALOG
===================================================================== */
async function loadBooks() {
  const q = document.getElementById('bookSearch').value;
  const books = await api('/api/books?q=' + encodeURIComponent(q));
  document.getElementById('booksBody').innerHTML = books.map(b => `
    <tr>
      <td>${b.accession_no}</td>
      <td><strong>${escapeHtml(b.title)}</strong></td>
      <td>${escapeHtml(b.author || '')}</td>
      <td>${escapeHtml(b.subject || '')}</td>
      <td>${escapeHtml(b.rack_no || '')}</td>
      <td>${b.copies}</td>
      <td>${statTag(b.status)}</td>
      <td>${b.barcode_path ? `<a href="/${b.barcode_path}" target="_blank">View</a>` : '—'}</td>
      <td>
        <button class="ghost" data-edit-book="${b.id}">Edit</button>
        ${IS_STAFF ? `<button class="danger" data-del-book="${b.id}">Delete</button>` : ''}
      </td>
    </tr>`).join('') || `<tr><td colspan="9" style="text-align:center;color:#9a9180;padding:20px;">No books yet.</td></tr>`;
  refreshIncompleteBadge();
}
document.getElementById('bookSearch').addEventListener('input', loadBooks);

function bookForm(existing) {
  const b = existing || {};
  return `
    <h3>${existing ? 'Edit Book' : 'Add a Book'}</h3>
    <form id="bookForm">
      <div class="field-grid">
        <div class="field full"><label>Title *</label><input id="f_title" required value="${escapeHtml(b.title || '')}"></div>
        <div class="field"><label>Author</label><input id="f_author" value="${escapeHtml(b.author || '')}"></div>
        <div class="field"><label>ISBN</label><input id="f_isbn" value="${escapeHtml(b.isbn || '')}"></div>
        <div class="field"><label>Publisher</label><input id="f_publisher" value="${escapeHtml(b.publisher || '')}"></div>
        <div class="field"><label>Edition</label><input id="f_edition" value="${escapeHtml(b.edition || '')}"></div>
        <div class="field"><label>Subject</label><input id="f_subject" value="${escapeHtml(b.subject || '')}"></div>
        <div class="field"><label>Class</label><input id="f_class" value="${escapeHtml(b.class_name || '')}"></div>
        <div class="field"><label>Rack No.</label><input id="f_rack" value="${escapeHtml(b.rack_no || '')}"></div>
        <div class="field"><label>Copies *</label><input type="number" id="f_copies" min="1" required value="${b.copies || 1}"></div>
        ${existing ? `<div class="field"><label>Status</label>
          <select id="f_status">
            ${['Available', 'Lost', 'Damaged'].map(s => `<option ${b.status === s ? 'selected' : ''}>${s}</option>`).join('')}
          </select></div>` : ''}
      </div>
      <div class="modal-actions">
        <button type="button" class="ghost" onclick="closeModal()">Cancel</button>
        <button type="submit" class="brass">${existing ? 'Save Changes' : 'Add Book'}</button>
      </div>
    </form>`;
}

document.getElementById('addBookBtn').addEventListener('click', () => {
  showModal(bookForm(null));
  document.getElementById('bookForm').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      await api('/api/books', {
        method: 'POST', body: JSON.stringify({
          title: document.getElementById('f_title').value,
          author: document.getElementById('f_author').value,
          isbn: document.getElementById('f_isbn').value,
          publisher: document.getElementById('f_publisher').value,
          edition: document.getElementById('f_edition').value,
          subject: document.getElementById('f_subject').value,
          class_name: document.getElementById('f_class').value,
          rack_no: document.getElementById('f_rack').value,
          copies: document.getElementById('f_copies').value,
        })
      });
      toast('Book added.'); closeModal(); loadBooks();
    } catch (err) { toast(err.message, true); }
  });
});

document.getElementById('booksBody').addEventListener('click', async e => {
  const editId = e.target.getAttribute('data-edit-book');
  const delId = e.target.getAttribute('data-del-book');
  if (editId) {
    const books = await api('/api/books');
    const book = books.find(b => String(b.id) === editId);
    showModal(bookForm(book));
    document.getElementById('bookForm').addEventListener('submit', async ev => {
      ev.preventDefault();
      try {
        await api(`/api/books/${editId}`, {
          method: 'PUT', body: JSON.stringify({
            title: document.getElementById('f_title').value,
            author: document.getElementById('f_author').value,
            isbn: document.getElementById('f_isbn').value,
            publisher: document.getElementById('f_publisher').value,
            edition: document.getElementById('f_edition').value,
            subject: document.getElementById('f_subject').value,
            class_name: document.getElementById('f_class').value,
            rack_no: document.getElementById('f_rack').value,
            copies: document.getElementById('f_copies').value,
            status: document.getElementById('f_status') ? document.getElementById('f_status').value : 'Available',
          })
        });
        toast('Book updated.'); closeModal(); loadBooks();
      } catch (err) { toast(err.message, true); }
    });
  }
  if (delId) {
    if (!confirm('Delete this book?')) return;
    try { await api(`/api/books/${delId}`, { method: 'DELETE' }); toast('Book deleted.'); loadBooks(); }
    catch (err) { toast(err.message, true); }
  }
});

document.getElementById('exportBooksBtn').addEventListener('click', () => window.open('/api/export/books'));

document.getElementById('findDuplicatesBtn').addEventListener('click', async () => {
  const btn = document.getElementById('findDuplicatesBtn');
  const resultEl = document.getElementById('duplicatesResult');
  btn.disabled = true; btn.textContent = 'Scanning...';
  try {
    const groups = await api('/api/books/duplicate-titles');
    if (!groups.length) {
      resultEl.innerHTML = `<p class="hint">No duplicate titles found — every title in your catalog has a single Accession Number.</p>`;
    } else {
      resultEl.innerHTML = `<p class="hint">${groups.length} title(s) appear on more than one Accession Number:</p>` +
        groups.map(g => `
          <div class="online-card" style="margin-bottom:10px;">
            <h4>${escapeHtml(g.title)} <span class="hint">(${g.count} entries, ${g.total_copies} total copies)</span></h4>
            <div class="table-wrap"><table>
              <thead><tr><th>Accession No</th><th>Author</th><th>Rack</th><th>Copies</th></tr></thead>
              <tbody>${g.entries.map(e => `<tr><td>${escapeHtml(e.accession_no)}</td><td>${escapeHtml(e.author || '—')}</td><td>${escapeHtml(e.rack_no || '—')}</td><td>${e.copies}</td></tr>`).join('')}</tbody>
            </table></div>
          </div>`).join('');
    }
  } catch (err) { toast(err.message, true); }
  btn.disabled = false; btn.textContent = 'Scan Catalog for Duplicates';
});
document.getElementById('importBooksBtn').addEventListener('click', () => document.getElementById('importBooksInput').click());
document.getElementById('importBooksInput').addEventListener('change', async e => {
  const file = e.target.files[0]; if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  try {
    const res = await fetch('/api/import/books', { method: 'POST', body: fd, headers: { 'X-CSRF-Token': document.body.dataset.csrf || '' } });
    let data = null;
    try { data = await res.json(); } catch (parseErr) { /* fall through to generic message below */ }
    if (!res.ok) {
      toast((data && data.error) || `Import failed (server returned ${res.status}).`, true);
    } else {
      const bits = [`Added ${data.created} of ${data.total_rows} book(s) from "${data.sheet_used}"`];
      if (data.skipped_duplicate) bits.push(`${data.skipped_duplicate} already existed`);
      if (data.skipped_blank) bits.push(`${data.skipped_blank} had no title`);
      if (data.skipped_error) bits.push(`${data.skipped_error} row(s) had an error`);
      toast(bits.join(' — ') + '.');
      if (data.first_error) console.warn('First import row error:', data.first_error);
      loadBooks();
      refreshIncompleteBadge();
    }
  } catch (err) { toast('Import failed — could not reach the server. Please try again.', true); }
  e.target.value = '';
});

/* ---------- Remove incomplete/blank catalog entries (from a failed past import) ---------- */
async function refreshIncompleteBadge() {
  const badge = document.getElementById('incompleteBadge');
  if (!badge || !IS_ADMIN) { if (badge) badge.style.display = 'none'; return; }
  try {
    const info = await api('/api/books/incomplete-count');
    if (info.incomplete > 0) {
      badge.textContent = `Clean Up ${info.incomplete} Incomplete`;
      badge.style.display = 'inline-block';
    } else {
      badge.style.display = 'none';
    }
  } catch (e) { badge.style.display = 'none'; }
}

document.getElementById('incompleteBadge')?.addEventListener('click', async () => {
  try {
    const info = await api('/api/books/incomplete-count');
    if (!info.incomplete) { toast('No incomplete entries found.'); return; }
    if (!confirm(`Remove ${info.incomplete} catalog entr${info.incomplete === 1 ? 'y' : 'ies'} with no title/author (e.g. from a failed import)? Any book with real loan history is kept regardless.`)) return;
    const result = await api('/api/books/purge-incomplete', { method: 'DELETE' });
    let msg = `Removed ${result.removed} incomplete entr${result.removed === 1 ? 'y' : 'ies'}.`;
    if (result.skipped_has_history) msg += ` ${result.skipped_has_history} were kept because they have loan history.`;
    toast(msg);
    loadBooks(); refreshIncompleteBadge();
  } catch (err) { toast(err.message, true); }
});

/* =====================================================================
   MEMBERS
===================================================================== */
async function loadMembers() {
  const q = document.getElementById('memberSearch').value;
  const members = await api('/api/members?q=' + encodeURIComponent(q));
  document.getElementById('membersBody').innerHTML = members.map(m => `
    <tr>
      <td>${m.member_id}</td>
      <td><strong>${escapeHtml(m.name)}</strong></td>
      <td>${m.type}</td>
      <td>${escapeHtml(m.class_dept || '')}</td>
      <td>${escapeHtml(m.admission_no || '—')}</td>
      <td>${m.dob ? isoToDDMMYYYY(m.dob) : '—'}</td>
      <td>${m.type === 'Student' ? (m.pin_set ? 'Set' : '—') : '—'}</td>
      <td>${m.active_loans}</td>
      <td>${m.qr_path ? `<a href="/${m.qr_path}" target="_blank">View</a>` : '—'}</td>
      <td>
        <button class="ghost" data-edit-member="${m.id}">Edit</button>
        ${IS_STAFF && m.type === 'Student' && m.pin_set ? `<button class="ghost" data-reset-pin="${m.id}" title="Clear this student's PIN — they'll set a new one on next login">Reset PIN</button>` : ''}
        <button class="danger" data-del-member="${m.id}">Remove</button>
      </td>
    </tr>`).join('') || `<tr><td colspan="9" style="text-align:center;color:#9a9180;padding:20px;">No members yet.</td></tr>`;
  refreshPinRequestsBadge();
}
document.getElementById('memberSearch').addEventListener('input', loadMembers);

/* ---------- "Forgot PIN" requests waiting for admin/librarian approval ---------- */
async function refreshPinRequestsBadge() {
  const badge = document.getElementById('pinRequestsBadge');
  if (!badge || !IS_STAFF) { if (badge) badge.style.display = 'none'; return; }
  try {
    const requests = await api('/api/pin-reset-requests');
    if (requests.length > 0) {
      badge.textContent = `${requests.length} PIN Reset Request${requests.length !== 1 ? 's' : ''}`;
      badge.style.display = 'inline-block';
    } else {
      badge.style.display = 'none';
    }
  } catch (e) { badge.style.display = 'none'; }
}

document.getElementById('pinRequestsBadge')?.addEventListener('click', async () => {
  try {
    const requests = await api('/api/pin-reset-requests');
    const rows = requests.map(r => `
      <tr>
        <td>${escapeHtml(r.member_name)}</td>
        <td>${escapeHtml(r.class_dept || '')}</td>
        <td>${escapeHtml(r.admission_no || '')}</td>
        <td>${r.requested_at || ''}</td>
        <td>
          <button class="brass" data-approve-pin-req="${r.id}">Approve</button>
          <button class="ghost" data-deny-pin-req="${r.id}">Deny</button>
        </td>
      </tr>`).join('') || `<tr><td colspan="5" style="text-align:center;color:#9a9180;padding:20px;">No pending requests.</td></tr>`;
    showModal(`
      <h3>PIN Reset Requests</h3>
      <p class="login-hint" style="margin-top:0;">Only approve once you've verified who's asking (in person, or by calling the parent number on file) — approving clears their PIN and they'll set a new one on next login.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Name</th><th>Class/Dept</th><th>Admission No.</th><th>Requested</th><th></th></tr></thead>
        <tbody id="pinReqBody">${rows}</tbody>
      </table></div>
    `);
    document.getElementById('pinReqBody').addEventListener('click', async e => {
      const approveId = e.target.getAttribute('data-approve-pin-req');
      const denyId = e.target.getAttribute('data-deny-pin-req');
      if (approveId) {
        try {
          await api(`/api/pin-reset-requests/${approveId}/approve`, { method: 'POST' });
          toast('PIN cleared — student can set a new one on next login.');
          closeModal(); refreshPinRequestsBadge(); loadMembers();
        } catch (err) { toast(err.message, true); }
      }
      if (denyId) {
        try {
          await api(`/api/pin-reset-requests/${denyId}/deny`, { method: 'POST' });
          toast('Request denied.');
          closeModal(); refreshPinRequestsBadge();
        } catch (err) { toast(err.message, true); }
      }
    });
  } catch (err) { toast(err.message, true); }
});

function memberForm(existing) {
  const m = existing || {};
  return `
    <h3>${existing ? 'Edit Member' : 'Add a Member'}</h3>
    <form id="memberForm">
      <div class="field-grid">
        <div class="field full"><label>Full Name *</label><input id="m_name" required value="${escapeHtml(m.name || '')}"></div>
        <div class="field"><label>Type *</label>
          <select id="m_type">
            <option ${m.type === 'Student' ? 'selected' : ''}>Student</option>
            <option ${m.type === 'Staff' ? 'selected' : ''}>Staff</option>
          </select>
        </div>
        <div class="field"><label>Class / Dept.</label><input id="m_class" value="${escapeHtml(m.class_dept || '')}"></div>
        <div class="field"><label>Admission No. <span class="hint">(student login ID)</span></label><input id="m_admission" value="${escapeHtml(m.admission_no || '')}"></div>
        <div class="field"><label>Date of Birth <span class="hint">(student login password, format DD-MM-YYYY)</span></label><input type="text" id="m_dob" placeholder="DD-MM-YYYY" pattern="\\d{2}-\\d{2}-\\d{4}" value="${isoToDDMMYYYY(m.dob || '')}"></div>
        <div class="field"><label>Phone</label><input id="m_phone" value="${escapeHtml(m.phone || '')}"></div>
        <div class="field"><label>Parent Phone</label><input id="m_pphone" value="${escapeHtml(m.parent_phone || '')}"></div>
      </div>
      <div class="modal-actions">
        <button type="button" class="ghost" onclick="closeModal()">Cancel</button>
        <button type="submit" class="brass">${existing ? 'Save Changes' : 'Add Member'}</button>
      </div>
    </form>`;
}

document.getElementById('addMemberBtn').addEventListener('click', () => {
  showModal(memberForm(null));
  document.getElementById('memberForm').addEventListener('submit', async e => {
    e.preventDefault();
    const dobIso = ddmmyyyyToISO(document.getElementById('m_dob').value);
    if (dobIso === null) { toast('Date of Birth must be in DD-MM-YYYY format.', true); return; }
    try {
      await api('/api/members', {
        method: 'POST', body: JSON.stringify({
          name: document.getElementById('m_name').value,
          type: document.getElementById('m_type').value,
          class_dept: document.getElementById('m_class').value,
          admission_no: document.getElementById('m_admission').value,
          dob: dobIso,
          phone: document.getElementById('m_phone').value,
          parent_phone: document.getElementById('m_pphone').value,
        })
      });
      toast('Member added.'); closeModal(); loadMembers();
    } catch (err) { toast(err.message, true); }
  });
});

document.getElementById('membersBody').addEventListener('click', async e => {
  const editId = e.target.getAttribute('data-edit-member');
  const delId = e.target.getAttribute('data-del-member');
  if (editId) {
    const members = await api('/api/members');
    const member = members.find(m => String(m.id) === editId);
    showModal(memberForm(member));
    document.getElementById('memberForm').addEventListener('submit', async ev => {
      ev.preventDefault();
      const dobIso = ddmmyyyyToISO(document.getElementById('m_dob').value);
      if (dobIso === null) { toast('Date of Birth must be in DD-MM-YYYY format.', true); return; }
      try {
        await api(`/api/members/${editId}`, {
          method: 'PUT', body: JSON.stringify({
            name: document.getElementById('m_name').value,
            type: document.getElementById('m_type').value,
            class_dept: document.getElementById('m_class').value,
            admission_no: document.getElementById('m_admission').value,
            dob: dobIso,
            phone: document.getElementById('m_phone').value,
            parent_phone: document.getElementById('m_pphone').value,
          })
        });
        toast('Member updated.'); closeModal(); loadMembers();
      } catch (err) { toast(err.message, true); }
    });
  }
  if (delId) {
    if (!confirm('Remove this member?')) return;
    try { await api(`/api/members/${delId}`, { method: 'DELETE' }); toast('Member removed.'); loadMembers(); }
    catch (err) { toast(err.message, true); }
  }
  const resetPinId = e.target.getAttribute('data-reset-pin');
  if (resetPinId) {
    if (!confirm("Clear this student's PIN? Only do this once you've actually verified who's asking — they'll be prompted to create a new PIN the next time they log in.")) return;
    try {
      await api(`/api/members/${resetPinId}/reset-pin`, { method: 'POST' });
      toast('PIN cleared. The student will set a new one on next login.');
      loadMembers();
    } catch (err) { toast(err.message, true); }
  }
});
document.getElementById('exportMembersBtn').addEventListener('click', () => window.open('/api/export/members'));
document.getElementById('importMembersBtn').addEventListener('click', () => document.getElementById('importMembersInput').click());
document.getElementById('importMembersInput').addEventListener('change', async e => {
  const file = e.target.files[0]; if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  try {
    const res = await fetch('/api/import/members', { method: 'POST', body: fd, headers: { 'X-CSRF-Token': document.body.dataset.csrf || '' } });
    let data = null;
    try { data = await res.json(); } catch (parseErr) { /* fall through to generic message below */ }
    if (!res.ok) {
      toast((data && data.error) || `Import failed (server returned ${res.status}).`, true);
    } else {
      const bits = [`Added ${data.created} of ${data.total_rows} student(s) from "${data.sheet_used}"`];
      if (data.skipped_duplicate) bits.push(`${data.skipped_duplicate} already existed (matched by admission no.)`);
      if (data.skipped_blank) bits.push(`${data.skipped_blank} had no name`);
      if (data.skipped_error) bits.push(`${data.skipped_error} row(s) had an error`);
      toast(bits.join(' — ') + '.');
      if (data.first_error) console.warn('First import row error:', data.first_error);
      loadMembers();
    }
  } catch (err) { toast('Import failed — could not reach the server. Please try again.', true); }
  e.target.value = '';
});

/* =====================================================================
   ISSUE & RETURN
===================================================================== */
async function loadCirculation() {
  const status = document.getElementById('circFilter').value;
  const issues = await api('/api/issues?status=' + status);
  document.getElementById('circBody').innerHTML = issues.map(i => `
    <tr>
      <td>${i.accession_no}</td>
      <td>${i.member_id}</td>
      <td>${i.issue_date}</td>
      <td>${i.due_date}</td>
      <td>${statTag(i.status)}</td>
      <td>${i.fine > 0 ? '₹' + i.fine : '—'}</td>
      <td>
        ${i.status !== 'Returned' ? `<button class="brass" data-return="${i.id}">Return</button>
          <button class="secondary" data-renew="${i.id}">Renew</button>` : ''}
      </td>
    </tr>`).join('') || `<tr><td colspan="7" style="text-align:center;color:#9a9180;padding:20px;">No loans found.</td></tr>`;
}
document.getElementById('circFilter').addEventListener('change', loadCirculation);

document.getElementById('issueBtn').addEventListener('click', async () => {
  const books = (await api('/api/books')).filter(b => b.status === 'Available');
  const members = await api('/api/members');
  if (!books.length) { toast('No copies available to issue.', true); return; }
  showModal(`
    <h3>Issue a Book</h3>
    <form id="issueForm">
      <div class="field full"><label>Book *</label>
        <select id="i_book">${books.map(b => `<option value="${b.accession_no}">${escapeHtml(b.title)} — ${b.accession_no}</option>`).join('')}</select>
      </div>
      <div class="field full">
        <label>Member *</label>
        <input type="text" id="i_member_search" autocomplete="off"
               placeholder="Search by name, admission no., or class...">
        <div id="i_member_list" style="max-height:180px; overflow:auto; border:1px solid #ccc; margin-top:4px; display:none;"></div>
        <input type="hidden" id="i_member" required>
        <p class="hint" id="i_member_selected"></p>
      </div>
      <div class="modal-actions">
        <button type="button" class="ghost" onclick="closeModal()">Cancel</button>
        <button type="submit" class="brass">Issue Book</button>
      </div>
    </form>`);

  const searchEl = document.getElementById('i_member_search');
  const listEl = document.getElementById('i_member_list');
  const hiddenEl = document.getElementById('i_member');
  const selectedEl = document.getElementById('i_member_selected');

  function renderMemberMatches() {
    const q = searchEl.value.trim().toLowerCase();
    if (!q) { listEl.style.display = 'none'; listEl.innerHTML = ''; return; }
    const matches = members.filter(m =>
      (m.name || '').toLowerCase().includes(q) ||
      (m.admission_no || '').toLowerCase().includes(q) ||
      (m.class_dept || '').toLowerCase().includes(q) ||
      (m.member_id || '').toLowerCase().includes(q)
    ).slice(0, 25);
    listEl.innerHTML = matches.map(m => `
      <div class="member-hit" data-pick="${m.member_id}"
           style="padding:6px 8px; cursor:pointer; border-bottom:1px solid #eee;">
        <strong>${escapeHtml(m.name)}</strong> — ${escapeHtml(m.class_dept || '—')}
        · Adm. No: ${escapeHtml(m.admission_no || '—')} · ID: ${m.member_id}
      </div>`).join('') || `<div style="padding:6px 8px; color:#9a9180;">No matching student/staff found.</div>`;
    listEl.style.display = matches.length || q ? 'block' : 'none';
  }
  searchEl.addEventListener('input', renderMemberMatches);
  listEl.addEventListener('click', e => {
    const pick = e.target.closest('[data-pick]');
    if (!pick) return;
    const m = members.find(mm => mm.member_id === pick.getAttribute('data-pick'));
    hiddenEl.value = m.member_id;
    selectedEl.textContent = `Selected: ${m.name} (${m.class_dept || '—'} · ${m.admission_no || m.member_id})`;
    listEl.style.display = 'none';
    searchEl.value = '';
  });

  document.getElementById('issueForm').addEventListener('submit', async e => {
    e.preventDefault();
    if (!hiddenEl.value) { toast('Please search for and select a member first.', true); return; }
    try {
      await api('/api/issues', {
        method: 'POST', body: JSON.stringify({
          accession_no: document.getElementById('i_book').value,
          member_id: hiddenEl.value,
        })
      });
      toast('Book issued.'); closeModal(); loadCirculation();
    } catch (err) { toast(err.message, true); }
  });
});

document.getElementById('circBody').addEventListener('click', async e => {
  const returnId = e.target.getAttribute('data-return');
  const renewId = e.target.getAttribute('data-renew');
  if (returnId) {
    try { const r = await api(`/api/issues/${returnId}/return`, { method: 'POST' });
      toast(r.fine > 0 ? `Returned. Fine due: ₹${r.fine}` : 'Returned. No fine due.'); loadCirculation();
    } catch (err) { toast(err.message, true); }
  }
  if (renewId) {
    try { await api(`/api/issues/${renewId}/renew`, { method: 'POST', body: JSON.stringify({ extra_days: 14 }) });
      toast('Loan renewed by 14 days.'); loadCirculation();
    } catch (err) { toast(err.message, true); }
  }
});
document.getElementById('exportIssuesBtn').addEventListener('click', () => window.open('/api/export/issues'));

/* =====================================================================
   DIGITAL LIBRARY — multi-format repository (PDF/EPUB/Word/PowerPoint/
   audio/video), Smart Search (server-ranked), QR codes per item.
   Reading is open to everyone signed in; add/edit/delete are staff-only.
===================================================================== */
const FILE_TYPE_LIMITS_MB = { pdf: 60, epub: 60, doc: 60, docx: 60, ppt: 60, pptx: 60, mp4: 400, webm: 400, mp3: 150, wav: 150, m4a: 150 };
const FILE_TYPE_LABELS = { pdf: 'PDF', epub: 'EPUB', doc: 'Word', docx: 'Word', ppt: 'PowerPoint', pptx: 'PowerPoint', mp4: 'Video', webm: 'Video', mp3: 'Audio', wav: 'Audio', m4a: 'Audio' };
const FILE_TYPE_ICON = { pdf: '📄', epub: '📖', doc: '📝', docx: '📝', ppt: '📊', pptx: '📊', mp4: '🎬', webm: '🎬', mp3: '🎧', wav: '🎧', m4a: '🎧' };
let ONLINE_BOOKS_CACHE = [];

document.getElementById('addOnlineBookBtn').style.display = IS_STAFF ? '' : 'none';
// Students see their own class's books by default; this lifts that filter.
document.getElementById('onlineAllGradesWrap').style.display = IS_STUDENT ? '' : 'none';

async function loadOnlineBooks() {
  const q = document.getElementById('onlineSearch').value;
  const allGrades = IS_STUDENT && document.getElementById('onlineAllGrades').checked;
  const books = await api('/api/online-books?q=' + encodeURIComponent(q) + (allGrades ? '&all_grades=1' : ''));
  ONLINE_BOOKS_CACHE = books;

  if (IS_STUDENT) {
    try {
      const flags = await api('/api/student/my-flags');
      MY_BOOKMARKS = new Set(flags.bookmarked);
      MY_FAVORITES = new Set(flags.favorited);
    } catch (e) { /* not fatal — buttons just show as unmarked */ }
  }

  // Subject filter, built from whatever subjects actually exist.
  const catEl = document.getElementById('onlineCategoryFilter');
  const curCat = catEl.value;
  const cats = [...new Set(books.map(b => (b.subject || '').trim()).filter(Boolean))].sort();
  catEl.innerHTML = `<option value="">All Subjects</option>` +
    cats.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
  catEl.value = cats.includes(curCat) ? curCat : '';

  // Category (genre) filter, built the same way.
  const genreEl = document.getElementById('onlineGenreFilter');
  const curGenre = genreEl.value;
  const genres = [...new Set(books.map(b => (b.category || '').trim()).filter(Boolean))].sort();
  genreEl.innerHTML = `<option value="">All Categories</option>` +
    genres.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
  genreEl.value = genres.includes(curGenre) ? curGenre : '';

  renderOnlineBooks();
  renderDlStatsStrip();
  renderDlSubjectIcons();
  if (IS_STUDENT) renderSearchSuggestions(q);
}
let MY_BOOKMARKS = new Set();
let MY_FAVORITES = new Set();
let ACTIVE_MEDIA_GROUP = null; // 'audio' | 'video' | null — set by category tile clicks
const AUDIO_TYPES = ['mp3', 'wav', 'm4a'];
const VIDEO_TYPES = ['mp4', 'webm'];

const SUBJECT_ICON_MAP = [
  [/math/i, '📐'], [/phys/i, '⚛️'], [/chem/i, '🧪'], [/bio/i, '🧬'],
  [/comput|informatics|it\b/i, '💻'], [/english/i, '📗'], [/hindi/i, '🕉️'],
  [/telugu/i, '📜'], [/social|history|civics|geograph/i, '🌍'], [/econom/i, '💰'],
  [/art|craft|draw/i, '🎨'], [/music/i, '🎵'], [/sport|physical|pe\b/i, '⚽'],
  [/sanskrit/i, '📿'], [/environ/i, '🌱'], [/general knowledge|gk\b/i, '🧠'],
];
function iconForSubject(subject) {
  const match = SUBJECT_ICON_MAP.find(([re]) => re.test(subject));
  return match ? match[1] : '📘';
}

function renderDlSubjectIcons() {
  const subjects = [...new Set(ONLINE_BOOKS_CACHE.map(b => (b.subject || '').trim()).filter(Boolean))].sort();
  const section = document.getElementById('dlSubjectSection');
  if (!subjects.length) { section.style.display = 'none'; return; }
  section.style.display = '';
  document.getElementById('dlSubjectIconGrid').innerHTML = subjects.map((s, i) => `
    <div class="dl-cat-tile" data-subject-tile="${escapeHtml(s)}" style="--tile-c:${['#12294f','#a3352b','#1d7a4c','#8a5a00','#6a3fa0','#0b6e8f','#b8860b'][i % 7]};">
      <div class="dl-cat-icon">${iconForSubject(s)}</div>
      <div class="dl-cat-label">${escapeHtml(s)}</div>
    </div>`).join('');
}
document.getElementById('dlSubjectIconGrid').addEventListener('click', e => {
  const tile = e.target.closest('[data-subject-tile]');
  if (!tile) return;
  document.querySelectorAll('.dl-cat-tile').forEach(t => t.classList.remove('active-filter'));
  tile.classList.add('active-filter');
  const value = tile.getAttribute('data-subject-tile');
  ACTIVE_MEDIA_GROUP = null;
  document.getElementById('onlineCategoryFilter').value = value;
  renderOnlineBooks();
  showDlBanner(`Subject: ${value}`, () => { document.getElementById('onlineCategoryFilter').value = ''; renderOnlineBooks(); });
});

function renderDlStatsStrip() {
  const books = ONLINE_BOOKS_CACHE;
  const audioCount = books.filter(b => AUDIO_TYPES.includes(b.file_type)).length;
  const videoCount = books.filter(b => VIDEO_TYPES.includes(b.file_type)).length;
  const subjectCount = new Set(books.map(b => b.subject).filter(Boolean)).size;
  const categoryCount = new Set(books.map(b => b.category).filter(Boolean)).size;
  document.getElementById('dlStatsStrip').innerHTML = [
    { n: books.length, l: 'Total Items' },
    { n: subjectCount, l: 'Subjects' },
    { n: categoryCount, l: 'Categories' },
    { n: videoCount, l: 'Videos' },
    { n: audioCount, l: 'Audio' },
  ].map(s => `<div class="dl-stat-chip"><b>${s.n}</b>${s.l}</div>`).join('');
}

function renderOnlineBooks() {
  const cat = document.getElementById('onlineCategoryFilter').value;
  const genre = document.getElementById('onlineGenreFilter').value;
  const type = document.getElementById('onlineTypeFilter').value;
  const q = document.getElementById('onlineSearch').value.trim();
  let books = ONLINE_BOOKS_CACHE.filter(b =>
    (!cat || (b.subject || '') === cat) &&
    (!genre || (b.category || '') === genre) &&
    (!type || b.file_type === type) &&
    (!ACTIVE_MEDIA_GROUP || (ACTIVE_MEDIA_GROUP === 'audio' ? AUDIO_TYPES : VIDEO_TYPES).includes(b.file_type)));
  // Smart Search already ranks by relevance server-side when q is set;
  // only re-sort alphabetically when there's no search term.
  if (!q) books = [...books].sort((a, b) => (a.title || '').localeCompare(b.title || ''));

  document.getElementById('onlineGrid').innerHTML = books.map(b => {
    const ft = b.file_type || 'pdf'; // legacy rows may predate the file_type column — treat as PDF (read-only), never as a download
    const icon = FILE_TYPE_ICON[ft] || '📁';
    const label = FILE_TYPE_LABELS[ft] || ft.toUpperCase();
    const sizeStr = b.file_size_kb ? (b.file_size_kb > 1024 ? `${(b.file_size_kb / 1024).toFixed(1)} MB` : `${b.file_size_kb} KB`) : '';
    const officeConverted = ['doc', 'docx', 'ppt', 'pptx'].includes(ft) && b.has_preview;
    const canPreview = ['pdf', 'epub', 'mp4', 'webm', 'mp3', 'wav', 'm4a'].includes(ft) || officeConverted;
    // Word/PowerPoint files that couldn't be auto-converted (LibreOffice not
    // installed on this PC) can't be shown in-browser at all — a real
    // download is unavoidable for anyone reading them. Students only get that
    // option for those specific, unconverted formats; every previewable
    // format (including auto-converted Word/PowerPoint) opens read-only
    // in-page with no raw file download offered.
    let actionBtn;
    if (canPreview) {
      actionBtn = `<button class="brass" data-read="${b.id}" data-type="${officeConverted ? 'pdf' : ft}" data-title="${escapeHtml(b.title)}">Read Online</button>`;
    } else if (IS_STAFF) {
      actionBtn = `<button class="brass" data-read="${b.id}" data-type="${ft}" data-title="${escapeHtml(b.title)}">Download</button>`;
    } else {
      actionBtn = `<span class="hint">Ask library staff for this file (${label} isn't viewable in-browser).</span>`;
    }
    return `
    <div class="online-card">
      <h4>${icon} ${escapeHtml(b.title)}</h4>
      <p>${escapeHtml(b.author || '')}${b.author ? ' · ' : ''}${escapeHtml(b.subject || '')}
        ${b.category ? `<br><span class="tag" style="background:var(--parchment-dark); color:var(--navy);">${escapeHtml(b.category)}</span>` : ''}
        ${IS_STAFF && b.grade_target ? `<span class="tag" style="background:var(--parchment-dark); color:var(--navy);">Class ${b.grade_target}</span>` : ''}
        <br><span class="hint">${label}${sizeStr ? ' · ' + sizeStr : ''}${officeConverted ? ' · readable in-browser' : ''}${b.ocr_applied ? ' · OCR text indexed' : ''}</span></p>
      <div class="io-row">
        ${actionBtn}
        ${IS_STUDENT ? `<button class="ghost" data-toggle-bookmark="${b.id}" title="Save for later">${MY_BOOKMARKS.has(b.id) ? '🔖' : '📑'}</button>` : ''}
        ${IS_STUDENT ? `<button class="ghost" data-toggle-favorite="${b.id}" title="Favorite">${MY_FAVORITES.has(b.id) ? '⭐' : '☆'}</button>` : ''}
        ${IS_STAFF && officeConverted ? `<button class="secondary" data-download-original="${b.id}">Original ${label}</button>` : ''}
        ${IS_STAFF ? `<button class="secondary" data-edit-online="${b.id}">Edit</button>` : ''}
        ${IS_STAFF ? `<button class="ghost" data-qr-online="${b.id}">QR</button>` : ''}
        ${IS_ADMIN ? `<button class="danger" data-del-online="${b.id}">Delete</button>` : ''}
      </div>
    </div>`;
  }).join('') || `<p class="hint">No items in the Digital Library yet${q || cat || type ? ' matching your filters' : ''}.</p>`;
}

document.getElementById('onlineSearch').addEventListener('input', loadOnlineBooks);
document.getElementById('onlineAllGrades').addEventListener('change', loadOnlineBooks);
document.getElementById('onlineCategoryFilter').addEventListener('change', renderOnlineBooks);
document.getElementById('onlineGenreFilter').addEventListener('change', renderOnlineBooks);
document.getElementById('onlineTypeFilter').addEventListener('change', renderOnlineBooks);

/* ---------- NDL-style category tile browsing ---------- */
function clearDlBanner() {
  document.getElementById('dlActiveFilterBanner').style.display = 'none';
  document.querySelectorAll('.dl-cat-tile').forEach(t => t.classList.remove('active-filter'));
}
function showDlBanner(label, onClear) {
  const banner = document.getElementById('dlActiveFilterBanner');
  banner.style.display = 'flex';
  banner.innerHTML = `<span>Showing: <b>${escapeHtml(label)}</b></span><button id="dlClearFilterBtn">✕ Clear</button>`;
  document.getElementById('dlClearFilterBtn').addEventListener('click', () => {
    clearDlBanner();
    onClear();
  });
}

document.getElementById('dlCategoryGrid').addEventListener('click', async e => {
  const tile = e.target.closest('.dl-cat-tile');
  if (!tile) return;
  const action = tile.getAttribute('data-cat-action');
  document.querySelectorAll('.dl-cat-tile').forEach(t => t.classList.remove('active-filter'));
  tile.classList.add('active-filter');

  if (action === 'all') {
    ACTIVE_MEDIA_GROUP = null;
    document.getElementById('onlineGenreFilter').value = '';
    document.getElementById('onlineCategoryFilter').value = '';
    document.getElementById('onlineTypeFilter').value = '';
    document.getElementById('onlineSearch').value = '';
    clearDlBanner();
    renderOnlineBooks();
  } else if (action.startsWith('category:')) {
    const value = action.slice('category:'.length);
    ACTIVE_MEDIA_GROUP = null;
    document.getElementById('onlineGenreFilter').value = value;
    renderOnlineBooks();
    const found = ONLINE_BOOKS_CACHE.some(b => (b.category || '') === value);
    showDlBanner(
      found ? tile.querySelector('.dl-cat-label').textContent : `${tile.querySelector('.dl-cat-label').textContent} (nothing added under "${value}" yet — ask a librarian to tag some items)`,
      () => { document.getElementById('onlineGenreFilter').value = ''; renderOnlineBooks(); }
    );
  } else if (action === 'type:audio' || action === 'type:video') {
    ACTIVE_MEDIA_GROUP = action.split(':')[1];
    renderOnlineBooks();
    showDlBanner(tile.querySelector('.dl-cat-label').textContent, () => { ACTIVE_MEDIA_GROUP = null; renderOnlineBooks(); });
  } else if (action === 'subject-wise') {
    ACTIVE_MEDIA_GROUP = null;
    renderOnlineBooks();
    clearDlBanner();
    const sel = document.getElementById('onlineCategoryFilter');
    sel.scrollIntoView({ behavior: 'smooth', block: 'center' });
    sel.focus();
    sel.style.outline = '3px solid var(--brass)';
    setTimeout(() => { sel.style.outline = ''; }, 2000);
  } else if (action === 'focus-search') {
    ACTIVE_MEDIA_GROUP = null;
    renderOnlineBooks();
    clearDlBanner();
    const box = document.getElementById('onlineSearch');
    box.scrollIntoView({ behavior: 'smooth', block: 'center' });
    box.focus();
  } else if (action === 'recommended') {
    ACTIVE_MEDIA_GROUP = null;
    try {
      let recs = [];
      let label = 'Recommended For You';
      if (IS_STUDENT) {
        recs = (await api('/api/student/my-library')).recommendations;
      } else {
        recs = [...ONLINE_BOOKS_CACHE].slice(-8).reverse();
        label = 'Recently Added (sign in as a student to see personal recommendations)';
      }
      document.getElementById('onlineGrid').innerHTML = recs.length
        ? recs.map(b => `<div class="online-card"><h4>${FILE_TYPE_ICON[b.file_type] || '📁'} ${escapeHtml(b.title)}</h4>
            <p class="hint">${escapeHtml(b.author || '')}${b.author ? ' · ' : ''}${escapeHtml(b.subject || '')}</p>
            <div class="io-row"><button class="brass" data-mini-read="${b.id}" data-type="${b.file_type}" data-title="${escapeHtml(b.title)}">Read Online</button></div></div>`).join('')
        : `<p class="hint">Nothing to recommend yet — read and favorite a few items first.</p>`;
      showDlBanner(label, () => renderOnlineBooks());
    } catch (err) { toast(err.message, true); }
  }
});
document.getElementById('onlineGrid').addEventListener('click', e => {
  const btn = e.target.closest('[data-mini-read]');
  if (!btn) return;
  openMediaViewerAndTrackPoints(btn.getAttribute('data-mini-read'), btn.getAttribute('data-type'), btn.getAttribute('data-title'));
});

async function openMediaViewerAndTrackPoints(id, type, title) {
  if (!IS_STUDENT) { await openMediaViewer(id, type, title); return; }
  let before = 0;
  try { before = (await api('/api/student/points')).points; } catch (e) { /* non-fatal */ }
  await openMediaViewer(id, type, title);
  // The read gets logged server-side as a side effect of the /link request
  // the viewer just made; give it a moment to land, then check whether
  // points actually went up (first read of this item, not a re-read).
  setTimeout(async () => {
    try {
      const after = await api('/api/student/points');
      if (after.points > before) toast(`📖 +${after.points - before} points for reading something new! Total: ${after.points}`);
    } catch (e) { /* non-fatal */ }
  }, 1200);
}

/* ---------- media viewer ----------
   The viewer asks /link for the URL to show rather than building a /view URL
   itself. On the cloud deployment that URL is a short-lived presigned link;
   for video/audio the viewer warns shortly before it stops working and lets
   the student renew it ("Need more time") without losing their place. With
   local storage the link never expires and none of the timer code runs.
   All viewer state lives at module level so closeMediaViewer() can reliably
   cancel anything still pending. */
const TIMED_MEDIA_TYPES = ['mp4', 'webm', 'mp3', 'wav', 'm4a'];
const PROGRESS_INTERVAL_MS = 15000;
let viewerSession = 0;        // bumped on every open/close; stale async work checks it
let viewerWarnTimer = null;   // fires when the "Need more time" bar is due
let viewerBookId = null;
let viewerMedia = null;       // the <video>/<audio> element, if any
let viewerLastProgressSent = 0;

// fetch() directly (not api()) because the caller needs the status code:
// a 401 means "signed out", which is a prompt rather than an error toast.
async function fetchViewerLink(id, renew) {
  const res = await fetch(`/api/online-books/${id}/link${renew ? '?renew=1' : ''}`, { cache: 'no-store' });
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-json */ }
  return { ok: res.ok, status: res.status, data };
}

function sendViewerProgress(final) {
  // Staff have no reading history, and 0 s means nothing was watched yet.
  if (!IS_STUDENT || !viewerMedia || viewerBookId == null) return;
  const seconds = Math.floor(viewerMedia.currentTime || 0);
  if (seconds <= 0) return;
  viewerLastProgressSent = Date.now();
  // Fire and forget: losing one progress update must never disturb playback.
  // keepalive lets the request finish even as the page tears the viewer down.
  api(`/api/online-books/${viewerBookId}/progress`, {
    method: 'POST', body: JSON.stringify({ seconds }), keepalive: !!final
  }).catch(() => {});
}

function renewBarMessage(link) {
  const left = link.expires_in - link.warn_at;
  if (left >= 300) return 'Your access ends in 5 minutes.';
  const mins = Math.max(1, Math.ceil(left / 60));
  return `Your access ends in ${mins} ${mins === 1 ? 'minute' : 'minutes'}.`;
}

function scheduleRenewWarning(link, session) {
  clearTimeout(viewerWarnTimer);
  if (link.expires_in == null || link.warn_at == null) return;
  viewerWarnTimer = setTimeout(() => {
    if (session !== viewerSession) return;
    showRenewBar(link, session);
  }, link.warn_at * 1000);
}

function hideRenewBar() {
  const bar = document.getElementById('renewBar');
  if (bar) bar.remove();
}

function showRenewBar(link, session) {
  hideRenewBar();
  const wrap = document.querySelector('#pdfViewerOverlay .pdf-frame-wrap');
  const header = wrap.querySelector('.pdf-bar');
  const bar = document.createElement('div');
  bar.className = 'renew-bar';
  bar.id = 'renewBar';
  bar.setAttribute('role', 'status');
  bar.innerHTML = `<span id="renewMsg">${escapeHtml(renewBarMessage(link))}</span>
    <button type="button" id="renewLinkBtn">Need more time</button>`;
  header.insertAdjacentElement('afterend', bar);
  document.getElementById('renewLinkBtn').addEventListener('click', () => renewViewerLink(session));
}

async function renewViewerLink(session) {
  const btn = document.getElementById('renewLinkBtn');
  const msg = document.getElementById('renewMsg');
  if (!btn || !msg) return;
  btn.disabled = true;
  let result;
  try {
    result = await fetchViewerLink(viewerBookId, true);
  } catch (e) {
    result = { ok: false, status: 0, data: null };
  }
  if (session !== viewerSession) return;   // viewer was closed meanwhile
  if (result.ok) {
    const media = viewerMedia;
    // Swapping src restarts the element, so remember the place and restore it.
    const resumeAt = media.currentTime;
    const wasPaused = media.paused;
    media.addEventListener('loadedmetadata', () => {
      media.currentTime = resumeAt;
      if (!wasPaused) media.play().catch(() => {});
    }, { once: true });
    media.src = result.data.url;
    if (wasPaused) media.pause();
    hideRenewBar();
    scheduleRenewWarning(result.data, session);
  } else if (result.status === 401) {
    msg.innerHTML = 'You\'ve been signed out. <a href="/login" target="_blank" rel="noopener">Sign in again</a> to continue.';
    btn.remove();
  } else {
    msg.textContent = 'Could not renew right now. Please try again.';
    btn.disabled = false;
  }
}

async function openMediaViewer(id, type, title) {
  const wrap = document.querySelector('#pdfViewerOverlay .pdf-frame-wrap');
  // A previous viewer may still be open (or its timers pending).
  closeMediaViewer();
  const session = viewerSession;

  let result;
  try {
    result = await fetchViewerLink(id, false);
  } catch (e) {
    toast('Could not open this item. Check your connection and try again.', true);
    return;
  }
  if (session !== viewerSession) return;
  if (!result.ok) {
    toast((result.data && result.data.error) || 'Could not open this item.', true);
    return;
  }
  const link = result.data;
  const url = link.url;
  // The link reports what is actually served (a Word/PowerPoint file with a
  // generated PDF preview is served as pdf); fall back to the caller's type.
  type = link.file_type || type;

  let media = null;
  if (type === 'pdf') {
    wrap.innerHTML = `<div class="pdf-bar"><span id="pdfTitle">${escapeHtml(title)}</span><button class="ghost" id="closePdfBtn">✕ Close</button></div>
      <iframe id="pdfFrame" src="${url}#toolbar=0&navpanes=0"></iframe>`;
  } else if (['mp4', 'webm'].includes(type)) {
    wrap.innerHTML = `<div class="pdf-bar"><span>${escapeHtml(title)}</span><button class="ghost" id="closePdfBtn">✕ Close</button></div>
      <video src="${url}" controls autoplay controlsList="nodownload noremoteplayback" disablePictureInPicture oncontextmenu="return false;" style="width:100%; max-height:80vh; background:#000; border-radius:6px;"></video>`;
    media = wrap.querySelector('video');
  } else if (['mp3', 'wav', 'm4a'].includes(type)) {
    wrap.innerHTML = `<div class="pdf-bar"><span>${escapeHtml(title)}</span><button class="ghost" id="closePdfBtn">✕ Close</button></div>
      <div style="background:var(--parchment); border-radius:8px; padding:40px 20px; text-align:center;">
        <div style="font-size:48px; margin-bottom:12px;">🎧</div>
        <audio src="${url}" controls autoplay controlsList="nodownload" oncontextmenu="return false;" style="width:100%;"></audio>
      </div>`;
    media = wrap.querySelector('audio');
  } else if (type === 'epub') {
    wrap.innerHTML = `<div class="pdf-bar"><span>${escapeHtml(title)}</span><button class="ghost" id="closePdfBtn">✕ Close</button></div>
      <div id="epubArea" style="flex:1; background:#fff; border-radius:6px;"></div>`;
    loadEpubReader(url);
  } else {
    // Office/legacy formats: browsers can't render these — just download them.
    window.location.href = url;
    return;
  }

  if (media) {
    viewerBookId = id;
    viewerMedia = media;
    viewerLastProgressSent = Date.now();
    // Resume where the student left off, unless they were basically done
    // or had only just started.
    media.addEventListener('loadedmetadata', () => {
      const p = link.progress_seconds || 0;
      if (p > 5 && (!isFinite(media.duration) || p < media.duration - 5)) media.currentTime = p;
    }, { once: true });
    media.addEventListener('timeupdate', () => {
      if (Date.now() - viewerLastProgressSent >= PROGRESS_INTERVAL_MS) sendViewerProgress(false);
    });
    if (TIMED_MEDIA_TYPES.includes(type)) scheduleRenewWarning(link, session);
  }
  document.getElementById('closePdfBtn').addEventListener('click', closeMediaViewer);
  document.getElementById('pdfViewerOverlay').classList.add('show');
}

function closeMediaViewer() {
  sendViewerProgress(true);
  viewerSession++;
  clearTimeout(viewerWarnTimer);
  viewerWarnTimer = null;
  viewerMedia = null;
  viewerBookId = null;
  document.getElementById('pdfViewerOverlay').classList.remove('show');
  document.querySelector('#pdfViewerOverlay .pdf-frame-wrap').innerHTML = '';
}

let EPUBJS_LOADED = false;
function loadEpubReader(url) {
  function render() {
    const book = ePub(url);
    const rendition = book.renderTo("epubArea", { width: "100%", height: "100%" });
    rendition.display();
  }
  if (EPUBJS_LOADED || window.ePub) { render(); return; }
  const s1 = document.createElement('script');
  s1.src = '/static/vendor/jszip.min.js';
  s1.onload = () => {
    const s2 = document.createElement('script');
    s2.src = '/static/vendor/epub.min.js';
    s2.onload = () => { EPUBJS_LOADED = true; render(); };
    s2.onerror = () => { document.getElementById('epubArea').innerHTML = '<p class="hint" style="padding:20px;">Could not load the EPUB reader. <a href="' + url + '">Download the file instead</a>.</p>'; };
    document.head.appendChild(s2);
  };
  document.head.appendChild(s1);
}

document.getElementById('onlineGrid').addEventListener('click', async e => {
  const readBtn = e.target.closest('[data-read]');
  const editBtn = e.target.closest('[data-edit-online]');
  const delBtn = e.target.closest('[data-del-online]');
  const qrBtn = e.target.closest('[data-qr-online]');
  const origBtn = e.target.closest('[data-download-original]');
  const bookmarkBtn = e.target.closest('[data-toggle-bookmark]');
  const favoriteBtn = e.target.closest('[data-toggle-favorite]');

  if (readBtn) {
    await openMediaViewerAndTrackPoints(readBtn.getAttribute('data-read'), readBtn.getAttribute('data-type'), readBtn.getAttribute('data-title'));
  }
  if (bookmarkBtn) {
    const id = bookmarkBtn.getAttribute('data-toggle-bookmark');
    const isSet = MY_BOOKMARKS.has(Number(id));
    try {
      await api(`/api/online-books/${id}/bookmark`, { method: isSet ? 'DELETE' : 'POST' });
      if (isSet) MY_BOOKMARKS.delete(Number(id)); else MY_BOOKMARKS.add(Number(id));
      bookmarkBtn.textContent = isSet ? '📑' : '🔖';
      toast(isSet ? 'Removed from Bookmarks.' : 'Saved to Bookmarks.');
    } catch (err) { toast(err.message, true); }
  }
  if (favoriteBtn) {
    const id = favoriteBtn.getAttribute('data-toggle-favorite');
    const isSet = MY_FAVORITES.has(Number(id));
    try {
      await api(`/api/online-books/${id}/favorite`, { method: isSet ? 'DELETE' : 'POST' });
      if (isSet) MY_FAVORITES.delete(Number(id)); else MY_FAVORITES.add(Number(id));
      favoriteBtn.textContent = isSet ? '☆' : '⭐';
      toast(isSet ? 'Removed from Favorites.' : 'Added to Favorites.');
    } catch (err) { toast(err.message, true); }
  }
  if (origBtn) {
    window.location.href = `/api/online-books/${origBtn.getAttribute('data-download-original')}/download-original`;
  }
  if (editBtn) {
    const book = ONLINE_BOOKS_CACHE.find(b => String(b.id) === editBtn.getAttribute('data-edit-online'));
    openOnlineBookForm(book);
  }
  if (qrBtn) {
    const book = ONLINE_BOOKS_CACHE.find(b => String(b.id) === qrBtn.getAttribute('data-qr-online'));
    showModal(`<h3>QR Code</h3><p class="hint">${escapeHtml(book.title)}</p>
      <div style="text-align:center; padding:10px 0;">
        ${book.qr_path ? `<img src="/${book.qr_path}" style="width:220px; height:220px;">` : '<p class="hint">No QR code yet — try Edit &amp; Save to regenerate it.</p>'}
      </div>
      <div class="modal-actions"><button class="ghost" onclick="closeModal()">Close</button></div>`);
  }
  if (delBtn) {
    if (!confirm('Delete this item from the Digital Library? The file will be removed too.')) return;
    try {
      await api(`/api/online-books/${delBtn.getAttribute('data-del-online')}`, { method: 'DELETE' });
      toast('Deleted from the Digital Library.');
      loadOnlineBooks();
    } catch (err) { toast(err.message, true); }
  }
});

// "All classes" plus Class 1-12; the value is what the server's grade_target expects.
function gradeSelect(id, selected) {
  return `<select id="${id}"><option value="">All classes</option>` +
    Array.from({ length: 12 }, (_, i) => i + 1)
      .map(g => `<option value="${g}"${g === selected ? ' selected' : ''}>Class ${g}</option>`).join('') +
    `</select>`;
}

function onlineBookForm(existing) {
  return `
    <h3>${existing ? 'Edit Item' : 'Add to Digital Library'}</h3>
    <form id="onlineBookForm">
      <label>Title *<input type="text" id="ob_title" value="${existing ? escapeHtml(existing.title) : ''}" required></label>
      <label>Author<input type="text" id="ob_author" value="${existing ? escapeHtml(existing.author || '') : ''}"></label>
      <label>Category <span class="hint">(genre — e.g. Fiction, Comic, Biography)</span>
        <input type="text" id="ob_category" list="ob_category_list" value="${existing ? escapeHtml(existing.category || '') : ''}">
      </label>
      <datalist id="ob_category_list">
        ${['Fiction', 'Non-Fiction', 'Comic', 'Biography', 'Reference', 'Poetry', 'Drama', 'Textbook', 'Encyclopedia',
           ...new Set(ONLINE_BOOKS_CACHE.map(b => b.category).filter(Boolean))]
          .filter((v, i, arr) => arr.indexOf(v) === i)
          .map(s => `<option value="${escapeHtml(s)}">`).join('')}
      </datalist>
      <label>Subject <span class="hint">(academic — e.g. Maths, Physics, Chemistry)</span>
        <input type="text" id="ob_subject" list="ob_subject_list" value="${existing ? escapeHtml(existing.subject || '') : ''}">
      </label>
      <datalist id="ob_subject_list">
        ${['Mathematics', 'Physics', 'Chemistry', 'Biology', 'Computer Science', 'English', 'Social Studies', 'Hindi', 'Telugu',
           ...new Set(ONLINE_BOOKS_CACHE.map(b => b.subject).filter(Boolean))]
          .filter((v, i, arr) => arr.indexOf(v) === i)
          .map(s => `<option value="${escapeHtml(s)}">`).join('')}
      </datalist>
      <label>Class <span class="hint">(students see books for their own class by default)</span>
        ${gradeSelect('ob_grade', existing ? existing.grade_target : null)}
      </label>
      <label>${existing ? 'Replace File (optional)' : 'File *'}
        <input type="file" id="ob_file" accept=".pdf,.epub,.doc,.docx,.ppt,.pptx,.mp4,.webm,.mp3,.wav,.m4a" ${existing ? '' : 'required'}>
      </label>
      <p class="hint">PDF, EPUB, Word, PowerPoint (up to 60 MB) · Video (up to 400 MB) · Audio (up to 150 MB).</p>
      <div id="ob_progress_wrap" style="display:none; margin-top:8px;">
        <div style="background:var(--parchment-dark); border-radius:999px; height:10px; overflow:hidden;">
          <div id="ob_progress_bar" style="background:var(--brass); height:100%; width:0%; transition:width .2s;"></div>
        </div>
        <p class="hint" id="ob_progress_text" style="margin-top:4px;">Uploading... 0%</p>
      </div>
      <div class="modal-actions">
        <button type="button" class="ghost" onclick="closeModal()">Cancel</button>
        <button type="submit" class="brass" id="ob_submit_btn">${existing ? 'Save Changes' : 'Add to Library'}</button>
      </div>
    </form>`;
}

function openOnlineBookForm(existing) {
  showModal(onlineBookForm(existing));
  document.getElementById('onlineBookForm').addEventListener('submit', async e => {
    e.preventDefault();
    const fileInput = document.getElementById('ob_file');
    const file = fileInput.files[0];
    if (file) {
      const ext = file.name.split('.').pop().toLowerCase();
      const limitMb = FILE_TYPE_LIMITS_MB[ext];
      if (!limitMb) { toast(`Unsupported file type: .${ext}`, true); return; }
      if (file.size > limitMb * 1024 * 1024) { toast(`File is too large — the limit for this type is ${limitMb} MB.`, true); return; }
    }
    const fd = new FormData();
    fd.append('title', document.getElementById('ob_title').value);
    fd.append('author', document.getElementById('ob_author').value);
    fd.append('subject', document.getElementById('ob_subject').value);
    fd.append('category', document.getElementById('ob_category').value);
    fd.append('grade_target', document.getElementById('ob_grade').value);
    if (file) fd.append('file', file);

    const progressWrap = document.getElementById('ob_progress_wrap');
    const progressBar = document.getElementById('ob_progress_bar');
    const progressText = document.getElementById('ob_progress_text');
    const submitBtn = document.getElementById('ob_submit_btn');

    if (file && file.size > 5 * 1024 * 1024) {  // only show a progress bar for anything worth showing one for
      progressWrap.style.display = '';
      submitBtn.disabled = true;
      submitBtn.textContent = 'Uploading...';
    }

    try {
      const url = existing ? `/api/online-books/${existing.id}` : '/api/online-books';
      const { status, ok, data } = await xhrUpload(url, existing ? 'PUT' : 'POST', fd, pct => {
        progressBar.style.width = pct + '%';
        progressText.textContent = pct < 100 ? `Uploading... ${pct}%` : 'Finishing up (saving, generating QR code)...';
      });
      if (!ok) throw new Error((data && data.error) || `Request failed (server returned ${status}).`);
      toast(existing ? 'Saved.' : 'Added to the Digital Library.');
      closeModal();
      loadOnlineBooks();
    } catch (err) {
      toast(err.message, true);
      submitBtn.disabled = false;
      submitBtn.textContent = existing ? 'Save Changes' : 'Add to Library';
      progressWrap.style.display = 'none';
    }
  });
}

document.getElementById('addOnlineBookBtn').addEventListener('click', () => openOnlineBookForm(null));

document.getElementById('bulkImportOnlineBtn').style.display = IS_STAFF ? '' : 'none';
document.getElementById('bulkImportOnlineBtn').addEventListener('click', () =>
  document.getElementById('bulkImportOnlineInput').click());
document.getElementById('bulkImportOnlineInput').addEventListener('change', async e => {
  const files = Array.from(e.target.files || []);
  if (!files.length) return;
  const category = prompt(
    `Importing ${files.length} file(s). Titles come from filenames — rename afterwards with Edit if needed.\n\n` +
    `Optional: one Category (genre, e.g. Fiction, Comic) to apply to all of them, or leave blank.`, '') || '';
  const subject = prompt(
    `Optional: one Subject (academic, e.g. Maths, Physics) to apply to all of them, or leave blank.`, '') || '';
  const grade = prompt(
    `Optional: one Class (1-12) to aim all of them at, or leave blank for all classes.`, '') || '';
  const fd = new FormData();
  files.forEach(f => fd.append('files', f));
  if (grade.trim()) fd.append('grade_target', grade.trim());
  if (subject.trim()) fd.append('subject', subject.trim());
  if (category.trim()) fd.append('category', category.trim());

  const totalMb = (files.reduce((sum, f) => sum + f.size, 0) / (1024 * 1024)).toFixed(1);
  showModal(`
    <h3>Importing ${files.length} file(s)...</h3>
    <p class="hint">Total size: ${totalMb} MB. Large batches over Wi-Fi can take several minutes — leave this open.</p>
    <div style="background:var(--parchment-dark); border-radius:999px; height:10px; overflow:hidden; margin-top:10px;">
      <div id="bulk_progress_bar" style="background:var(--brass); height:100%; width:0%; transition:width .2s;"></div>
    </div>
    <p class="hint" id="bulk_progress_text" style="margin-top:4px;">Uploading... 0%</p>
  `);

  try {
    const { status, ok, data } = await xhrUpload('/api/online-books/bulk-import', 'POST', fd, pct => {
      const bar = document.getElementById('bulk_progress_bar');
      const text = document.getElementById('bulk_progress_text');
      if (bar) bar.style.width = pct + '%';
      if (text) text.textContent = pct < 100 ? `Uploading... ${pct}%` : 'Finishing up (saving each file, generating QR codes)...';
    });
    if (!ok) throw new Error((data && data.error) || `Bulk import failed (server returned ${status}).`);
    let msg = `Added ${data.created} of ${data.total} file(s).`;
    if (data.skipped && data.skipped.length) msg += ` Skipped: ${data.skipped.join(', ')}`;
    closeModal();
    toast(msg, data.skipped && data.skipped.length > 0 && data.created === 0);
    loadOnlineBooks();
  } catch (err) {
    closeModal();
    toast(err.message, true);
  }
  e.target.value = '';
});

/* =====================================================================
   QR CODE LIBRARY — printable sheet of every digital item's QR code
===================================================================== */
async function loadQrLibrary() {
  if (!ONLINE_BOOKS_CACHE.length) ONLINE_BOOKS_CACHE = await api('/api/online-books');
  renderQrLibrary();
}
function renderQrLibrary() {
  const q = document.getElementById('qrSearch').value.trim().toLowerCase();
  const items = ONLINE_BOOKS_CACHE.filter(b => !q || (b.title || '').toLowerCase().includes(q));
  document.getElementById('qrGrid').innerHTML = items.map(b => `
    <div class="online-card" style="text-align:center;">
      ${b.qr_path ? `<img src="/${b.qr_path}" style="width:140px; height:140px;">` : '<p class="hint">No QR yet</p>'}
      <h4 style="margin-top:8px;">${escapeHtml(b.title)}</h4>
      <p class="hint">${escapeHtml(b.subject || '')}</p>
    </div>`).join('') || `<p class="hint">No items yet — add something to the Digital Library first.</p>`;
}
document.getElementById('qrSearch').addEventListener('input', renderQrLibrary);
document.getElementById('printQrBtn').addEventListener('click', () => window.print());

document.getElementById('regenQrBtn').style.display = IS_ADMIN ? '' : 'none';
document.getElementById('regenQrBtn').addEventListener('click', async () => {
  if (!confirm('Rebuild every QR code in the Digital Library using this server\'s current network address? This fixes codes that only opened on the server PC itself and not on phones.')) return;
  const btn = document.getElementById('regenQrBtn');
  btn.disabled = true; btn.textContent = 'Rebuilding...';
  try {
    const res = await api('/api/online-books/regenerate-qr-codes', { method: 'POST' });
    toast(`Rebuilt ${res.updated} QR code(s).`);
    loadQrLibrary();
  } catch (err) { toast(err.message, true); }
  btn.disabled = false; btn.textContent = '↻ Fix QR Codes Not Opening on Phones';
});


/* =====================================================================
   REGISTERS (lost / stock / periodicals / visitors / donations)
===================================================================== */
const REGISTER_CONFIG = {
  lost: { endpoint: '/api/lost-books', headers: ['Accession No', 'Member', 'Lost Date', 'Cost', 'Remarks'],
    fields: [['accession_no', 'Accession No', 'text'], ['member_id', 'Member ID', 'text'], ['cost', 'Cost (₹)', 'number'], ['remarks', 'Remarks', 'text']],
    row: r => [r.accession_no, r.member_id || '—', r.lost_date, r.cost, r.remarks || '—'] },
  stock: { endpoint: '/api/stock-check', headers: ['Accession No', 'Date', 'Status', 'Remarks'],
    fields: [['accession_no', 'Accession No', 'text'], ['found_status', 'Status (Present/Missing/Damaged)', 'text'], ['remarks', 'Remarks', 'text']],
    row: r => [r.accession_no, r.check_date, r.found_status, r.remarks || '—'] },
  periodicals: { endpoint: '/api/periodicals', headers: ['Name', 'Type', 'Date', 'Remarks'],
    fields: [['name', 'Name', 'text'], ['type', 'Type (Newspaper/Magazine)', 'text'], ['remarks', 'Remarks', 'text']],
    row: r => [r.name, r.type, r.entry_date, r.remarks || '—'] },
  visitors: { endpoint: '/api/visitors', headers: ['Name', 'Purpose', 'Time'],
    fields: [['name', 'Name', 'text'], ['purpose', 'Purpose', 'text']],
    row: r => [r.name, r.purpose || '—', r.visit_time] },
  donations: { endpoint: '/api/donations', headers: ['Donor', 'Book Title', 'Qty', 'Date'],
    fields: [['donor_name', 'Donor Name', 'text'], ['book_title', 'Book Title', 'text'], ['quantity', 'Quantity', 'number']],
    row: r => [r.donor_name, r.book_title, r.quantity, r.donation_date] },
};

async function loadRegister() {
  const key = document.getElementById('registerPicker').value;
  const cfg = REGISTER_CONFIG[key];
  document.getElementById('registerHead').innerHTML = '<tr>' + cfg.headers.map(h => `<th>${h}</th>`).join('') + '</tr>';
  const rows = await api(cfg.endpoint);
  document.getElementById('registerBody').innerHTML = rows.map(r =>
    '<tr>' + cfg.row(r).map(c => `<td>${escapeHtml(c)}</td>`).join('') + '</tr>').join('') ||
    `<tr><td colspan="${cfg.headers.length}" style="text-align:center;color:#9a9180;padding:20px;">No entries yet.</td></tr>`;
}
document.getElementById('registerPicker').addEventListener('change', loadRegister);
document.getElementById('addRegisterBtn').addEventListener('click', () => {
  const key = document.getElementById('registerPicker').value;
  const cfg = REGISTER_CONFIG[key];
  showModal(`
    <h3>Add Entry</h3>
    <form id="registerForm">
      ${cfg.fields.map(([id, label, type]) => `<div class="field full"><label>${label}</label><input id="rf_${id}" type="${type}"></div>`).join('')}
      <div class="modal-actions">
        <button type="button" class="ghost" onclick="closeModal()">Cancel</button>
        <button type="submit" class="brass">Add</button>
      </div>
    </form>`);
  document.getElementById('registerForm').addEventListener('submit', async e => {
    e.preventDefault();
    const payload = {};
    cfg.fields.forEach(([id]) => payload[id] = document.getElementById(`rf_${id}`).value);
    try { await api(cfg.endpoint, { method: 'POST', body: JSON.stringify(payload) }); toast('Entry added.'); closeModal(); loadRegister(); }
    catch (err) { toast(err.message, true); }
  });
});

/* =====================================================================
   MANAGE USERS
===================================================================== */
let USERS_CACHE = [];
async function loadUsers() {
  const users = await api('/api/users');
  USERS_CACHE = users;
  document.getElementById('usersBody').innerHTML = users.map(u => `
    <tr><td>${escapeHtml(u.username)}</td><td>${u.role}</td><td>${escapeHtml(u.name)}</td><td>${escapeHtml(u.phone || '—')}</td>
      <td><button class="secondary" data-edit-user="${u.id}">Edit</button>
        <button class="danger" data-del-user="${u.id}">Remove</button></td></tr>`).join('');
}

function userForm(existing) {
  return `
    <h3>${existing ? 'Edit Account' : 'Add Account'}</h3>
    <form id="userForm">
      <div class="field full"><label>Display Name *</label><input id="u_name" value="${existing ? escapeHtml(existing.name) : ''}" required></div>
      <div class="field-grid">
        <div class="field"><label>Username ${existing ? '' : '*'}</label>
          <input id="u_username" value="${existing ? escapeHtml(existing.username) : ''}" ${existing ? 'disabled' : 'required'}>
          ${existing ? '<span class="hint">Username can\'t be changed here.</span>' : ''}
        </div>
        <div class="field"><label>Mobile Number</label><input id="u_phone" value="${existing ? escapeHtml(existing.phone || '') : ''}" placeholder="e.g. 9876543210"></div>
      </div>
      <div class="field full"><label>${existing ? 'New Password' : 'Password *'} <span class="hint">${existing ? '(leave blank to keep the current password)' : ''}</span></label>
        <input id="u_password" type="password" ${existing ? '' : 'required'}>
      </div>
      <div class="field full"><label>Role *</label>
        <select id="u_role">
          <option value="librarian" ${existing && existing.role === 'librarian' ? 'selected' : ''}>Librarian</option>
          <option value="admin" ${existing && existing.role === 'admin' ? 'selected' : ''}>Admin</option>
          <option value="viewer" ${existing && existing.role === 'viewer' ? 'selected' : ''}>Viewer</option>
        </select>
      </div>
      <div class="modal-actions">
        <button type="button" class="ghost" onclick="closeModal()">Cancel</button>
        <button type="submit" class="brass">${existing ? 'Save Changes' : 'Create'}</button>
      </div>
    </form>`;
}

function openUserForm(existing) {
  showModal(userForm(existing));
  document.getElementById('userForm').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      if (existing) {
        const payload = {
          name: document.getElementById('u_name').value,
          phone: document.getElementById('u_phone').value,
          role: document.getElementById('u_role').value,
        };
        const pw = document.getElementById('u_password').value;
        if (pw) payload.password = pw;
        await api(`/api/users/${existing.id}`, { method: 'PUT', body: JSON.stringify(payload) });
        toast('Account updated.');
      } else {
        await api('/api/users', { method: 'POST', body: JSON.stringify({
          name: document.getElementById('u_name').value,
          username: document.getElementById('u_username').value,
          phone: document.getElementById('u_phone').value,
          password: document.getElementById('u_password').value,
          role: document.getElementById('u_role').value,
        })});
        toast('Account created.');
      }
      closeModal(); loadUsers();
    } catch (err) { toast(err.message, true); }
  });
}

document.getElementById('addUserBtn').addEventListener('click', () => openUserForm(null));
document.getElementById('usersBody').addEventListener('click', async e => {
  const editId = e.target.getAttribute('data-edit-user');
  const delId = e.target.getAttribute('data-del-user');
  if (editId) {
    const user = USERS_CACHE.find(u => String(u.id) === editId);
    openUserForm(user);
  }
  if (delId) {
    if (!confirm('Remove this account?')) return;
    try { await api(`/api/users/${delId}`, { method: 'DELETE' }); toast('Account removed.'); loadUsers(); }
    catch (err) { toast(err.message, true); }
  }
});

document.getElementById('brandingForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const msgEl = document.getElementById('brandingMsg');
  msgEl.textContent = '';
  const name = document.getElementById('brandSchoolName').value.trim();
  if (!name) { msgEl.textContent = 'Institution name is required.'; msgEl.style.color = 'var(--danger)'; return; }

  const logoFile = document.getElementById('brandLogo').files[0];
  if (logoFile && logoFile.size > 5 * 1024 * 1024) {
    msgEl.textContent = 'Logo file is too large — please use one under 5 MB.';
    msgEl.style.color = 'var(--danger)';
    return;
  }

  const formData = new FormData();
  formData.append('school_name', name);
  formData.append('school_address', document.getElementById('brandSchoolAddress').value.trim());
  if (logoFile) formData.append('school_logo', logoFile);

  try {
    const res = await fetch('/api/branding', { method: 'POST', body: formData, headers: { 'X-CSRF-Token': document.body.dataset.csrf || '' } });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not save branding.');
    msgEl.style.color = 'var(--brass)';
    msgEl.textContent = data.warning || 'Saved. Reload the page to see the changes everywhere.';
    toast('Branding updated.');
  } catch (err) {
    msgEl.style.color = 'var(--danger)';
    msgEl.textContent = err.message;
  }
});

document.getElementById('backupBtn').addEventListener('click', async () => {
  try { const r = await api('/api/backup', { method: 'POST' }); toast(`Backup saved: ${r.file}`); }
  catch (err) { toast(err.message, true); }
});
document.getElementById('listBackupsBtn').addEventListener('click', async () => {
  const files = await api('/api/backup/list');
  document.getElementById('backupList').innerHTML = files.length
    ? '<ul>' + files.map(f => `<li><a href="/api/backup/download/${f}">${f}</a></li>`).join('') + '</ul>'
    : '<p class="hint">No backups yet.</p>';
});

/* =====================================================================
   SEARCH SUGGESTIONS — related books/videos/snippets for a search term.
   Local keyword/subject matching across Catalog + Digital Library, not
   a cloud AI model — works fully offline, matching how Smart Search works.
===================================================================== */
let SUGGEST_DEBOUNCE = null;
function renderSearchSuggestions(q) {
  clearTimeout(SUGGEST_DEBOUNCE);
  const box = document.getElementById('searchSuggestions');
  if (!q || q.trim().length < 3) { box.innerHTML = ''; return; }
  SUGGEST_DEBOUNCE = setTimeout(async () => {
    try {
      const data = await api('/api/student/search-suggestions?q=' + encodeURIComponent(q));
      const hasAny = data.books.length || data.digital.length || data.snippets.length;
      if (!hasAny) { box.innerHTML = ''; return; }
      box.innerHTML = `
        <div class="online-card" style="background:var(--parchment); margin-bottom:16px;">
          <h4>✨ Related to "${escapeHtml(q)}"</h4>
          ${data.snippets.length ? `<p class="hint" style="margin-bottom:8px;">Found inside these documents:</p>
            ${data.snippets.map(s => `<p class="hint" style="margin:2px 0;">📄 <b>${escapeHtml(s.title)}</b> — "${escapeHtml(s.snippet)}"</p>`).join('')}` : ''}
          ${data.books.length ? `<p class="hint" style="margin-top:8px;">📚 Physical books: ${data.books.map(b => escapeHtml(b.title)).join(', ')}</p>` : ''}
          ${data.digital.length ? `<p class="hint">🖥️ Digital items: ${data.digital.map(b => escapeHtml(b.title)).join(', ')}</p>` : ''}
        </div>`;
    } catch (e) { box.innerHTML = ''; }
  }, 400);
}

/* =====================================================================
   POINTS — a small toast when visiting the Digital Library for the
   first time today; reading points are handled in openMediaViewerAndTrackPoints.
===================================================================== */
async function trackDigitalLibraryVisit() {
  try {
    const res = await api('/api/student/visit-digital-library', { method: 'POST' });
    if (res.newly_awarded) toast(`📚 +2 points for visiting the Digital Library today! Total: ${res.points}`);
  } catch (e) { /* non-fatal */ }
}

/* =====================================================================
   MY LIBRARY (student-only) — My Books, History, Bookmarks, Favorites,
   Recently Read, Recommendations.
===================================================================== */
function renderMiniOnlineGrid(elId, books, emptyMsg) {
  document.getElementById(elId).innerHTML = books.map(b => {
    const ft = b.file_type || 'pdf';
    const icon = FILE_TYPE_ICON[ft] || '📁';
    return `<div class="online-card">
      <h4>${icon} ${escapeHtml(b.title)}</h4>
      <p class="hint">${escapeHtml(b.author || '')}${b.author ? ' · ' : ''}${escapeHtml(b.subject || '')}</p>
      <div class="io-row">
        <button class="brass" data-mini-read="${b.id}" data-type="${ft}" data-title="${escapeHtml(b.title)}">Read Online</button>
      </div>
    </div>`;
  }).join('') || `<p class="hint">${emptyMsg}</p>`;
}

// Shared click delegation for every mini-grid on the My Library page
// (Bookmarks/Favorites/Recently Read/Recommendations all use the same markup).
['myBookmarksGrid', 'myFavoritesGrid', 'myRecentGrid', 'myRecsGrid'].forEach(id => {
  document.getElementById(id).addEventListener('click', e => {
    const btn = e.target.closest('[data-mini-read]');
    if (!btn) return;
    openMediaViewerAndTrackPoints(btn.getAttribute('data-mini-read'), btn.getAttribute('data-type'), btn.getAttribute('data-title'));
  });
});

async function loadMyLibrary() {
  try {
    const data = await api('/api/student/my-library');
    document.getElementById('myLibraryPoints').innerHTML = [
      { n: data.points, l: '⭐ Total Points' },
      { n: data.my_books.length, l: 'Currently Borrowed' },
      { n: data.recently_read.length, l: 'Digital Items Read' },
      { n: data.bookmarks.length, l: 'Bookmarks' },
    ].map(s => `<div class="stat-card"><div class="stat-num">${s.n}</div><div class="stat-lbl">${s.l}</div></div>`).join('');

    document.getElementById('myBooksBody').innerHTML = data.my_books.map(i => `
      <tr><td>${escapeHtml(i.accession_no)}</td><td>${i.issue_date}</td><td>${i.due_date}</td><td>${statTag(i.status)}</td></tr>
    `).join('') || `<tr><td colspan="4" style="text-align:center;color:#9a9180;padding:16px;">No books currently borrowed.</td></tr>`;

    document.getElementById('myHistoryBody').innerHTML = data.history.map(i => `
      <tr><td>${escapeHtml(i.accession_no)}</td><td>${i.issue_date}</td><td>${i.due_date}</td><td>${i.return_date || '—'}</td><td>${statTag(i.status)}</td></tr>
    `).join('') || `<tr><td colspan="5" style="text-align:center;color:#9a9180;padding:16px;">No borrowing history yet.</td></tr>`;

    renderMiniOnlineGrid('myBookmarksGrid', data.bookmarks, 'No bookmarks yet — save items from the Digital Library to see them here.');
    renderMiniOnlineGrid('myFavoritesGrid', data.favorites, 'No favorites yet — star items from the Digital Library.');
    renderMiniOnlineGrid('myRecentGrid', data.recently_read, 'Nothing read yet — visit the Digital Library to get started.');
    renderMiniOnlineGrid('myRecsGrid', data.recommendations, 'Recommendations will appear here as you read and favorite items.');
  } catch (err) { toast(err.message, true); }
}

/* =====================================================================
   ACHIEVEMENTS — leaderboard, points banner, and digital certificates.
===================================================================== */
async function loadAchievements() {
  try {
    const rows = await api('/api/leaderboard');
    document.getElementById('leaderboardBody').innerHTML = rows.map((r, i) => `
      <tr><td>${i + 1}</td><td>${escapeHtml(r.name)}</td><td>${escapeHtml(r.class_dept || '—')}</td><td>${r.points}</td></tr>
    `).join('') || `<tr><td colspan="4" style="text-align:center;color:#9a9180;padding:16px;">No students yet.</td></tr>`;

    if (IS_STUDENT) {
      const mine = await api('/api/student/points');
      const banner = document.getElementById('myPointsBanner');
      banner.style.display = '';
      banner.innerHTML = `<div class="stat-row"><div class="stat-card"><div class="stat-num">${mine.points}</div><div class="stat-lbl">Your Points</div></div></div>`;
      const certs = await api('/api/certificates');
      renderCertGrid(certs, false);
    }

    if (IS_STAFF) {
      document.getElementById('awardCertificateBlock').style.display = '';
      const memberSelect = document.getElementById('certMemberSelect');
      memberSelect.innerHTML = rows.map(r => `<option value="${r.member_id}">${escapeHtml(r.name)} (${escapeHtml(r.class_dept || '—')}) — ${r.points} pts</option>`).join('')
        || '<option value="">No students registered yet</option>';
      const certs = await api('/api/certificates');
      renderCertGrid(certs, true);
    }
  } catch (err) { toast(err.message, true); }
}

function renderCertGrid(certs, staffView) {
  document.getElementById('myCertificatesGrid').innerHTML = certs.map(c => `
    <div class="online-card">
      <h4>🎓 ${escapeHtml(c.title)}</h4>
      <p class="hint">${staffView ? escapeHtml(c.member_id) + ' · ' : ''}Awarded ${c.awarded_date}${c.issued_by ? ' · ' + escapeHtml(c.issued_by) : ''}</p>
      <div class="io-row"><button class="brass" data-download-cert="${c.id}">Download</button></div>
    </div>
  `).join('') || `<p class="hint">No certificates awarded yet.</p>`;
}
document.getElementById('myCertificatesGrid').addEventListener('click', e => {
  const btn = e.target.closest('[data-download-cert]');
  if (btn) window.open(`/api/certificates/${btn.getAttribute('data-download-cert')}/download`);
});

const awardCertBtn = document.getElementById('awardCertBtn');
if (awardCertBtn) {
  awardCertBtn.addEventListener('click', async () => {
    const member_id = document.getElementById('certMemberSelect').value;
    const title = document.getElementById('certTitleSelect').value;
    if (!member_id) { toast('Choose a student first.', true); return; }
    try {
      await api('/api/certificates', { method: 'POST', body: JSON.stringify({ member_id, title }) });
      toast('Certificate awarded! 🎉');
      loadAchievements();
    } catch (err) { toast(err.message, true); }
  });
}

/* ---------- initial load ---------- */
loadHome();
if (IS_STUDENT) loadOnlineBooks();
