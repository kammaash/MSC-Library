"""Regression tests for the audit fixes (access control, orphaned files,
importer savepoints, barcode names, sign-in hardening, ...)."""
import io

import pytest

from conftest import _persist, make_admin, make_student, login_as, student_user


@pytest.fixture(autouse=True)
def clean_local_uploads(app_module):
    """Local-mode uploads land in the session-wide data dir; other test
    modules assert those folders are empty, so leave them that way."""
    import os
    import shutil
    yield
    for sub in ("pdfs", "barcodes", "qrcodes"):
        shutil.rmtree(os.path.join(app_module.BASE_DIR, "uploads", sub), ignore_errors=True)


def _make_user(role, username):
    from models import User
    from werkzeug.security import generate_password_hash

    return _persist(User(username=username, name=username, role=role,
                         password_hash=generate_password_hash("pw-123456")))


# --------------------------------------------------------------------------
# A. Access control
# --------------------------------------------------------------------------
STAFF_ONLY_URLS = ["/api/members", "/api/issues", "/api/fines",
                   "/api/export/members", "/api/reports/member-list",
                   "/api/reports/issue-register", "/api/reports/overdue",
                   "/api/reports/fine-register", "/api/reports/stock-report",
                   "/api/lost-books", "/api/periodicals", "/api/visitors",
                   "/api/donations"]


@pytest.mark.parametrize("url", STAFF_ONLY_URLS)
def test_student_is_forbidden_from_staff_listings(client, url):
    login_as(client, student_user(make_student()))
    assert client.get(url).status_code == 403


def test_viewer_can_read_members(client):
    login_as(client, _make_user("viewer", "viewer1"))
    assert client.get("/api/members").status_code == 200
    assert client.get("/api/lost-books").status_code == 200


def test_admin_still_reads_staff_listings(client):
    make_student()
    login_as(client, make_admin())
    for url in STAFF_ONLY_URLS:
        assert client.get(url).status_code == 200, url


# --------------------------------------------------------------------------
# B. Orphaned files when the database write fails
# --------------------------------------------------------------------------
def _fail_commit_once(monkeypatch, app_module):
    real = app_module.db.session.commit
    state = {"raised": False}

    def flaky():
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("simulated database failure")
        return real()

    monkeypatch.setattr(app_module.db.session, "commit", flaky)


def _pdf(name):
    return (io.BytesIO(b"%PDF-1.4 tiny " + name.encode()), name)


def test_add_online_book_db_failure_removes_stored_files(client, r2, app_module, monkeypatch):
    login_as(client, make_admin())
    _fail_commit_once(monkeypatch, app_module)
    resp = client.post("/api/online-books", data={"title": "T", "file": _pdf("a.pdf")},
                       content_type="multipart/form-data")
    assert resp.status_code == 500
    assert resp.get_json()["error"] == "Could not save the file. Please try again."
    assert r2.list_keys("uploads/") == []


def test_bulk_import_commit_failure_removes_all_stored_files(client, r2, app_module, monkeypatch):
    login_as(client, make_admin())
    _fail_commit_once(monkeypatch, app_module)
    resp = client.post("/api/online-books/bulk-import",
                       data={"files": [_pdf("a.pdf"), _pdf("b.pdf")]},
                       content_type="multipart/form-data")
    assert resp.status_code == 500
    assert r2.list_keys("uploads/") == []


def test_bulk_import_one_failing_file_does_not_poison_the_others(client, r2, app_module, monkeypatch):
    from models import OnlineBook
    login_as(client, make_admin())
    real = app_module._generate_qr_for_online_book
    calls = {"n": 0}

    def flaky_qr(book):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("qr failed")
        return real(book)

    monkeypatch.setattr(app_module, "_generate_qr_for_online_book", flaky_qr)
    resp = client.post("/api/online-books/bulk-import",
                       data={"files": [_pdf("first.pdf"), _pdf("second.pdf")]},
                       content_type="multipart/form-data")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["created"] == 1 and len(body["skipped"]) == 1
    with app_module.app.app_context():
        books = OnlineBook.query.all()
        assert len(books) == 1
        kept = {books[0].pdf_path, books[0].qr_path}
    # Only the first item's objects remain; the second file's are gone.
    assert set(r2.list_keys("uploads/")) == kept


# --------------------------------------------------------------------------
# C. Excel importers keep earlier rows when a later row fails
# --------------------------------------------------------------------------
def _xlsx(rows):
    import pandas as pd

    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    buf.seek(0)
    return buf


