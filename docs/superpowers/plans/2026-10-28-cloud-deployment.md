# SmartLib 360 Cloud Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the full SmartLib 360 app at `library.msceducation.net` on Render + Neon + Cloudflare R2, with 30-minute file links, a "Need more time" button, Microsoft sign-in for students, class-targeted books and video resume — while the offline school-PC mode keeps working unchanged.

**Architecture:** One new `storage.py` module hides where files live (local `uploads/` or R2) behind a single interface; `app.py` routes every file read/write through it. The existing schema is kept and only gains four nullable columns; `_auto_migrate_schema` is made dialect-agnostic so those columns also reach Postgres. Cloud-only behaviour (R2, Microsoft sign-in, JSON backups) switches on purely from environment variables, so a run with none of them set behaves exactly as today.

**Tech Stack:** Python 3.12, Flask 3.1, Flask-SQLAlchemy, gunicorn (gthread), Postgres (Neon) / SQLite, boto3 (R2 via S3 API), msal (Microsoft Entra ID), pytest + moto (tests), vanilla JS frontend in `static/js/app.js`.

**Spec:** `docs/superpowers/specs/2026-10-28-cloud-deployment-design.md`

## Global Constraints

- **Offline mode is untouched:** with no `DATABASE_URL`, no `R2_*`, no `ENTRA_*` variables set, every route must behave as before — files under `BASE_DIR/uploads/`, `/api/online-books/<id>/view` streams the file, SQLite file backups.
- **File keys:** stored paths stay relative keys of the form `uploads/<folder>/<name>` with forward slashes. Legacy rows containing backslashes (`uploads\pdfs\x.pdf`, written on Windows) must resolve in both modes.
- **R2 config is all-or-nothing:** `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` — all four set = R2, none set = local, anything else = `RuntimeError` at startup. Same rule for `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET` (and Entra additionally requires `PUBLIC_BASE_URL`).
- **Link lifetime:** `LINK_TTL_SECONDS = 1800` (overridable via `LIBRARY_LINK_TTL_SECONDS` for testing); warning shown 300 s before expiry; renewal requires a valid login; no cap on renewals. Timer bar is for video/audio only.
- **Images and backups links:** presigned URLs for QR codes, barcodes and backup downloads use `FILE_LINK_TTL_SECONDS = 300`.
- **Microsoft sign-in:** students only; accept only `tid == ENTRA_TENANT_ID`; never use `response_mode=form_post` (the `SameSite=Lax` session cookie would not be sent back).
- **Regions:** Neon `aws-ap-southeast-1`, R2 location hint Asia-Pacific, Render Singapore.
- **Single app process:** `SERVER_INSTANCE_ID` is per-process, so gunicorn must run `--workers 1 --threads 8`.
- **Dependencies:** pin exact versions (`==`) in `requirements.txt` following its existing comment style; test-only packages go in a new `requirements-dev.txt`.
- **Never commit** `database.db`, `instance_secret.key`, `uploads/` contents, or any R2/Entra/Neon secret.
- **Style:** match `app.py`'s existing habit of explanatory block comments on non-obvious decisions; no new frameworks.
- **Branch:** all work on branch `cloud-deployment`, never directly on `main`.

## Review Focus

1. **Legacy Windows paths** — a row with `pdf_path = "uploads\\pdfs\\x.pdf"` must open in local and R2 mode (tests in Task 3 and Task 5).
2. **Login expired while watching** (Render restart wipes sessions via `SERVER_INSTANCE_ID`) — the link endpoint must return `401` JSON, not a redirect to the HTML login page that `fetch` silently follows (test in Task 6).
3. **Unlinked or outside Microsoft accounts** — no library card is created or guessed; the student sees a plain explanation (tests in Task 9).
4. **R2 upload fails mid-way** — no `OnlineBook` row is created and no temp file is left behind (test in Task 5).
5. **Half-configured R2** (3 of 4 variables set on Render) — startup fails loudly instead of silently writing files to Render's wiped disk (test in Task 3).

---

### Task 1: Test harness and data-directory seam

There are no tests today, and importing `app.py` opens `BASE_DIR/database.db` — the real local database. This task adds an override so tests run against a throwaway directory.

**Files:**
- Modify: `app.py:45-59` (`_get_base_dir`)
- Create: `tests/conftest.py`, `tests/test_harness.py`, `requirements-dev.txt`, `pytest.ini`

**Interfaces:**
- Produces:
  - `_get_base_dir()` returns `os.environ["LIBRARY_DATA_DIR"]` when set (before the frozen/.exe branch).
  - `tests/conftest.py` fixtures: `app_module` (the imported `app` module), `db_reset` (autouse: `drop_all` + `create_all` + `security.ensure_security_tables(DB_PATH)` + `security.mark_setup_complete(DB_PATH)` + delete all rows from `login_rate_limits`), `client` (test client whose session holds `_csrf_token = "test-csrf"` and which sends header `X-CSRF-Token: test-csrf`), helper functions `make_admin() -> User`, `make_student(admission_no="A100", dob="2014-05-01", class_dept="7-A", **fields) -> Member` (extra `fields` such as `email`, `entra_oid` are set on the row once those columns exist in Task 2), `login_as(client, user)` (sets `_user_id`, `_fresh=True`, `srv = app_module.SERVER_INSTANCE_ID` in the session).
  - `conftest.py` sets `LIBRARY_DATA_DIR` to a session temp dir and **deletes** `DATABASE_URL`, all `R2_*`, all `ENTRA_*`, `PUBLIC_BASE_URL` from `os.environ` **before** importing `app`.

- [ ] **Step 1: Create branch and dev environment**

```bash
git checkout -b cloud-deployment
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest moto
```
Then write `requirements-dev.txt` with `-r requirements.txt` plus `pytest==<installed>` and `moto[s3]==<installed>` (exact versions from `pip freeze`). `pytest.ini`: `testpaths = tests`.

