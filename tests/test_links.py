from conftest import make_admin, make_student, login_as, student_user
from models import ReadHistory, OnlineBook


def _progress(client, book_id, payload):
    return client.post(f"/api/online-books/{book_id}/progress", json=payload)


def test_link_requires_login_as_json(client):
    r = client.get("/api/online-books/1/link")
    assert r.status_code == 401 and r.get_json()["login_required"] is True


def test_progress_requires_login_as_json(client):
    r = _progress(client, 1, {"seconds": 5})
    assert r.status_code == 401 and r.get_json()["login_required"] is True


def test_local_link_has_no_expiry(client, sample_book):
    login_as(client, make_admin())
    j = client.get(f"/api/online-books/{sample_book.id}/link").get_json()
    assert j["url"] == f"/api/online-books/{sample_book.id}/view?src=app" and j["expires_in"] is None
    assert j["warn_at"] is None and j["file_type"] == "mp4" and j["progress_seconds"] == 0


def test_r2_link_expires_in_30_minutes_and_warns_at_25(client, r2, sample_book):
    login_as(client, make_admin())
    j = client.get(f"/api/online-books/{sample_book.id}/link").get_json()
    assert (j["expires_in"], j["warn_at"]) == (1800, 1500)
    assert "X-Amz-Expires=1800" in j["url"] and sample_book.pdf_path in j["url"]


def test_r2_link_prefers_preview_pdf(client, r2, app_module):
    from conftest import _persist
    login_as(client, make_admin())
    book = _persist(OnlineBook(title="Deck", pdf_path="uploads/pdfs/deck.pptx", file_type="pptx",
                               preview_pdf_path="uploads/pdfs/deck.pdf"))
    j = client.get(f"/api/online-books/{book.id}/link").get_json()
    assert "deck.pdf" in j["url"] and j["file_type"] == "pdf"


def test_unknown_book_is_404_json(client):
    login_as(client, make_admin())
    r = client.get("/api/online-books/999/link")
    assert r.status_code == 404 and r.get_json()["error"] == "Item not found."


def test_renew_does_not_bump_open_count(client, sample_book, app_module):
    s = make_student(); login_as(client, student_user(s))
    client.get(f"/api/online-books/{sample_book.id}/link")
    client.get(f"/api/online-books/{sample_book.id}/link?renew=1")
    with app_module.app.app_context():
        assert ReadHistory.query.one().open_count == 1


def test_view_with_src_app_does_not_log(client, sample_book, app_module):
    s = make_student(); login_as(client, student_user(s))
    client.get(f"/api/online-books/{sample_book.id}/view?src=app")
    with app_module.app.app_context():
        assert ReadHistory.query.count() == 0


def test_progress_saved_and_returned(client, sample_book):
    s = make_student(); login_as(client, student_user(s))
    client.get(f"/api/online-books/{sample_book.id}/link")
    assert _progress(client, sample_book.id, {"seconds": 95}).status_code == 200
    assert client.get(f"/api/online-books/{sample_book.id}/link?renew=1").get_json()["progress_seconds"] == 95


def test_progress_creates_history_row_if_missing(client, sample_book, app_module):
    s = make_student(); login_as(client, student_user(s))
    assert _progress(client, sample_book.id, {"seconds": 7}).status_code == 200
    with app_module.app.app_context():
        row = ReadHistory.query.one()
        assert (row.progress_seconds, row.open_count) == (7, 1)


def test_progress_rejects_staff_and_bad_values(client, sample_book, app_module):
    login_as(client, make_admin())
    assert _progress(client, sample_book.id, {"seconds": 5}).status_code == 403
    client2 = client.application.test_client()
    with client2.session_transaction() as sess:
        sess["_csrf_token"] = "test-csrf"
    client2.environ_base["HTTP_X_CSRF_TOKEN"] = "test-csrf"
    s = make_student(); login_as(client2, student_user(s))
    for bad in ({"seconds": -1}, {"seconds": "5"}, {"seconds": 1.5}, {"seconds": True}, {}, [1]):
        assert _progress(client2, sample_book.id, bad).status_code == 400, bad
    assert _progress(client2, 999, {"seconds": 5}).status_code == 404
    with app_module.app.app_context():
        assert ReadHistory.query.count() == 0