def test_member_import_failed_row_keeps_the_other_rows(client, app_module, monkeypatch):
    from models import Member
    login_as(client, make_admin())
    real = app_module.generate_qr_for_member
    calls = {"n": 0}

    def flaky(member):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("INSERT INTO members ... parameters: secret-dob")
        return real(member)

    monkeypatch.setattr(app_module, "generate_qr_for_member", flaky)
    data = _xlsx([{"Name": "One", "AdmissionNo": "R1"},
                  {"Name": "Two", "AdmissionNo": "R2"},
                  {"Name": "Three", "AdmissionNo": "R3"}])
    r = client.post("/api/import/members", data={"file": (data, "m.xlsx")},
                    content_type="multipart/form-data")
    j = r.get_json()
    assert r.status_code == 200
    assert j["created"] == 2 and j["skipped_error"] == 1
    assert j["first_error"] == "row 3: could not be saved"
    assert "secret-dob" not in r.get_data(as_text=True)
    with app_module.app.app_context():
        assert sorted(m.admission_no for m in Member.query.all()) == ["R1", "R3"]


def test_book_import_failed_row_keeps_the_other_rows(client, app_module, monkeypatch):
    from models import Book
    login_as(client, make_admin())
    real = app_module.generate_barcode_for_book
    calls = {"n": 0}

    def flaky(book):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("INSERT INTO books ... parameters: leaked")
        return real(book)

    monkeypatch.setattr(app_module, "generate_barcode_for_book", flaky)
    data = _xlsx([{"Title": "One", "Accession No": "X1"},
                  {"Title": "Two", "Accession No": "X2"},
                  {"Title": "Three", "Accession No": "X3"}])
    r = client.post("/api/import/books", data={"file": (data, "b.xlsx")},
                    content_type="multipart/form-data")
    j = r.get_json()
    assert r.status_code == 200
    assert j["created"] == 2 and j["skipped_error"] == 1
    assert j["first_error"] == "row 3: could not be saved"
    assert "leaked" not in r.get_data(as_text=True)
    with app_module.app.app_context():
        assert sorted(b.accession_no for b in Book.query.all()) == ["X1", "X3"]


# --------------------------------------------------------------------------
# D. Barcode files for look-alike accession numbers do not collide
# --------------------------------------------------------------------------
def test_look_alike_accession_numbers_get_distinct_barcode_files(client, app_module):
    import os
    from models import Book
    login_as(client, make_admin())
    for acc in ("B1", "B#1", "B:1"):
        r = client.post("/api/books", json={"accession_no": acc, "title": f"Book {acc}"})
        assert r.status_code == 201
    with app_module.app.app_context():
        paths = {b.accession_no: b.barcode_path for b in Book.query.all()}
    assert len(set(paths.values())) == 3
    assert paths["B1"] == "uploads/barcodes/B1.png"
    for p in paths.values():
        assert os.path.exists(os.path.join(app_module.BASE_DIR, *p.split("/")))


# --------------------------------------------------------------------------
# G. The setup flag is read from the database only until it is seen complete
# --------------------------------------------------------------------------
def test_setup_flag_is_cached_once_complete(client, app_module, monkeypatch):
    import security
    monkeypatch.setattr(app_module, "_SETUP_COMPLETE_CACHE", False)
    calls = []
    real = security.setup_is_complete
    monkeypatch.setattr(security, "setup_is_complete",
                        lambda path: calls.append(path) or real(path))
    client.get("/login")
    client.get("/login")
    client.get("/login")
    assert len(calls) == 1


def test_incomplete_setup_still_redirects_to_wizard(client, app_module, monkeypatch):
    import security
    monkeypatch.setattr(app_module, "_SETUP_COMPLETE_CACHE", False)
    monkeypatch.setattr(security, "setup_is_complete", lambda path: False)
    r = client.get("/login")
    assert r.status_code == 302 and "/setup" in r.location
    assert client.get("/api/whoami").status_code == 403
    assert app_module._SETUP_COMPLETE_CACHE is False


# --------------------------------------------------------------------------
# H. ProxyFix does not trust X-Forwarded-Host
# --------------------------------------------------------------------------
def test_forwarded_host_is_ignored_when_proxy_trusted(app_module):
    from flask import Flask, request

    tiny = Flask(__name__)
    tiny.add_url_rule("/host", "host", lambda: request.host)
    assert app_module._apply_proxy_fix(tiny, {"LIBRARY_TRUST_PROXY": "1"}) is True
    r = tiny.test_client().get("/host", headers={"X-Forwarded-Host": "evil.example",
                                                 "X-Forwarded-For": "203.0.113.7"})
    assert r.get_data(as_text=True) != "evil.example"


# --------------------------------------------------------------------------
# I. Bad input is a 400/404, not a 500
# --------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["bookmark", "favorite"])
def test_bookmark_and_favorite_of_missing_item_is_404(client, kind):
    login_as(client, student_user(make_student()))
    r = client.post(f"/api/online-books/99999/{kind}")
    assert r.status_code == 404 and "error" in r.get_json()


