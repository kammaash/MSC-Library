import io
import os
import tempfile

import pytest

from conftest import _persist, make_admin, make_student, login_as


def _upload(client, name="VII_SCIENCE.pdf", data=b"%PDF-1.4 tiny"):
    return client.post("/api/online-books", data={"title": "Sci", "file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def _pdf_path(app_module, book_id):
    from models import OnlineBook
    with app_module.app.app_context():
        return OnlineBook.query.get(book_id).pdf_path


def _book_count(app_module):
    from models import OnlineBook
    with app_module.app.app_context():
        return OnlineBook.query.count()


@pytest.fixture
def temp_dirs(monkeypatch):
    """Records every directory tempfile.mkdtemp hands out, so a test can
    prove each one was removed again."""
    created = []
    real = tempfile.mkdtemp

    def recording(*args, **kwargs):
        path = real(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", recording)
    return created


def test_upload_to_r2_leaves_no_local_file(client, r2, app_module, temp_dirs):
    login_as(client, make_admin())
    book = _upload(client).get_json()
    assert len(r2.list_keys("uploads/pdfs/")) == 1
    pdf_dir = os.path.join(app_module.BASE_DIR, "uploads", "pdfs")
    assert not os.path.isdir(pdf_dir) or not os.listdir(pdf_dir)
    assert temp_dirs and not any(os.path.exists(d) for d in temp_dirs)
    assert _pdf_path(app_module, book["id"]).startswith("uploads/pdfs/")


def test_upload_locally_stores_file_under_uploads_pdfs(client, app_module, temp_dirs):
    login_as(client, make_admin())
    book = _upload(client).get_json()
    key = _pdf_path(app_module, book["id"])
    assert key.startswith("uploads/pdfs/") and "\\" not in key
    with open(os.path.join(app_module.BASE_DIR, key), "rb") as fh:
        assert fh.read() == b"%PDF-1.4 tiny"
    assert not any(os.path.exists(d) for d in temp_dirs)


def test_view_redirects_to_30_minute_link_in_r2(client, r2):
    login_as(client, make_admin())
    book = _upload(client).get_json()
    r = client.get(f"/api/online-books/{book['id']}/view")
    assert r.status_code == 302 and "X-Amz-Expires=1800" in r.location


def test_view_streams_locally(client):
    login_as(client, make_admin())
    book = _upload(client).get_json()
    r = client.get(f"/api/online-books/{book['id']}/view")
    assert r.status_code == 200 and r.data.startswith(b"%PDF")
    assert r.headers["Content-Disposition"] == "inline"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "no-store" in r.headers["Cache-Control"]


def test_office_file_without_preview_downloads_with_title_name(client):
    login_as(client, make_admin())
    book = _upload(client, name="notes.docx", data=b"not really a docx").get_json()
    r = client.get(f"/api/online-books/{book['id']}/view")
    assert r.status_code == 200
    assert "attachment" in r.headers["Content-Disposition"]
    assert f"Sci.docx" in r.headers["Content-Disposition"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_legacy_backslash_path_still_opens(client, app_module):
    from models import OnlineBook
    pdf_dir = os.path.join(app_module.BASE_DIR, "uploads", "pdfs")
    os.makedirs(pdf_dir, exist_ok=True)
    with open(os.path.join(pdf_dir, "old.pdf"), "wb") as fh:
        fh.write(b"%PDF-1.4 legacy")
    row = _persist(OnlineBook(title="Old", pdf_path="uploads\\pdfs\\old.pdf",
                              file_type="pdf", added_date="2020-01-01"))
    login_as(client, make_admin())
    r = client.get(f"/api/online-books/{row.id}/view")
    assert r.status_code == 200 and r.data.startswith(b"%PDF")


def test_legacy_backslash_path_redirects_in_r2(client, r2):
    from models import OnlineBook
    r2.save_bytes(b"%PDF-1.4 legacy", "uploads/pdfs/old.pdf", "application/pdf")
    row = _persist(OnlineBook(title="Old", pdf_path="uploads\\pdfs\\old.pdf",
                              file_type="pdf", added_date="2020-01-01"))
    login_as(client, make_admin())
    r = client.get(f"/api/online-books/{row.id}/view")
    assert r.status_code == 302 and "/uploads/pdfs/old.pdf" in r.location


def test_failed_r2_upload_creates_no_row(client, r2, app_module, monkeypatch, temp_dirs):
    login_as(client, make_admin())
    monkeypatch.setattr(r2, "save_file", lambda *a, **k: (_ for _ in ()).throw(OSError("network")))
    assert _upload(client).status_code == 500
    assert _book_count(app_module) == 0
    assert r2.list_keys("uploads/") == []
    assert temp_dirs and not any(os.path.exists(d) for d in temp_dirs)


def test_second_save_failure_deletes_first_saved_key(client, r2, app_module, monkeypatch):
    login_as(client, make_admin())
    # Pretend LibreOffice produced a preview, then fail on its upload.
    def fake_preview(source_path, ext, out_dir):
        path = os.path.join(out_dir, "preview.pdf")
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4 preview")
        return path

    monkeypatch.setattr(app_module, "_convert_to_pdf_preview", fake_preview)
    real_save = r2.save_file
    calls = []

    def flaky(src, key, content_type=None, move=False):
        calls.append(key)
        if len(calls) == 2:
            raise OSError("network")
        return real_save(src, key, content_type, move=move)

    monkeypatch.setattr(r2, "save_file", flaky)
    assert _upload(client, name="notes.docx", data=b"x").status_code == 500
    assert len(calls) == 2
    assert r2.list_keys("uploads/") == []
    assert _book_count(app_module) == 0


def test_preview_pdf_is_stored_and_served(client, r2, app_module, monkeypatch):
    login_as(client, make_admin())

    def fake_preview(source_path, ext, out_dir):
        path = os.path.join(out_dir, "preview.pdf")
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4 preview")
        return path

    monkeypatch.setattr(app_module, "_convert_to_pdf_preview", fake_preview)
    book = _upload(client, name="notes.docx", data=b"x").get_json()
    assert len(r2.list_keys("uploads/pdfs/")) == 2
    r = client.get(f"/api/online-books/{book['id']}/view")
    assert r.status_code == 302 and "uploads/pdfs/" in r.location and ".pdf" in r.location


def test_convert_to_pdf_preview_skips_non_office_and_missing_soffice(app_module, tmp_path):
    assert app_module._convert_to_pdf_preview(str(tmp_path / "a.pdf"), ".pdf", str(tmp_path)) is None
    if not app_module.shutil.which("soffice"):
        assert app_module._convert_to_pdf_preview(str(tmp_path / "a.docx"), ".docx", str(tmp_path)) is None


def test_oversize_upload_is_rejected_and_cleaned(client, r2, app_module, monkeypatch, temp_dirs):
    login_as(client, make_admin())
    monkeypatch.setitem(app_module.DIGITAL_LIBRARY_MAX_MB, "document", 0)
    r = _upload(client, data=b"%PDF-1.4 " + b"x" * 2048)
    assert r.status_code == 400
    assert r2.list_keys("uploads/") == []
    assert not any(os.path.exists(d) for d in temp_dirs)


def test_delete_removes_objects(client, r2):
    login_as(client, make_admin())
    book = _upload(client).get_json()
    assert book["qr_path"] and r2.list_keys("uploads/qrcodes/")
    client.delete(f"/api/online-books/{book['id']}")
    assert r2.list_keys("uploads/") == []


def _replace(client, book_id, data=b"%PDF-1.4 new", name="NEW.pdf"):
    return client.put(f"/api/online-books/{book_id}",
                      data={"title": "Sci", "file": (io.BytesIO(data), name)},
                      content_type="multipart/form-data")


def test_edit_with_replacement_deletes_old_objects_after_commit(client, r2, app_module):
    login_as(client, make_admin())
    book = _upload(client).get_json()
    old_key = _pdf_path(app_module, book["id"])
    assert _replace(client, book["id"]).status_code == 200
    new_key = _pdf_path(app_module, book["id"])
    assert new_key != old_key
    assert r2.list_keys("uploads/pdfs/") == [new_key]
    # The QR code is regenerated under the same key, so it must survive.
    assert r2.list_keys("uploads/qrcodes/") == [f"uploads/qrcodes/online_{book['id']}.png"]


def test_edit_failed_commit_keeps_old_objects_and_drops_new(client, r2, app_module, monkeypatch):
    login_as(client, make_admin())
    book = _upload(client).get_json()
    old_key = _pdf_path(app_module, book["id"])

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(app_module.db.session, "commit", boom, raising=False)
    # The unhandled error propagates through the test client (TESTING mode)
    # or becomes a 500, depending on config; either way the files matter.
    try:
        r = _replace(client, book["id"])
        assert r.status_code == 500
    except RuntimeError:
        pass
    monkeypatch.undo()
    assert _pdf_path(app_module, book["id"]) == old_key
    assert r2.list_keys("uploads/pdfs/") == [old_key]


def test_download_original_in_r2_redirects_as_attachment(client, r2):
    login_as(client, make_admin())
    book = _upload(client).get_json()
    r = client.get(f"/api/online-books/{book['id']}/download-original")
    assert r.status_code == 302
    assert "attachment" in r.location and "Sci.pdf" in r.location


def test_download_original_locally(client):
    login_as(client, make_admin())
    book = _upload(client).get_json()
    r = client.get(f"/api/online-books/{book['id']}/download-original")
    assert r.status_code == 200 and "Sci.pdf" in r.headers["Content-Disposition"]


def test_bulk_import_reports_storage_failure_and_keeps_others(client, r2, app_module, monkeypatch, temp_dirs):
    login_as(client, make_admin())
    real_save = r2.save_file

    def flaky(src, key, content_type=None, move=False):
        if "bad" in key:
            raise OSError("network")
        return real_save(src, key, content_type, move=move)

    monkeypatch.setattr(r2, "save_file", flaky)
    r = client.post("/api/online-books/bulk-import", content_type="multipart/form-data", data={
        "files": [(io.BytesIO(b"%PDF-1.4 a"), "good.pdf"), (io.BytesIO(b"%PDF-1.4 b"), "bad.pdf")]})
    body = r.get_json()
    assert r.status_code == 200 and body["created"] == 1
    assert len(body["skipped"]) == 1 and "bad.pdf" in body["skipped"][0]
    keys = r2.list_keys("uploads/pdfs/")
    assert len(keys) == 1 and "good" in keys[0]
    assert _book_count(app_module) == 1
    assert not any(os.path.exists(d) for d in temp_dirs)


def _student_user():
    from models import User
    from werkzeug.security import generate_password_hash
    member = make_student()
    return _persist(User(username="stu", name="Stu", role="student", member_id=member.id,
                         password_hash=generate_password_hash("pw-12345678")))


def _history_count(app_module):
    from models import ReadHistory
    with app_module.app.app_context():
        return ReadHistory.query.count()


def test_view_logs_read_history_for_student(client, app_module):
    admin_client = app_module.app.test_client()
    with admin_client.session_transaction() as s:
        s["_csrf_token"] = "test-csrf"
    admin_client.environ_base["HTTP_X_CSRF_TOKEN"] = "test-csrf"
    login_as(admin_client, make_admin())
    book = _upload(admin_client).get_json()

    login_as(client, _student_user())
    assert client.get(f"/api/online-books/{book['id']}/view").status_code == 200
    assert _history_count(app_module) == 1


def test_view_with_src_app_does_not_log_read_history(client, app_module):
    admin_client = app_module.app.test_client()
    with admin_client.session_transaction() as s:
        s["_csrf_token"] = "test-csrf"
    admin_client.environ_base["HTTP_X_CSRF_TOKEN"] = "test-csrf"
    login_as(admin_client, make_admin())
    book = _upload(admin_client).get_json()

    login_as(client, _student_user())
    assert client.get(f"/api/online-books/{book['id']}/view?src=app").status_code == 200
    assert _history_count(app_module) == 0


def test_link_ttl_defaults_to_1800(app_module):
    assert app_module.LINK_TTL_SECONDS == 1800


def test_local_upload_is_moved_into_place_not_copied(client, app_module, monkeypatch):
    """The working copy sits next to uploads/ and is renamed into place, so a
    large video is written once (audit item M10)."""
    login_as(client, make_admin())
    renames = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: renames.append((a, b)) or real_replace(a, b))
    resp = _upload(client)
    assert resp.status_code == 201
    stored = os.path.join(app_module.BASE_DIR, *_pdf_path(app_module, resp.get_json()["id"]).split("/"))
    assert [os.path.realpath(b) for _, b in renames] == [os.path.realpath(stored)]
    assert renames[0][0].startswith(app_module._UPLOAD_WORK_DIR)
    assert os.listdir(app_module._UPLOAD_WORK_DIR) == []
