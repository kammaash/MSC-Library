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
    assert r.get_json()["error"] == "The file was saved but could not be recorded. Please try again."


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
    key = "uploads/backups/" + name
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
        app_module.storage.delete("uploads/backups/" + name)


def test_backups_not_served_from_uploads_route_on_r2(client, r2):
    r2.client.put_object(Bucket=r2.bucket, Key="uploads/backups/b.json", Body=b"{}")
    login_as(client, make_admin())
    assert client.get("/uploads/backups/b.json").status_code == 404
    assert client.get("/api/backup/download/b.json").status_code == 302


def test_other_uploads_are_still_served(client, sample_book):
    login_as(client, make_admin())
    assert client.get("/uploads/videos/sample.mp4").status_code == 200


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
