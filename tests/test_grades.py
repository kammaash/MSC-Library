import io

import pytest
from conftest import make_admin, make_student, login_as, student_user, _persist
from models import OnlineBook


@pytest.mark.parametrize("raw,expected", [
    ("7-A", 7), ("VII", 7), ("VII-B", 7), ("Class 10", 10), ("Std XII", 12),
    ("12 Science", 12), ("LKG", None), ("", None), (None, None), ("13", None),
    ("0", None), ("IV-A", 4), ("viii", 8), ("Vision", None), ("XIII", None),
    ("7A", 7), ("100", None), ("Standard 5", 5),
])
def test_grade_from_class(app_module, raw, expected):
    assert app_module._grade_from_class(raw) == expected


def _books():
    for title, grade in (("G7", 7), ("G8", 8), ("Any", None)):
        _persist(OnlineBook(title=title, pdf_path=f"uploads/pdfs/{title}.pdf", file_type="pdf",
                            grade_target=grade))


def _titles(client, query=""):
    return {b["title"] for b in client.get("/api/online-books" + query).get_json()}


def test_student_sees_own_class_and_untargeted(client):
    _books()
    login_as(client, student_user(make_student(class_dept="7-A")))
    assert _titles(client) == {"G7", "Any"}
    assert len(client.get("/api/online-books?all_grades=1").get_json()) == 3


def test_student_with_unparseable_class_sees_everything(client):
    _books()
    login_as(client, student_user(make_student(class_dept="LKG")))
    assert _titles(client) == {"G7", "G8", "Any"}


def test_staff_are_never_filtered(client):
    _books()
    login_as(client, make_admin())
    assert _titles(client) == {"G7", "G8", "Any"}


def test_to_dict_exposes_grade_target(client):
    _books()
    login_as(client, make_admin())
    rows = {b["title"]: b["grade_target"] for b in client.get("/api/online-books").get_json()}
    assert rows == {"G7": 7, "G8": 8, "Any": None}


def _post(client, grade=None):
    data = {"file": (io.BytesIO(b"%PDF-1.4 test"), "a.pdf")}
    if grade is not None:
        data["grade_target"] = grade
    return client.post("/api/online-books", data=data, content_type="multipart/form-data")


@pytest.mark.parametrize("bad", ["14", "0", "-1", "abc", "7.5"])
def test_bad_grade_rejected(client, bad):
    login_as(client, make_admin())
    r = _post(client, bad)
    assert r.status_code == 400
    assert r.get_json()["error"] == "Class must be between 1 and 12."


def test_add_with_grade_and_blank_grade(client):
    login_as(client, make_admin())
    assert _post(client, "8").get_json()["grade_target"] == 8
    assert _post(client, "").get_json()["grade_target"] is None
    assert _post(client).get_json()["grade_target"] is None


def test_edit_grade_set_clear_and_leave_unchanged(client):
    login_as(client, make_admin())
    book_id = _post(client, "8").get_json()["id"]
    url = f"/api/online-books/{book_id}"
    assert client.put(url, data={"title": "T"}).get_json()["grade_target"] == 8      # field absent
    assert client.put(url, data={"title": "T", "grade_target": "9"}).get_json()["grade_target"] == 9
    assert client.put(url, data={"title": "T", "grade_target": "13"}).status_code == 400
    assert client.put(url, data={"title": "T", "grade_target": ""}).get_json()["grade_target"] is None


def test_bulk_import_applies_grade_and_rejects_bad(client, app_module):
    login_as(client, make_admin())
    def files():
        return {"files": [(io.BytesIO(b"%PDF-1.4 one"), "one.pdf"), (io.BytesIO(b"%PDF-1.4 two"), "two.pdf")]}
    r = client.post("/api/online-books/bulk-import", data={**files(), "grade_target": "6"},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["created"] == 2
    with app_module.app.app_context():
        assert {b.grade_target for b in OnlineBook.query.all()} == {6}
    r = client.post("/api/online-books/bulk-import", data={**files(), "grade_target": "99"},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["error"] == "Class must be between 1 and 12."