def test_bookmark_of_existing_item_still_works(client, sample_book):
    login_as(client, student_user(make_student()))
    r = client.post(f"/api/online-books/{sample_book.id}/bookmark")
    assert r.status_code == 200 and r.get_json()["bookmarked"] is True


def test_issue_accepts_a_numeric_member_id(client, sample_book):
    from models import Member
    login_as(client, make_admin())
    client.post("/api/books", json={"accession_no": "N1", "title": "T"})
    # An unknown numeric id is a clean 404 rather than a type error.
    r = client.post("/api/issues", json={"accession_no": "N1", "member_id": 12345})
    assert r.status_code == 404
    r = client.post("/api/issues", json={"accession_no": "N1"})
    assert r.status_code == 404


@pytest.mark.parametrize("bad", ["abc", -1, 10**9, 2.5, True, "1e3"])
def test_add_book_rejects_bad_copies(client, bad):
    login_as(client, make_admin())
    r = client.post("/api/books", json={"accession_no": "C1", "title": "T", "copies": bad})
    assert r.status_code == 400
    assert "whole number between 0 and 1000000" in r.get_json()["error"]


def test_update_book_rejects_bad_copies_and_accepts_good(client):
    login_as(client, make_admin())
    book = client.post("/api/books", json={"accession_no": "C2", "title": "T"}).get_json()
    assert client.put(f"/api/books/{book['id']}", json={"copies": "lots"}).status_code == 400
    r = client.put(f"/api/books/{book['id']}", json={"copies": "4"})
    assert r.status_code == 200 and r.get_json()["copies"] == 4


def test_lost_book_rejects_bad_cost(client):
    login_as(client, make_admin())
    for bad in ("free", -5, 2 * 10**9):
        r = client.post("/api/lost-books", json={"accession_no": "L1", "cost": bad})
        assert r.status_code == 400
        assert "whole number between 0 and 1000000" in r.get_json()["error"]
    assert client.post("/api/lost-books", json={"accession_no": "L1", "cost": 150}).status_code == 201


def test_json_backup_releases_the_db_session_before_uploading(client, r2, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_database_url", "postgresql://example/db")
    order = []
    real_remove = app_module.db.session.remove
    monkeypatch.setattr(app_module.db.session, "remove", lambda: order.append("remove") or real_remove())
    real_save = r2.save_bytes
    monkeypatch.setattr(r2, "save_bytes", lambda *a, **k: order.append("save") or real_save(*a, **k))
    login_as(client, make_admin())
    assert client.post("/api/backup").status_code == 200
    assert order.index("remove") < order.index("save")


# --------------------------------------------------------------------------
# J. Local-mode download names and non-ASCII upload names
# --------------------------------------------------------------------------
def test_download_name_drops_control_characters_and_quotes(app_module):
    assert app_module._download_name('Bad\r\nTitle "x"\\y', "docx") == "BadTitle xy.docx"
    assert app_module._download_name("తెలుగు", "pdf") == "తెలుగు.pdf"
    assert app_module._download_name("\r\n", "pdf") == "download.pdf"


def test_download_original_survives_a_newline_in_the_title(client, app_module):
    from models import OnlineBook
    import os
    key = "uploads/pdfs/crlf_test.docx"
    path = os.path.join(app_module.BASE_DIR, *key.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"docx bytes")
    book = _persist(OnlineBook(title="Line one\r\nLine two", pdf_path=key, file_type="docx"))
    login_as(client, make_admin())
    r = client.get(f"/api/online-books/{book.id}/download-original")
    assert r.status_code == 200
    assert "\r" not in r.headers["Content-Disposition"] and "\n" not in r.headers["Content-Disposition"]


def test_non_ascii_upload_name_keeps_extension_and_title(client, app_module):
    from models import OnlineBook
    login_as(client, make_admin())
    resp = client.post("/api/online-books",
                       data={"file": (io.BytesIO(b"%PDF-1.4 tiny"), "తెలుగు పుస్తకం.pdf")},
                       content_type="multipart/form-data")
    assert resp.status_code == 201
    with app_module.app.app_context():
        book = OnlineBook.query.one()
        assert book.pdf_path.endswith(".pdf") and book.pdf_path.endswith("_file.pdf")
        assert "తెలుగు" in book.title


# --------------------------------------------------------------------------
# K. SQLite backups include rows still in the write-ahead log
# --------------------------------------------------------------------------
def test_sqlite_backup_contains_recent_rows(client, app_module):
    import sqlite3
    make_student(admission_no="WAL1")
    login_as(client, make_admin())
    name = client.post("/api/backup").get_json()["file"]
    key = "backups/" + name
    path = app_module.storage.local_path(key)
    try:
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute("SELECT admission_no FROM members").fetchall()
        finally:
            conn.close()
        assert ("WAL1",) in rows
    finally:
        app_module.storage.delete(key)