- [ ] **Step 2: Write the failing tests** in `tests/test_harness.py`

```python
def test_app_uses_isolated_data_dir(app_module, tmp_path_factory):
    assert "MSC-Library/database.db" not in app_module.DB_PATH
    assert os.environ["LIBRARY_DATA_DIR"] in app_module.DB_PATH

def test_login_as_admin_reaches_whoami(client):
    login_as(client, make_admin())
    r = client.get("/api/whoami")
    assert r.status_code == 200 and r.get_json()["role"] == "admin"
```

- [ ] **Step 3: Run** `python -m pytest tests/test_harness.py -v` — expected: FAIL (DB_PATH points at the repo).

- [ ] **Step 4: Implement** the `LIBRARY_DATA_DIR` branch in `_get_base_dir()` and the fixtures in `tests/conftest.py`.

- [ ] **Step 5: Run** `python -m pytest -v` — expected: 2 passed. Confirm `git status` shows `database.db` unchanged.

- [ ] **Step 6: Commit** — `git add app.py tests/ requirements-dev.txt pytest.ini && git commit -m "test: add pytest harness with isolated data dir"`

---

### Task 2: New columns, and schema auto-migration that also works on Postgres

`_auto_migrate_schema()` (`app.py:2998`) only opens the SQLite file, so on Neon new model columns would never be added and every query touching them would crash.

**Files:**
- Modify: `models.py` (`Member`, `OnlineBook`, `ReadHistory`), `app.py:2998-3077` (`_auto_migrate_schema`, `init_db`)
- Test: `tests/test_migration.py`

**Interfaces:**
- Produces model columns (all nullable, no unique constraints at DB level):
  - `Member.email = db.Column(db.String(254))` — stored lowercased
  - `Member.entra_oid = db.Column(db.String(64))`
  - `OnlineBook.grade_target = db.Column(db.Integer)` — 1–12, NULL = all classes
  - `ReadHistory.progress_seconds = db.Column(db.Integer, default=0)`
- Produces `_auto_migrate_schema() -> list[str]` — runs **inside** an app context, uses `sqlalchemy.inspect(db.engine)` to compare live tables with `db.Model` mappers, issues `ALTER TABLE <t> ADD COLUMN <c> <type> [DEFAULT …]` via `db.engine.begin()`, returns the `"table.column"` names it added. Column type comes from `col.type.compile(dialect=db.engine.dialect)`. `init_db()` calls it after `db.create_all()`, still inside the app context.

- [ ] **Step 1: Write the failing test**

```python
def test_auto_migrate_adds_missing_columns(app_module):
    with app_module.app.app_context():
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE members DROP COLUMN email"))
        added = app_module._auto_migrate_schema()
        assert "members.email" in added
        cols = {c["name"] for c in inspect(db.engine).get_columns("members")}
        assert "email" in cols

def test_auto_migrate_is_noop_when_schema_current(app_module):
    with app_module.app.app_context():
        assert app_module._auto_migrate_schema() == []
```
(SQLite ≥ 3.35 supports `DROP COLUMN`; Python 3.12 ships a new enough SQLite.)

- [ ] **Step 2: Run** `python -m pytest tests/test_migration.py -v` — expected: FAIL.
- [ ] **Step 3: Implement** the columns and the inspector-based `_auto_migrate_schema()`; keep the existing "never removes a column" docstring promise.
- [ ] **Step 4: Run** `python -m pytest -v` — expected: all pass.
- [ ] **Step 5: Manual check against the old local DB:** `cp database.db /tmp/old.db && LIBRARY_DATA_DIR=/tmp python -c "import app"` with `/tmp/database.db` = that copy; then `sqlite3 /tmp/database.db "pragma table_info(members)"` lists `email` and `entra_oid`.
- [ ] **Step 6: Commit** — `git commit -am "feat: add email/grade/progress columns; dialect-agnostic auto-migration"`

---

### Task 3: Storage module (local disk or R2)

