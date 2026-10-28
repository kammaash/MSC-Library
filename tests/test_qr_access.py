"""Who may fetch which QR code under /uploads/qrcodes/ (audit item M1)."""
import pytest

from conftest import login_as, make_admin, make_student, student_user


@pytest.fixture
def qr_members(app_module):
    """Two students with generated QR codes, and a digital item with one."""
    from models import Member, OnlineBook
    db = app_module.db
    make_student("A1")
    make_student("A2")
    with app_module.app.app_context():
        for member in Member.query.all():
            app_module.generate_qr_for_member(member)
        book = OnlineBook(title="Atlas", pdf_path="uploads/pdfs/atlas.pdf", file_type="pdf")
        db.session.add(book)
        db.session.flush()
        app_module._generate_qr_for_online_book(book)
        db.session.commit()
        mine = Member.query.filter_by(admission_no="A1").one()
        db.session.expunge(mine)
        other_qr = Member.query.filter_by(admission_no="A2").one().qr_path
        book_qr = book.qr_path
    assert mine.qr_path and other_qr and book_qr
    return mine, other_qr, book_qr


def test_student_gets_own_qr_and_item_qrs_only(client, qr_members):
    mine, other_qr, book_qr = qr_members
    login_as(client, student_user(mine))
    assert client.get("/" + mine.qr_path).status_code == 200
    assert client.get("/" + book_qr).status_code == 200
    assert client.get("/" + other_qr).status_code == 404


def test_student_cannot_dodge_the_check_with_case_or_dots(client, qr_members):
    mine, other_qr, _ = qr_members
    login_as(client, student_user(mine))
    assert client.get("/" + other_qr.replace("qrcodes", "QRCODES")).status_code == 404
    assert client.get("/" + other_qr.replace("qrcodes/", "x/../qrcodes/")).status_code == 404


@pytest.mark.parametrize("role", ["admin", "librarian", "viewer"])
def test_staff_and_viewers_get_every_qr(client, qr_members, role):
    from conftest import _persist
    from models import User
    _, other_qr, _ = qr_members
    login_as(client, _persist(User(username=role, name=role, role=role, password_hash="x")))
    assert client.get("/" + other_qr).status_code == 200


def test_student_still_gets_barcodes(client, app_module):
    import os
    path = os.path.join(app_module.BASE_DIR, "uploads", "barcodes", "B1.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"png")
    login_as(client, student_user(make_student()))
    assert client.get("/uploads/barcodes/B1.png").status_code == 200


def test_student_blocked_in_r2_mode_too(client, r2, qr_members):
    mine, other_qr, _ = qr_members
    login_as(client, student_user(mine))
    assert client.get("/" + other_qr).status_code == 404
