"""
Tests for the final-review hardening wave: database URL handling, error
messages, backup access, deletes under enforced foreign keys, and so on.
"""
import pytest

import migrate_to_cloud


@pytest.mark.parametrize("raw, expected", [
    ("postgres://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
    ("postgresql://u:p@h/db?sslmode=require", "postgresql+psycopg2://u:p@h/db?sslmode=require"),
    ("postgresql+psycopg2://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
    ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ("sqlite:///x.db", "sqlite:///x.db"),
])
def test_sqlalchemy_database_uri(app_module, raw, expected):
    assert app_module._sqlalchemy_database_uri(raw) == expected
    assert migrate_to_cloud._sqlalchemy_database_uri(raw) == expected


def test_rate_limit_timestamps_are_double_precision(app_module):
    # On Postgres REAL is float4, which cannot hold a unix timestamp to better
    # than ~64 seconds. SQLite maps DOUBLE PRECISION to REAL affinity, so
    # existing SQLite databases behave exactly as before.
    import sqlite3
    conn = sqlite3.connect(app_module.DB_PATH)
    try:
        cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(login_rate_limits)")}
    finally:
        conn.close()
    assert cols["window_start"] == "DOUBLE PRECISION"
    assert cols["locked_until"] == "DOUBLE PRECISION"


# ---- generic error messages (no SQL, parameters or hostnames to the browser) ----
from conftest import make_admin, make_student, login_as, student_user  # noqa: E402

LEAKY = "SELECT secret FROM users WHERE host='db.neon.tech' password=hunter2"


def test_unhandled_api_error_returns_fixed_message(client, app_module, monkeypatch):
    def boom():
        raise RuntimeError(LEAKY)
    monkeypatch.setattr(app_module, "_current_student_member", boom)
    login_as(client, make_admin())
    r = client.post("/api/online-books/1/progress", json={"seconds": 1})
    assert r.status_code == 500
    assert r.get_json() == {"error": "Something went wrong. Please try again."}
    assert "neon" not in r.get_data(as_text=True) and "hunter2" not in r.get_data(as_text=True)


def test_add_online_book_errors_are_fixed_messages(client, app_module, monkeypatch):
    import io
    login_as(client, make_admin())

    def boom(*a, **k):
        raise RuntimeError(LEAKY)
    monkeypatch.setattr(app_module, "_save_digital_upload", boom)
    r = client.post("/api/online-books", data={"title": "T", "file": (io.BytesIO(b"x"), "a.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 500 and "hunter2" not in r.get_data(as_text=True)
    assert r.get_json()["error"] == "Could not save the file. Please try again."


def test_add_online_book_database_error_is_a_fixed_message(client, app_module, monkeypatch):
    import io
    from models import OnlineBook
    login_as(client, make_admin())
    monkeypatch.setattr(app_module, "_save_digital_upload",
                        lambda *a, **k: OnlineBook(title="T", pdf_path="uploads/pdfs/a.pdf", file_type="pdf"))

    def boom(book):
        raise RuntimeError(LEAKY)
    monkeypatch.setattr(app_module, "_generate_qr_for_online_book", boom)
    r = client.post("/api/online-books", data={"title": "T", "file": (io.BytesIO(b"x"), "a.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 500 and "hunter2" not in r.get_data(as_text=True)
    assert r.get_json()["error"] == "Could not save the file. Please try again."


def test_edit_online_book_save_error_is_a_fixed_message(client, app_module, sample_book, monkeypatch):
    import io
    login_as(client, make_admin())

    def boom(*a, **k):
        raise RuntimeError(LEAKY)
    monkeypatch.setattr(app_module, "_save_digital_upload", boom)
    r = client.put(f"/api/online-books/{sample_book.id}",
                   data={"title": "T", "file": (io.BytesIO(b"x"), "a.pdf")},
                   content_type="multipart/form-data")
    assert r.status_code == 500 and "hunter2" not in r.get_data(as_text=True)
    assert r.get_json()["error"] == "Could not save the file. Please try again."


@pytest.mark.parametrize("url", ["/api/import/books", "/api/import/members"])
def test_import_failure_is_a_fixed_message(client, app_module, monkeypatch, url):
    import io
    import pandas
    login_as(client, make_admin())

    def boom(*a, **k):
        raise RuntimeError(LEAKY)

    class Workbook:
        # Opens fine, then fails once the importer starts reading sheets.
        def __init__(self, *a, **k):
            pass

        @property
        def sheet_names(self):
            raise RuntimeError(LEAKY)
    monkeypatch.setattr(pandas, "ExcelFile", Workbook)
    r = client.post(url, data={"file": (io.BytesIO(b"x"), "a.xlsx")}, content_type="multipart/form-data")
    assert r.status_code == 500 and "hunter2" not in r.get_data(as_text=True)
    assert r.get_json()["error"] == "Import failed. Please check the file and try again."


# ---- backups are only reachable through the admin download route ----
def _put_backup(app_module, name="backup_2026_01_01_000000.json"):
    import os
    key = "backups/" + name
    path = app_module.storage.local_path(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b'{"tables": {}}')
    return name


def test_backups_are_not_served_from_uploads_route(client, app_module):
    name = _put_backup(app_module)
    try:
        student = student_user(make_student())
        login_as(client, student)
        assert client.get(f"/uploads/backups/{name}").status_code == 404
        login_as(client, make_admin())
        assert client.get(f"/uploads/backups/{name}").status_code == 404
        assert client.get("/uploads/backups/").status_code == 404
        # The admin-only route still works.
        r = client.get(f"/api/backup/download/{name}")
        assert r.status_code == 200 and r.data == b'{"tables": {}}'
    finally:
        app_module.storage.delete("backups/" + name)


def test_backups_not_served_from_uploads_route_on_r2(client, r2):
    r2.client.put_object(Bucket=r2.bucket, Key="uploads/backups/b.json", Body=b"{}")
    login_as(client, make_admin())
    assert client.get("/uploads/backups/b.json").status_code == 404
    assert client.get("/api/backup/download/b.json").status_code == 302


def test_digital_library_files_are_still_served(client, sample_book):
    login_as(client, make_admin())
    assert client.get("/" + sample_book.pdf_path).status_code == 200


# ---- /progress bounds ----
def test_progress_upper_bound(client, sample_book):
    login_as(client, student_user(make_student()))
    url = f"/api/online-books/{sample_book.id}/progress"
    assert client.post(url, json={"seconds": 86400}).status_code == 200
    r = client.post(url, json={"seconds": 86401})
    assert r.status_code == 400 and "seconds" in r.get_json()["error"]
    assert client.post(url, json={"seconds": 2**31}).status_code == 400


# ---- /link with a key the storage layer rejects ----
def test_link_with_unusable_key_is_a_json_404(client, r2, app_module):
    from conftest import _persist
    from models import OnlineBook
    login_as(client, make_admin())
    book = _persist(OnlineBook(title="Bad", pdf_path="uploads/../secret.pdf", file_type="pdf"))
    r = client.get(f"/api/online-books/{book.id}/link")
    assert r.status_code == 404 and r.get_json() == {"error": "Item not found."}


@pytest.mark.parametrize("path", ["Backups/x.json", "backups%5Cx.json", "qrcodes/../backups/x.json",
                                  "./backups/x.json", "BACKUPS"])
def test_backup_folder_variants_are_not_served(client, app_module, path):
    login_as(client, make_admin())
    assert client.get("/uploads/" + path).status_code == 404


# ---- deletes must satisfy foreign keys (Postgres enforces them) ----
def test_sqlite_foreign_keys_are_enforced_in_tests(app_module):
    from models import db
    from sqlalchemy import text
    with app_module.app.app_context():
        assert db.session.execute(text("PRAGMA foreign_keys")).scalar() == 1


def _engagement(member, book_id):
    """Bookmark, favourite, reading history, visit and PIN request for a student."""
    from conftest import _persist
    from models import (Bookmark, Favorite, ReadHistory, DigitalLibraryVisit,
                        PinResetRequest, Certificate)
    mid = member.member_id
    _persist(Bookmark(member_id=mid, online_book_id=book_id, added_date="2026-01-01"))
    _persist(Favorite(member_id=mid, online_book_id=book_id, added_date="2026-01-01"))
    _persist(ReadHistory(member_id=mid, online_book_id=book_id, open_count=1))
    _persist(DigitalLibraryVisit(member_id=mid, visit_date="2026-01-01"))
    _persist(PinResetRequest(member_id=mid, status="Pending"))
    _persist(Certificate(member_id=mid, title="Top Reader"))


def _count(app_module, model, **filters):
    with app_module.app.app_context():
        return model.query.filter_by(**filters).count()


def test_delete_online_book_with_student_activity(client, app_module, sample_book):
    from models import Bookmark, Favorite, ReadHistory, OnlineBook
    student = make_student()
    _engagement(student, sample_book.id)
    login_as(client, make_admin())
    r = client.delete(f"/api/online-books/{sample_book.id}")
    assert r.status_code == 200
    assert _count(app_module, OnlineBook) == 0
    for model in (Bookmark, Favorite, ReadHistory):
        assert _count(app_module, model, online_book_id=sample_book.id) == 0


def test_delete_student_who_has_logged_in_and_has_history(client, app_module, sample_book):
    from conftest import _persist
    from models import (User, Member, Issue, Book, Bookmark, Favorite, ReadHistory,
                        DigitalLibraryVisit, PinResetRequest, Certificate)
    student = make_student()
    other = make_student(admission_no="A200")
    student_user(student)
    other_user = student_user(other)
    _engagement(student, sample_book.id)
    _engagement(other, sample_book.id)
    book = _persist(Book(title="B", accession_no="ACC1", copies=1))
    _persist(Issue(accession_no=book.accession_no, member_id=student.member_id, issue_date="2026-01-01",
                   due_date="2026-01-15", return_date="2026-01-10", status="Returned"))
    login_as(client, make_admin())

    r = client.delete(f"/api/members/{student.id}")
    assert r.status_code == 200, r.get_data(as_text=True)

    assert _count(app_module, Member, id=student.id) == 0
    assert _count(app_module, User, member_id=student.id) == 0
    for model in (Bookmark, Favorite, ReadHistory, DigitalLibraryVisit, PinResetRequest):
        assert _count(app_module, model, member_id=student.member_id) == 0
    # Library history is kept, and another student's records are untouched.
    assert _count(app_module, Issue, member_id=student.member_id) == 1
    assert _count(app_module, Certificate, member_id=student.member_id) == 1
    assert _count(app_module, User, id=other_user.id) == 1
    for model in (Bookmark, Favorite, ReadHistory, DigitalLibraryVisit, PinResetRequest):
        assert _count(app_module, model, member_id=other.member_id) == 1


def test_delete_member_keeps_non_student_user_but_unlinks_it(client, app_module):
    from conftest import _persist
    from models import User
    member = make_student(admission_no="S1")
    staff_user = _persist(User(username="teacher", password_hash="x", name="T",
                               role="librarian", member_id=member.id))
    login_as(client, make_admin())
    assert client.delete(f"/api/members/{member.id}").status_code == 200
    with app_module.app.app_context():
        row = User.query.get(staff_user.id)
        assert row is not None and row.member_id is None


def test_delete_member_with_active_loan_is_still_refused(client, app_module):
    from conftest import _persist
    from models import Book, Issue, Member
    member = make_student()
    book = _persist(Book(title="B", accession_no="ACC1", copies=1))
    _persist(Issue(accession_no=book.accession_no, member_id=member.member_id, issue_date="2026-01-01",
                   due_date="2026-01-15", status="Active"))
    login_as(client, make_admin())
    r = client.delete(f"/api/members/{member.id}")
    assert r.status_code == 400
    assert _count(app_module, Member, id=member.id) == 1


def test_delete_member_removes_their_qr_file(client, app_module):
    import os
    member = make_student(admission_no="Q1")
    key = "uploads/qrcodes/q1.png"
    path = app_module.storage.local_path(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(b"png")
    from models import Member, db
    with app_module.app.app_context():
        db.session.get(Member, member.id).qr_path = key
        db.session.commit()
    login_as(client, make_admin())
    assert client.delete(f"/api/members/{member.id}").status_code == 200
    assert not os.path.exists(path)