**Files:**
- Create: `storage.py`, `tests/test_storage.py`
- Modify: `requirements.txt` (add `boto3==<latest>` with a comment in the file's existing style), `tests/conftest.py` (add `r2` fixture)

**Interfaces:**
- Produces in `storage.py`:
  - `normalize_key(key: str) -> str` — backslashes → `/`, strips leading `/`, raises `ValueError` if any segment is `..` or the result is empty.
  - `class LocalStorage(base_dir: str)`, attribute `remote = False`
  - `class R2Storage(client, bucket: str)`, attribute `remote = True`
  - Both implement:
    - `save_file(src_path: str, key: str, content_type: str | None = None) -> str` — returns the normalized key; never modifies or deletes `src_path`.
    - `save_bytes(data: bytes, key: str, content_type: str | None = None) -> str`
    - `delete(key: str | None) -> None` — `None`/missing key is a no-op; never raises.
    - `list_keys(prefix: str) -> list[str]`
  - `LocalStorage.local_path(key: str) -> str` — absolute path under `base_dir`.
  - `R2Storage.presigned_url(key: str, expires_in: int, content_type: str | None = None, download_name: str | None = None) -> str` — `ResponseContentDisposition` is `inline` when `download_name` is None, else `attachment; filename="<download_name>"`.
  - `get_storage(base_dir: str, env: Mapping[str, str] = os.environ) -> LocalStorage | R2Storage` — builds the boto3 client with `endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"`, `region_name="auto"`, `Config(signature_version="s3v4")`.
- Produces in `conftest.py`: fixture `r2` → inside `moto.mock_aws()`, creates bucket `test-bucket` with a `us-east-1` boto3 client, monkeypatches `app_module.storage = R2Storage(client, "test-bucket")`, yields the storage.

- [ ] **Step 1: Write the failing tests**

```python
def test_normalize_key_converts_windows_paths():
    assert normalize_key("uploads\\pdfs\\a.pdf") == "uploads/pdfs/a.pdf"

def test_normalize_key_rejects_traversal():
    with pytest.raises(ValueError):
        normalize_key("uploads/../instance_secret.key")

def test_local_roundtrip(tmp_path):
    s = LocalStorage(str(tmp_path)); s.save_bytes(b"hi", "uploads/qrcodes/x.png")
    assert open(s.local_path("uploads\\qrcodes\\x.png"), "rb").read() == b"hi"
    assert s.list_keys("uploads/qrcodes/") == ["uploads/qrcodes/x.png"]
    s.delete("uploads/qrcodes/x.png"); s.delete("uploads/qrcodes/x.png")  # second call is a no-op
    assert s.list_keys("uploads/qrcodes/") == []

def test_r2_roundtrip_and_presign(r2):
    r2.save_bytes(b"%PDF", "uploads/pdfs/a.pdf", "application/pdf")
    url = r2.presigned_url("uploads/pdfs/a.pdf", expires_in=1800)
    assert "X-Amz-Expires=1800" in url and "response-content-disposition=inline" in url
    assert r2.list_keys("uploads/pdfs/") == ["uploads/pdfs/a.pdf"]

def test_get_storage_local_when_no_r2_vars(tmp_path):
    assert isinstance(get_storage(str(tmp_path), env={}), LocalStorage)

def test_get_storage_rejects_partial_r2_config(tmp_path):
    env = {"R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "b", "R2_BUCKET_NAME": "c"}
    with pytest.raises(RuntimeError, match="R2_SECRET_ACCESS_KEY"):
        get_storage(str(tmp_path), env=env)
```

- [ ] **Step 2: Run** `python -m pytest tests/test_storage.py -v` — expected: FAIL (`storage` missing).
- [ ] **Step 3: Implement** `storage.py`. The `RuntimeError` message names every missing variable.
- [ ] **Step 4: Run** `python -m pytest -v` — expected: all pass.
- [ ] **Step 5: Commit** — `git commit -am "feat: add storage layer for local disk or Cloudflare R2"` (include the new files with `git add`).

---

### Task 4: QR codes, barcodes and `/uploads/` through storage

**Files:**
- Modify: `app.py` — module-level `storage`, `generate_barcode_for_book` (1058), `generate_qr_for_member` (1162), `_generate_qr_for_online_book` (1457), `serve_upload` (893), `purge_incomplete_books` barcode removal (~2920)
- Test: `tests/test_generated_images.py`

**Interfaces:**
- Consumes: `storage.get_storage`, `LocalStorage`, `R2Storage` (Task 3).
- Produces in `app.py`:
  - `storage = get_storage(BASE_DIR)` right after `BASE_DIR` is set; **every** later reference uses the module global `storage` so tests can monkeypatch it.
  - `PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")`
  - `FILE_LINK_TTL_SECONDS = 300`
  - `_send_stored(key: str, *, mimetype: str | None = None, download_name: str | None = None, expires_in: int = FILE_LINK_TTL_SECONDS) -> Response` — local: `send_file(storage.local_path(key), …)` (404 if missing, inline unless `download_name`); R2: `redirect(storage.presigned_url(…), 302)`. Invalid keys → 404.
  - Barcode/QR images are rendered to bytes in memory (`BytesIO`) and saved via `storage.save_bytes`; the model columns keep the same key format (`uploads/barcodes/<accession>.png`, `uploads/qrcodes/<member_id>.png`, `uploads/qrcodes/online_<id>.png`).
  - Online-book QR target: `f"{PUBLIC_BASE_URL}/api/online-books/{id}/view"` when `PUBLIC_BASE_URL` is set, otherwise the existing LAN URL.
- The frontend links (`/${b.barcode_path}`, `/${m.qr_path}`) keep working unchanged because `/uploads/<path>` now goes through `_send_stored`.

- [ ] **Step 1: Write the failing tests**

```python
def test_member_qr_saved_and_served_locally(client):
    login_as(client, make_admin())
    m = client.post("/api/members", json={"name": "Asha", "class_dept": "7-A"}).get_json()
    assert m["qr_path"] == f"uploads/qrcodes/{m['member_id']}.png"
    r = client.get("/" + m["qr_path"])
    assert r.status_code == 200 and r.mimetype == "image/png"

def test_book_barcode_in_r2_redirects(client, r2):
    login_as(client, make_admin())
    b = client.post("/api/books", json={"title": "Atlas"}).get_json()
    assert r2.list_keys("uploads/barcodes/") == [b["barcode_path"]]
    r = client.get("/" + b["barcode_path"])
    assert r.status_code == 302 and "X-Amz-Expires=300" in r.location

def test_online_book_qr_uses_public_base_url(app_module, monkeypatch, client):
    monkeypatch.setattr(app_module, "PUBLIC_BASE_URL", "https://library.msceducation.net")
    # decode the saved PNG with a QR reader is overkill — assert on the payload passed to qrcode.make
    ...  # monkeypatch qrcode.make to capture its argument; expect "https://library.msceducation.net/api/online-books/<id>/view"

def test_uploads_route_rejects_traversal(client):
    login_as(client, make_admin())
    assert client.get("/uploads/../instance_secret.key").status_code == 404
```

- [ ] **Step 2: Run** `python -m pytest tests/test_generated_images.py -v` — expected: FAIL.
- [ ] **Step 3: Implement** as specified above. Check the barcode library's `save()` behaviour: `python-barcode` appends `.png` itself; use `code.write(buf)` into a `BytesIO` instead so the key is fully under our control.
- [ ] **Step 4: Run** `python -m pytest -v` — expected: all pass.
- [ ] **Step 5: Commit** — `git commit -am "feat: route QR codes, barcodes and /uploads through storage"`

---

### Task 5: Digital library files through storage

**Files:**
- Modify: `app.py` — `_convert_to_pdf_preview` (1502), `_save_digital_upload` (1563), `view_online_book` (1626), `download_original_online_book` (1661), `edit_online_book` (1706), `delete_online_book` (1750)
- Test: `tests/test_digital_files.py`

**Interfaces:**
- Consumes: `storage`, `_send_stored` (Task 4).
- Produces:
  - `_save_digital_upload(file, subject_hint=None, category_hint=None, title_hint=None) -> OnlineBook` (same signature). New flow: save upload to a `tempfile.mkdtemp()` directory → size check → `_extract_content_text` → `_convert_to_pdf_preview(source_path, ext, out_dir) -> str | None` (now returns an **absolute temp path**, not a key) → `storage.save_file` for the original (`uploads/pdfs/<token>_<name>`) and preview (`uploads/pdfs/<token>_<stem>.pdf`) with the mimetype from `DIGITAL_LIBRARY_TYPES` → temp dir removed in `finally`. If any storage call raises, already-saved keys are deleted and the exception propagates (so no `OnlineBook` is created).
  - `view_online_book(book_id)`: logs read history **unless** `request.args.get("src") == "app"`; then `_send_stored(book.preview_pdf_path or book.pdf_path, mimetype=…, expires_in=LINK_TTL_SECONDS)`. Office files without preview → `download_name=f"{book.title}.{book.file_type}"`. Local responses keep the existing `Content-Disposition`, `nosniff`, `Cache-Control: no-store` headers.
  - Edit/delete remove old keys with `storage.delete` **after** the DB commit succeeds.
  - `LINK_TTL_SECONDS = int(os.environ.get("LIBRARY_LINK_TTL_SECONDS", "1800"))` defined here (used again in Task 6).

- [ ] **Step 1: Write the failing tests**

```python
def _upload(client, name="VII_SCIENCE.pdf", data=b"%PDF-1.4 tiny"):
    return client.post("/api/online-books", data={"title": "Sci", "file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")

def test_upload_to_r2_leaves_no_local_file(client, r2, app_module):
    login_as(client, make_admin())
    book = _upload(client).get_json()
    assert len(r2.list_keys("uploads/pdfs/")) == 1
    assert not os.listdir(os.path.join(app_module.BASE_DIR, "uploads", "pdfs"))  # dir may not exist: treat missing as empty

def test_view_redirects_to_30_minute_link_in_r2(client, r2):
    login_as(client, make_admin()); book = _upload(client).get_json()
    r = client.get(f"/api/online-books/{book['id']}/view")
    assert r.status_code == 302 and "X-Amz-Expires=1800" in r.location

def test_view_streams_locally(client):
    login_as(client, make_admin()); book = _upload(client).get_json()
    r = client.get(f"/api/online-books/{book['id']}/view")
    assert r.status_code == 200 and r.data.startswith(b"%PDF")

def test_legacy_backslash_path_still_opens(client, app_module):
    # create OnlineBook row directly with pdf_path="uploads\\pdfs\\old.pdf" and that file on disk
    ...
    assert client.get(f"/api/online-books/{book_id}/view").status_code == 200

def test_failed_r2_upload_creates_no_row(client, r2, monkeypatch):
    login_as(client, make_admin())
    monkeypatch.setattr(r2, "save_file", lambda *a, **k: (_ for _ in ()).throw(OSError("network")))
    assert _upload(client).status_code == 500
    assert OnlineBook.query.count() == 0

def test_delete_removes_objects(client, r2):
    login_as(client, make_admin()); book = _upload(client).get_json()
    client.delete(f"/api/online-books/{book['id']}")
    assert r2.list_keys("uploads/") == []
```

- [ ] **Step 2: Run** `python -m pytest tests/test_digital_files.py -v` — expected: FAIL.
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run** `python -m pytest -v` — expected: all pass.
- [ ] **Step 5: Commit** — `git commit -am "feat: store digital library files via storage; R2 view uses 30-minute links"`

---

### Task 6: Link and progress API

**Files:**
- Modify: `app.py` (new routes beside `view_online_book`)
- Test: `tests/test_links.py`

**Interfaces:**
- Consumes: `LINK_TTL_SECONDS`, `storage`, `_log_read_history`, `_current_student_member`.
- Produces:
  - `GET /api/online-books/<id>/link[?renew=1]` — **no** `@login_required` decorator; if `not current_user.is_authenticated` return `401 {"error": "Please sign in again to keep reading.", "login_required": true}`. For students, calls `_log_read_history` unless `renew=1`. Response:
    ```json
    {"url": "...", "expires_in": 1800, "warn_at": 1500, "file_type": "mp4", "progress_seconds": 0}
    ```
    `warn_at = max(expires_in - 300, expires_in // 2)`. Local mode: `url = "/api/online-books/<id>/view?src=app"`, `expires_in` and `warn_at` are `null`. R2 mode: `url` is the presigned URL of the preview (if any) else the original. `progress_seconds` is the student's `ReadHistory.progress_seconds` (0 for staff).
  - `POST /api/online-books/<id>/progress` body `{"seconds": <int ≥ 0>}` — students only (`403` otherwise); `400` on a non-integer or negative value; updates the student's `ReadHistory` row; returns `{"ok": true}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_link_requires_login_as_json(client):
    r = client.get("/api/online-books/1/link")
    assert r.status_code == 401 and r.get_json()["login_required"] is True

def test_local_link_has_no_expiry(client, sample_book):
    login_as(client, make_admin())
    j = client.get(f"/api/online-books/{sample_book.id}/link").get_json()
    assert j["url"] == f"/api/online-books/{sample_book.id}/view?src=app" and j["expires_in"] is None

def test_r2_link_expires_in_30_minutes_and_warns_at_25(client, r2, sample_book):
    login_as(client, make_admin())
    j = client.get(f"/api/online-books/{sample_book.id}/link").get_json()
    assert (j["expires_in"], j["warn_at"]) == (1800, 1500)

def test_renew_does_not_bump_open_count(client, sample_book):
    s = make_student(); login_as(client, student_user(s))
    client.get(f"/api/online-books/{sample_book.id}/link")
    client.get(f"/api/online-books/{sample_book.id}/link?renew=1")
    assert ReadHistory.query.one().open_count == 1

def test_view_with_src_app_does_not_log(client, sample_book):
    s = make_student(); login_as(client, student_user(s))
    client.get(f"/api/online-books/{sample_book.id}/view?src=app")
    assert ReadHistory.query.count() == 0

def test_progress_saved_and_returned(client, sample_book):
    s = make_student(); login_as(client, student_user(s))
    client.get(f"/api/online-books/{sample_book.id}/link")
    assert client.post(f"/api/online-books/{sample_book.id}/progress", json={"seconds": 95}).status_code == 200
    assert client.get(f"/api/online-books/{sample_book.id}/link?renew=1").get_json()["progress_seconds"] == 95

def test_progress_rejects_staff_and_bad_values(client, sample_book):
    login_as(client, make_admin())
    assert client.post(f"/api/online-books/{sample_book.id}/progress", json={"seconds": 5}).status_code == 403
```
Add `sample_book` (local file + `OnlineBook` row, `file_type="mp4"`) and `student_user(member) -> User` helpers to `conftest.py`.

- [ ] **Step 2: Run** `python -m pytest tests/test_links.py -v` — expected: FAIL.
- [ ] **Step 3: Implement** both routes.
- [ ] **Step 4: Run** `python -m pytest -v` — expected: all pass.
- [ ] **Step 5: Commit** — `git commit -am "feat: add link (30-min, renewable) and playback progress endpoints"`

---

### Task 7: Viewer — "Need more time" bar and resume

**Files:**
- Modify: `static/js/app.js:1000-1030` (`openMediaViewer`, `closeMediaViewer`), `static/css/style.css`

**Interfaces:**
- Consumes: `GET /link`, `POST /progress` (Task 6); existing `api()` helper (`app.js:57`).
- Produces (frontend):
  - `openMediaViewer(id, type, title)` becomes `async`; it first calls `/api/online-books/${id}/link`, then builds the same markup as today using `link.url`.
  - For `mp4`/`webm`/`mp3`/`wav`/`m4a` with `link.expires_in != null`: a `setTimeout` at `link.warn_at` seconds shows a bar inside `.pdf-frame-wrap`: **"Your access ends in 5 minutes."** + button **"Need more time"** (`id="renewLinkBtn"`). When `expires_in - warn_at < 300` the text uses the real remaining minutes, rounded up.
  - Clicking calls `/link?renew=1`; on success: remember `currentTime` and paused state, set `src`, restore both on `loadedmetadata`, hide the bar, schedule the next warning from the new response. On a `401`, the bar text becomes **"You've been signed out. Sign in again to continue."** with a link to `/login`. (`api()` throws on non-OK; check the error, or call `fetch` directly for this one request so the status is visible.)
  - Resume: on `loadedmetadata`, if `progress_seconds > 5` and `< duration - 5`, set `currentTime = progress_seconds`.
  - Progress: students only — `POST /progress` at most every 15 s from `timeupdate`, and once on close with `keepalive: true`.
  - `closeMediaViewer()` clears the timers.
  - PDF/EPUB: no timer.

- [ ] **Step 1: Implement** the changes above.
- [ ] **Step 2: Manual check, local mode** — `python app.py`, sign in as a student, open a PDF and a video: both open as before, no timer bar.
- [ ] **Step 3: Manual check, short links** — run with `LIBRARY_LINK_TTL_SECONDS=120` and R2 variables pointing at a test bucket (or a dev R2 bucket): open a video → after 60 s the bar shows "Your access ends in 1 minute." → click "Need more time" → playback continues from the same second, no reload flash, bar hides, warns again ~60 s later. Use the `run` skill / Playwright to drive and screenshot this.
- [ ] **Step 4: Manual check, expired login** — while the bar is showing, restart the server (clears sessions), click "Need more time" → "You've been signed out…" with a working sign-in link.
- [ ] **Step 5: Manual check, resume** — watch 40 s, close, reopen → starts at ~40 s.
- [ ] **Step 6: Commit** — `git commit -am "feat: viewer requests timed links, adds Need more time bar and resume"`

---

### Task 8: Books for my class (`grade_target`)

**Files:**
- Modify: `app.py` (`list_online_books` 1605, `add_online_book` 1671, `edit_online_book` 1706, `bulk_import_online_books` 1769), `models.py` (`OnlineBook.to_dict` adds `grade_target`), `static/js/app.js` (online-book form ~1150, `loadOnlineBooks` 748), `templates/index.html` (beside `#onlineSearch`, line 228)
- Test: `tests/test_grades.py`

**Interfaces:**
- Produces:
  - `_grade_from_class(class_dept: str | None) -> int | None` — leading Arabic number or Roman numeral I–XII, optionally preceded by "Class"/"Std"; anything else → `None`.
  - Add/edit/bulk accept form field `grade_target`: blank → `None`, integer 1–12, anything else → `400 "Class must be between 1 and 12."`.
  - `GET /api/online-books` for a **student** whose grade parses: returns only items with `grade_target IS NULL OR grade_target = <grade>`, unless `all_grades=1`. Staff are never filtered.
  - Frontend: admin form select `id="ob_grade"` ("All classes", 1–12); student-only checkbox `id="onlineAllGrades"` labelled **"Show books for all classes"** (unchecked by default) that adds `&all_grades=1`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("raw,expected", [
    ("7-A", 7), ("VII", 7), ("VII-B", 7), ("Class 10", 10), ("Std XII", 12),
    ("12 Science", 12), ("LKG", None), ("", None), (None, None), ("13", None),
])
def test_grade_from_class(app_module, raw, expected):
    assert app_module._grade_from_class(raw) == expected

def test_student_sees_own_class_and_untargeted(client):
    # books: grade 7, grade 8, None; student class "7-A"
    ...
    titles = {b["title"] for b in client.get("/api/online-books").get_json()}
    assert titles == {"G7", "Any"}
    assert len(client.get("/api/online-books?all_grades=1").get_json()) == 3

def test_bad_grade_rejected(client):
    login_as(client, make_admin())
    r = client.post("/api/online-books", data={"grade_target": "14", "file": (io.BytesIO(b"%PDF"), "a.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 400
```

- [ ] **Step 2: Run** `python -m pytest tests/test_grades.py -v` — expected: FAIL.
- [ ] **Step 3: Implement** backend, then frontend.
- [ ] **Step 4: Run** `python -m pytest -v` — expected: all pass. Manual check: admin sets a book to Class 8; a Class 7 student doesn't see it until ticking "Show books for all classes".
- [ ] **Step 5: Commit** — `git commit -am "feat: target digital books at a class; students see their class by default"`

---

### Task 9: Sign in with Microsoft (students)

**Files:**
- Modify: `app.py` (login refactor 528-630, `add_member` 1099, `update_member` 1118, `import_members` 2688, `MEMBER_IMPORT_COLUMN_ALIASES` 2808, new routes), `models.py` (`Member.to_dict` adds `email`, `ms_linked`), `templates/login.html`, `static/js/app.js` (member form ~528), `requirements.txt` (add `msal==<latest>`)
- Test: `tests/test_microsoft_login.py`

**Interfaces:**
- Produces in `app.py`:
  - `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET` from env; `ENTRA_ENABLED: bool`; partial config or Entra without `PUBLIC_BASE_URL` → `RuntimeError` at import.
  - `_entra_app() -> msal.ConfidentialClientApplication` with `authority=f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}"` (tests monkeypatch this function).
  - `_finish_student_login(member: Member) -> User` — extracted from the end of the PIN branch of `login()` (provision `student-<member_id>` User if missing, `login_user`, `session.permanent = False`, `session["srv"] = SERVER_INSTANCE_ID`). `login()` calls it; behaviour unchanged.
  - `GET /auth/microsoft/login` — `404` unless `ENTRA_ENABLED`; `flow = _entra_app().initiate_auth_code_flow(["User.Read"], redirect_uri=PUBLIC_BASE_URL + "/auth/microsoft/callback")`; store in `session["entra_flow"]`; redirect to `flow["auth_uri"]`.
  - `GET /auth/microsoft/callback` — `acquire_token_by_auth_code_flow(session.pop("entra_flow", {}), request.args.to_dict())`; on `"error"` or exception → redirect `/login?ms_error=failed`. From `id_token_claims`: if `tid != ENTRA_TENANT_ID` → `ms_error=wrong_school`. Find `Member(type="Student")` by `entra_oid == oid`, else by `email == preferred_username.lower()` **where `entra_oid` is NULL** (then set `entra_oid = oid`). Nothing found → `ms_error=not_linked`. Otherwise `_finish_student_login(member)` → redirect `/`.
  - `/login` GET passes `entra_enabled` and the `ms_error` message to the template. Messages:
    - `not_linked`: "Your Microsoft account isn't linked to a library card yet. Ask the librarian to add your school email to your library card."
    - `wrong_school`: "Please sign in with your school Microsoft account."
    - `failed`: "Microsoft sign-in didn't complete. Please try again."
  - Members API: `email` accepted on add/update/import (strip + lowercase, blank → `None`); duplicate → `409 "That email is already on another library card."`; changing a member's email clears `entra_oid`. Import alias `"email": ["email", "email id", "email address", "school email", "mail id"]`.
- Frontend: login page shows a **"Sign in with Microsoft"** button (link to `/auth/microsoft/login`) in Student mode only when `entra_enabled`, above the admission-number fields with an "or" divider; member form gets a "School Email" field next to Parent Phone (`id="m_email"`).

- [ ] **Step 1: Write the failing tests** (fake MSAL app returning fixed claims)

```python
CLAIMS = {"tid": "tenant-1", "oid": "oid-1", "preferred_username": "Asha@MSC.edu"}

def test_links_by_email_then_binds_oid(entra, client):
    m = make_student(email="asha@msc.edu")
    entra.claims = CLAIMS
    client.get("/auth/microsoft/login")
    r = client.get("/auth/microsoft/callback?code=x&state=y")
    assert r.status_code == 302 and r.location.endswith("/")
    assert db.session.get(Member, m.id).entra_oid == "oid-1"

def test_bound_account_still_works_after_email_change_in_entra(entra, client):
    make_student(email="asha@msc.edu", entra_oid="oid-1")
    entra.claims = {**CLAIMS, "preferred_username": "asha.k@msc.edu"}
    ...
    assert client.get("/api/whoami").get_json()["role"] == "student"

def test_unknown_email_not_linked(entra, client):
    entra.claims = CLAIMS
    ...
    assert "ms_error=not_linked" in r.location and User.query.filter_by(role="student").count() == 0

def test_other_tenant_rejected(entra, client):
    make_student(email="asha@msc.edu"); entra.claims = {**CLAIMS, "tid": "other"}
    ...
    assert "ms_error=wrong_school" in r.location

def test_routes_404_when_not_configured(client):
    assert client.get("/auth/microsoft/login").status_code == 404

def test_duplicate_member_email_rejected(client):
    login_as(client, make_admin()); make_student(email="a@msc.edu")
    r = client.post("/api/members", json={"name": "B", "email": " A@MSC.edu "})
    assert r.status_code == 409
```
The `entra` fixture monkeypatches `ENTRA_ENABLED=True`, `ENTRA_TENANT_ID="tenant-1"`, `PUBLIC_BASE_URL="https://library.example"`, and `_entra_app` → a fake whose `initiate_auth_code_flow` returns `{"auth_uri": "https://login.example/auth", "state": "y"}` and whose `acquire_token_by_auth_code_flow` returns `{"id_token_claims": fake.claims}`.

- [ ] **Step 2: Run** `python -m pytest tests/test_microsoft_login.py -v` — expected: FAIL.
- [ ] **Step 3: Implement** the refactor, routes, member email handling, then the template and form.
- [ ] **Step 4: Run** `python -m pytest -v` — expected: all pass, including the existing PIN login (add a regression test `test_pin_login_unchanged` covering need_pin → correct PIN → logged in).
- [ ] **Step 5: Commit** — `git commit -am "feat: Sign in with Microsoft for students, matched by school email"`

---

### Task 10: Backups on Postgres

**Files:**
- Modify: `app.py` (`backup_now` 2944, `download_backup` 2956, `list_backups` 2964, startup block ~3080), `backup_scheduler.py`
- Test: `tests/test_backups.py`

**Interfaces:**
- Produces:
  - `_export_all_tables_json() -> bytes` — UTF-8 JSON `{"exported_at": "<ISO>", "tables": {"<table>": [ {col: value} ]}}` for **every** table from `inspect(db.engine).get_table_names()` (covers security tables too); non-JSON values via `default=str`.
  - `backup_now()`: Postgres (`_database_url` set) → `storage.save_bytes(..., "uploads/backups/backup_<YYYY_mm_dd_HHMMSS>.json", "application/json")`; SQLite → existing file copy, saved through `storage.save_file` to `uploads/backups/backup_<stamp>.db`. Returns `{"ok": true, "file": "<basename>"}` either way.
  - `list_backups()` → basenames from `storage.list_keys("uploads/backups/")`, newest first.
  - `download_backup(filename)` → `_send_stored("uploads/backups/" + secure_filename(filename), download_name=filename)`.
  - `_prune_backups(retention_days: int) -> int` — deletes backups whose timestamp in the name is older than `retention_days`; returns the count.
  - `backup_scheduler.start_cloud_backups(backup_fn: Callable[[], str], prune_fn: Callable[[int], int], logger) -> None` — same startup-then-every-`BACKUP_INTERVAL_HOURS` loop and `BACKUP_RETENTION_DAYS` as the existing function, in a daemon thread. `app.py` starts it at import time only when `_database_url` is set (outside the `__main__` guard, so gunicorn runs it).

- [ ] **Step 1: Write the failing tests**

```python
def test_json_export_contains_every_table(app_module):
    make_student()
    data = json.loads(app_module._export_all_tables_json())
    assert {"members", "books", "app_security", "login_rate_limits"} <= set(data["tables"])
    assert data["tables"]["members"][0]["admission_no"] == "A100"

def test_backup_now_postgres_mode_writes_json_to_storage(client, r2, monkeypatch, app_module):
    monkeypatch.setattr(app_module, "_database_url", "postgresql://fake")
    login_as(client, make_admin())
    name = client.post("/api/backup").get_json()["file"]
    assert name.endswith(".json") and r2.list_keys("uploads/backups/") == ["uploads/backups/" + name]

def test_prune_deletes_only_old_backups(app_module):
    app_module.storage.save_bytes(b"{}", "uploads/backups/backup_2020_01_01_000000.json")
    app_module.storage.save_bytes(b"{}", f"uploads/backups/backup_{datetime.now():%Y_%m_%d_%H%M%S}.json")
    assert app_module._prune_backups(60) == 1
```

- [ ] **Step 2: Run** `python -m pytest tests/test_backups.py -v` — expected: FAIL.
- [ ] **Step 3: Implement.** Note that in Postgres mode `backup_now` must not touch `database.db`.
- [ ] **Step 4: Run** `python -m pytest -v` — expected: all pass.
- [ ] **Step 5: Commit** — `git commit -am "feat: JSON backups to storage when running on Postgres"`

---

### Task 11: One-off script to move a school PC's data to Neon + R2

**Files:**
- Create: `migrate_to_cloud.py`, `tests/test_migrate_to_cloud.py`

**Interfaces:**
- Produces:
  - `copy_database(source_url: str, target_url: str) -> dict[str, int]` — reflects both databases; **aborts with `RuntimeError` if any target table has rows**; copies tables in `target_metadata.sorted_tables` order, only columns present in both; converts values to `bool` for target `Boolean` columns (SQLite stores 0/1); replaces `\` with `/` in `barcode_path`, `qr_path`, `pdf_path`, `preview_pdf_path`; on Postgres resets each integer `id` sequence with `SELECT setval(pg_get_serial_sequence('<t>', 'id'), COALESCE(MAX(id), 1)) FROM <t>`. Returns rows copied per table.
  - `copy_uploads(uploads_dir: str, target) -> int` — walks `uploads_dir`, saves each file (skipping `.gitkeep`) to key `uploads/<relative path with />` with a `mimetypes` content type; returns file count.
  - CLI: `python migrate_to_cloud.py --sqlite PATH/database.db --uploads PATH/uploads` reading `DATABASE_URL` and `R2_*` from the environment (via `storage.get_storage`, which must return `R2Storage`, else exit with a message). Prints per-table counts.
  - Precondition (in the script's docstring and the deploy guide): run the app once against the Neon `DATABASE_URL` so `init_db()` creates the tables, **before** anyone completes `/setup`.

- [ ] **Step 1: Write the failing tests** (SQLite → SQLite stands in for SQLite → Postgres)

```python
def test_copies_rows_and_fixes_windows_paths(tmp_path, seeded_source_db, empty_target_db):
    counts = copy_database(seeded_source_db, empty_target_db)
    assert counts["online_books"] == 1
    row = query_one(empty_target_db, "select pdf_path, ocr_applied from online_books")
    assert row == ("uploads/pdfs/old.pdf", 0) or row == ("uploads/pdfs/old.pdf", False)

def test_refuses_non_empty_target(seeded_source_db):
    with pytest.raises(RuntimeError, match="not empty"):
        copy_database(seeded_source_db, seeded_source_db)

def test_copy_uploads_to_r2(tmp_path, r2):
    (tmp_path / "pdfs").mkdir(); (tmp_path / "pdfs" / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "pdfs" / ".gitkeep").write_bytes(b"")
    assert copy_uploads(str(tmp_path), r2) == 1
    assert r2.list_keys("uploads/") == ["uploads/pdfs/a.pdf"]
```
Fixtures build the source with `db.metadata.create_all` on a `sqlite:///` file and insert a row with `pdf_path="uploads\\pdfs\\old.pdf"`, `ocr_applied=1`.

- [ ] **Step 2: Run** `python -m pytest tests/test_migrate_to_cloud.py -v` — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python -m pytest -v` — expected: all pass.
- [ ] **Step 5: Commit** — `git add migrate_to_cloud.py tests/test_migrate_to_cloud.py && git commit -m "feat: script to move a school PC's data into Neon and R2"`

---

### Task 12: Deployment config, guide, and go-live

**Files:**
- Modify: `Procfile`, `app.py` (proxy handling near `_force_https`), `README.md`
- Create: `deploy/DEPLOY_TO_RENDER.md`

**Interfaces:**
- Produces:
  - `Procfile`: `web: gunicorn app:app --workers 1 --threads 8 --timeout 120`
  - `app.py`: when `LIBRARY_TRUST_PROXY=1`, wrap `app.wsgi_app` in `werkzeug.middleware.proxy_fix.ProxyFix(x_for=1, x_proto=1, x_host=1)` so login lockouts key on the real student IP rather than Render's proxy. Off by default (the LAN comment at `app.py:533-539` stays true for school-PC installs).
  - `deploy/DEPLOY_TO_RENDER.md`, written for a school IT person, in this order:
    1. **Neon:** create project in `aws-ap-southeast-1`; copy the pooled connection string.
    2. **R2:** create bucket `smartlib360-files` with location hint Asia-Pacific; create an API token with Object Read & Write on that bucket only; add CORS rule: allowed origin `https://library.msceducation.net`, methods `GET, HEAD`, headers `Range`, expose `Content-Length, Content-Range, Accept-Ranges` (EPUB reader and video seeking need it).
    3. **Microsoft Entra (school IT):** App registrations → New → single tenant → Web redirect URI `https://library.msceducation.net/auth/microsoft/callback` → create a client secret (note expiry date and set a calendar reminder) → copy tenant ID, client ID, secret. Default `User.Read` permission is enough.
    4. **Render:** Web Service from `kammaash/MSC-Library`, branch `main`, region Singapore, Starter or Free plan, build `pip install -r requirements.txt`, start from `Procfile`. Environment: `PYTHON_VERSION=3.12.7`, `DATABASE_URL`, `LIBRARY_SECRET_KEY` (generate with `python -c "import secrets; print(secrets.token_hex(32))"`), `LIBRARY_FORCE_HTTPS_COOKIES=1`, `LIBRARY_TRUST_PROXY=1`, `PUBLIC_BASE_URL=https://library.msceducation.net`, the four `R2_*`, the three `ENTRA_*`.
    5. **(Only if the library PC has real data)** run `migrate_to_cloud.py` from a computer holding the PC's `database.db` and `uploads/`, with the same env vars, **before** step 7.
    6. **Domain:** in the msceducation.net DNS, change `library` CNAME from `kammaash.github.io` to the Render `*.onrender.com` host, **DNS only** (no Cloudflare proxy); add the custom domain in Render and wait for its certificate. Then turn off GitHub Pages for the repo (Settings → Pages → Unpublish, or `gh api -X DELETE repos/kammaash/MSC-Library/pages`).
    7. **First run:** open `https://library.msceducation.net/setup` and create the admin account (skip if data was migrated).
    8. **Recovery:** Neon's restore window is the first line; admin → Backup produces a JSON export to R2; download one monthly and keep it off-cloud.
  - `README.md` Deployment section links the guide and lists the env vars in one table.

- [ ] **Step 1: Implement** Procfile, proxy handling, guide, README.
- [ ] **Step 2: Run** `python -m pytest -v` — expected: all pass; `python app.py` still starts in offline mode with the usual console banner.
- [ ] **Step 3: Staging smoke test** on a Render preview/branch deploy with a Neon branch and a separate R2 bucket, following the guide. Check each, in order: `/setup` creates admin → add a book (barcode image shows) → add a member with email (QR shows) → upload a PDF and an MP4 → open both as a student via PIN login → Microsoft sign-in with a school test account → Need more time on the video (set `LIBRARY_LINK_TTL_SECONDS=120` temporarily) → Backup button produces a `.json` listed for download → redeploy the service and confirm the uploaded files still open.
- [ ] **Step 4: Commit** — `git commit -am "chore: Render deployment config and step-by-step guide"`
- [ ] **Step 5: Go-live (needs the user's confirmation — outward-facing):** merge `cloud-deployment` into `main`, carry out guide steps 6–7 against production, and confirm `https://library.msceducation.net` shows the login page with a valid certificate.
