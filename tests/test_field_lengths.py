"""
Text longer than a VARCHAR column is refused with a readable 400 (audit item
I2). SQLite ignores declared lengths, so these tests check the app's own
validation; the same requests were run against Postgres (pgserver), where
without it they were 500s.
"""
import io

import pytest

from conftest import _persist, login_as, make_admin, make_student


@pytest.fixture
def admin(client):
    login_as(client, make_admin())
    return client


def _xlsx(rows):
    import pandas as pd
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    buf.seek(0)
    return buf


def test_helper_names_the_field_and_limit(app_module):
    from models import Member
    assert app_module._length_problem(Member, {"name": "x" * 150, "phone": None}) is None
    assert app_module._length_problem(Member, {"class_dept": "x" * 81}) == \
        "Class/Dept is too long (at most 80 characters)."
    assert app_module._length_problem(Member, {"not_a_column": "x" * 999}) is None
    # Numbers are measured as the text Postgres would store.
    assert "Phone" in app_module._length_problem(Member, {"phone": 10 ** 25})


@pytest.mark.parametrize("field,limit", [("name", 150), ("phone", 20), ("parent_phone", 20),
                                         ("type", 20), ("dob", 10), ("member_id", 30),
                                         ("admission_no", 50), ("class_dept", 80)])
def test_add_member_rejects_long_values(admin, field, limit):
    payload = {"name": "Asha", field: "x" * (limit + 1)}
    r = admin.post("/api/members", json=payload)
    assert r.status_code == 400
    assert f"at most {limit} characters" in r.get_json()["error"]


def test_add_member_at_the_limit_is_accepted(admin):
    r = admin.post("/api/members", json={"name": "x" * 150, "class_dept": "c" * 80})
    assert r.status_code == 201


def test_update_member_rejects_long_values_and_changes_nothing(admin, app_module):
    from models import Member
    member = make_student()
    r = admin.put(f"/api/members/{member.id}", json={"name": "New", "phone": "9" * 21})
    assert r.status_code == 400
    with app_module.app.app_context():
        assert app_module.db.session.get(Member, member.id).name == "Test Student"


@pytest.mark.parametrize("field,limit", [("title", 300), ("rack_no", 50), ("class_name", 50),
                                         ("accession_no", 50), ("isbn", 50)])
def test_add_book_rejects_long_values(admin, field, limit):
    r = admin.post("/api/books", json={"title": "T", field: "x" * (limit + 1)})
    assert r.status_code == 400 and f"at most {limit}" in r.get_json()["error"]


def test_update_book_rejects_long_values(admin):
    from models import Book
    book = _persist(Book(accession_no="B1", title="T", copies=1))
    assert admin.put(f"/api/books/{book.id}", json={"rack_no": "r" * 51}).status_code == 400
    assert admin.put(f"/api/books/{book.id}", json={"status": "s" * 21}).status_code == 400
    assert admin.put(f"/api/books/{book.id}", json={"rack_no": "R1"}).status_code == 200


def test_lost_book_rejects_long_accession(admin):
    r = admin.post("/api/lost-books", json={"accession_no": "a" * 51})
    assert r.status_code == 400


def test_add_online_book_checks_before_storing(admin, app_module):
    import os
    r = admin.post("/api/online-books", data={"title": "t" * 301, "file": (io.BytesIO(b"%PDF-1.4"), "a.pdf")},
                   content_type="multipart/form-data")
    assert r.status_code == 400 and "Title" in r.get_json()["error"]
    pdf_dir = os.path.join(app_module.BASE_DIR, "uploads", "pdfs")
    assert not os.path.isdir(pdf_dir) or not any(n.endswith("a.pdf") for n in os.listdir(pdf_dir))


def test_edit_online_book_rejects_long_category(admin):
    from models import OnlineBook
    book = _persist(OnlineBook(title="T", pdf_path="uploads/pdfs/x.pdf", file_type="pdf"))
    r = admin.put(f"/api/online-books/{book.id}", data={"title": "T", "category": "c" * 101},
                  content_type="multipart/form-data")
    assert r.status_code == 400


def test_users_post_and_put_reject_long_values(admin):
    r = admin.post("/api/users", json={"username": "u" * 81, "name": "N", "password": "pass-word-123"})
    assert r.status_code == 400 and "Username" in r.get_json()["error"]
    r = admin.post("/api/users", json={"username": "lib", "name": "N", "password": "pass-word-123",
                                       "phone": "9" * 21})
    assert r.status_code == 400
    from models import User
    user = _persist(User(username="lib2", name="L", role="librarian", password_hash="x"))
    assert admin.put(f"/api/users/{user.id}", json={"name": "n" * 151}).status_code == 400


def test_member_import_skips_and_counts_long_rows(admin, app_module):
    from models import Member
    data = _xlsx([{"Name": "Ok", "AdmissionNo": "R1"},
                  {"Name": "n" * 151, "AdmissionNo": "R2"},
                  {"Name": "Ok too", "AdmissionNo": "R3", "Mobile": "9" * 21}])
    r = admin.post("/api/import/members", data={"file": (data, "m.xlsx")}, content_type="multipart/form-data")
    j = r.get_json()
    assert j["created"] == 1 and j["skipped_too_long"] == 2
    assert j["first_error"] == "row 3: Name is too long (at most 150 characters)."
    with app_module.app.app_context():
        assert [m.admission_no for m in Member.query.all()] == ["R1"]


def test_book_import_skips_and_counts_long_rows(admin):
    data = _xlsx([{"Title": "Ok", "Accession No": "A1"},
                  {"Title": "Rack", "Accession No": "A2", "Rack No": "r" * 51}])
    r = admin.post("/api/import/books", data={"file": (data, "b.xlsx")}, content_type="multipart/form-data")
    j = r.get_json()
    assert j["created"] == 1 and j["skipped_too_long"] == 1
