import os

from conftest import make_admin, login_as


def test_member_qr_saved_and_served_locally(client):
    login_as(client, make_admin())
    m = client.post("/api/members", json={"name": "Asha", "class_dept": "7-A"}).get_json()
    assert m["qr_path"] == f"uploads/qrcodes/{m['member_id']}.png"
    r = client.get("/" + m["qr_path"])
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert r.data.startswith(b"\x89PNG")


def test_book_barcode_in_r2_redirects(client, r2):
    login_as(client, make_admin())
    b = client.post("/api/books", json={"title": "Atlas"}).get_json()
    assert r2.list_keys("uploads/barcodes/") == [b["barcode_path"]]
    r = client.get("/" + b["barcode_path"])
    assert r.status_code == 302 and "X-Amz-Expires=300" in r.location


def test_book_barcode_local_is_png_on_disk(client, app_module):
    login_as(client, make_admin())
    b = client.post("/api/books", json={"title": "Atlas"}).get_json()
    assert b["barcode_path"] == f"uploads/barcodes/{b['accession_no']}.png"
    with open(os.path.join(app_module.BASE_DIR, b["barcode_path"]), "rb") as fh:
        assert fh.read(4) == b"\x89PNG"


def test_barcode_filename_is_sanitised(client, r2, app_module):
    login_as(client, make_admin())
    b = client.post("/api/books", json={"title": "Odd", "accession_no": "AB/12"}).get_json()
    # The sanitised name gets a short hash of the real number so that look-alike
    # numbers ("AB/12" vs "AB_12") do not share one file.
    key = app_module._barcode_key("AB/12")
    assert key.startswith("uploads/barcodes/AB_12_") and "/" not in key[len("uploads/barcodes/"):]
    assert b["barcode_path"] == key
    assert r2.list_keys("uploads/barcodes/") == [key]


def test_barcode_skipped_when_accession_has_no_usable_name(client, r2):
    login_as(client, make_admin())
    resp = client.post("/api/books", json={"title": "Odd", "accession_no": "///"})
    assert resp.status_code in (200, 201)
    assert resp.get_json()["barcode_path"] is None
    assert r2.list_keys("uploads/barcodes/") == []


def test_online_book_qr_uses_public_base_url(app_module, monkeypatch, client):
    import qrcode
    monkeypatch.setattr(app_module, "PUBLIC_BASE_URL", "https://library.msceducation.net")
    payloads = []
    real_make = qrcode.make
    monkeypatch.setattr(qrcode, "make", lambda data, *a, **kw: payloads.append(data) or real_make(data, *a, **kw))

    class FakeBook:
        id = 7
        qr_path = None

    book = FakeBook()
    with app_module.app.app_context():
        app_module._generate_qr_for_online_book(book)
    assert payloads == ["https://library.msceducation.net/api/online-books/7/view"]
    assert book.qr_path == "uploads/qrcodes/online_7.png"


def test_online_book_qr_falls_back_to_lan_ip(app_module, monkeypatch):
    import qrcode
    monkeypatch.setattr(app_module, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(app_module, "get_lan_ip", lambda: "192.168.1.5")
    payloads = []
    real_make = qrcode.make
    monkeypatch.setattr(qrcode, "make", lambda data, *a, **kw: payloads.append(data) or real_make(data, *a, **kw))

    class FakeBook:
        id = 3
        qr_path = None

    with app_module.app.app_context():
        app_module._generate_qr_for_online_book(FakeBook())
    assert payloads == ["http://192.168.1.5:5000/api/online-books/3/view"]


def test_uploads_route_rejects_traversal(client):
    login_as(client, make_admin())
    assert client.get("/uploads/../instance_secret.key").status_code == 404
    assert client.get("/uploads/%2e%2e/instance_secret.key").status_code == 404
    assert client.get("/uploads/qrcodes/../../instance_secret.key").status_code == 404


def test_uploads_route_missing_file_404_and_requires_login(client):
    assert client.get("/uploads/qrcodes/nope.png").status_code in (302, 401)
    login_as(client, make_admin())
    assert client.get("/uploads/qrcodes/nope.png").status_code == 404


def test_purge_incomplete_books_removes_barcode_from_storage(client, app_module, r2):
    from models import db, Book
    login_as(client, make_admin())
    b = client.post("/api/books", json={"title": "Atlas"}).get_json()
    assert r2.list_keys("uploads/barcodes/") == [b["barcode_path"]]
    with app_module.app.app_context():
        monkey_book = Book.query.filter_by(accession_no=b["accession_no"]).first()
        monkey_book.title = ""
        db.session.commit()
    resp = client.delete("/api/books/purge-incomplete")
    assert resp.get_json()["removed"] == 1
    assert r2.list_keys("uploads/barcodes/") == []
