"""
security.py borrows pooled connections and caches per-request lookups
(audit item I3): a normal signed-in page load opens no new database
connection once the pool is warm.
"""
import pytest

import security
from conftest import login_as, make_admin


@pytest.fixture
def connect_counter(app_module, monkeypatch):
    """Counts every new DB-API connection: SQLAlchemy's pool opening one,
    and security.py's own direct connect paths (used before use_engine)."""
    from sqlalchemy import event
    counts = {"n": 0}

    def bump(*_args, **_kwargs):
        counts["n"] += 1

    with app_module.app.app_context():
        engine = app_module.db.engine
    event.listen(engine, "connect", bump)
    real_sqlite = security.sqlite3.connect
    monkeypatch.setattr(security.sqlite3, "connect", lambda *a, **k: bump() or real_sqlite(*a, **k))
    yield counts
    event.remove(engine, "connect", bump)


def test_security_uses_the_app_engine(app_module):
    with app_module.app.app_context():
        assert security._engine is app_module.db.engine


def test_signed_in_page_load_opens_no_new_connection(client, connect_counter):
    login_as(client, make_admin())
    assert client.get("/").status_code == 200   # warms the pool and the caches
    before = connect_counter["n"]
    for path in ("/", "/api/whoami", "/api/dashboard"):
        assert client.get(path).status_code == 200
    assert connect_counter["n"] == before


def test_cached_page_needs_no_security_queries(client, monkeypatch):
    login_as(client, make_admin())
    client.get("/")
    calls = []
    real = security._get_connection
    monkeypatch.setattr(security, "_get_connection", lambda path: calls.append(path) or real(path))
    client.get("/")
    assert calls == []


def test_branding_change_shows_at_once(client):
    login_as(client, make_admin())
    assert b"Riverside" not in client.get("/").data
    r = client.post("/api/branding", data={"school_name": "Riverside School", "school_address": "Hill Road"})
    assert r.status_code == 200
    page = client.get("/").data
    assert b"Riverside School" in page and b"Hill Road" in page


def test_must_change_flag_cache_follows_writes(app_module):
    admin = make_admin()
    path = app_module.DB_PATH
    assert security.must_change_password(path, admin.id) is False
    security.set_must_change_password(path, admin.id, True)
    assert security.must_change_password(path, admin.id) is True
    security.clear_must_change_password(path, admin.id)
    assert security.must_change_password(path, admin.id) is False


def test_forget_user_removes_row_and_cache(app_module):
    from sqlalchemy import text
    admin = make_admin()
    path = app_module.DB_PATH
    security.set_must_change_password(path, admin.id, True)
    assert security.must_change_password(path, admin.id) is True
    security.forget_user(path, admin.id)
    assert security.must_change_password(path, admin.id) is False
    with app_module.app.app_context():
        rows = app_module.db.session.execute(
            text("SELECT COUNT(*) FROM user_security_flags WHERE user_id = :u"), {"u": admin.id}).scalar()
    assert rows == 0


def test_forced_password_change_still_enforced(client, app_module):
    admin = make_admin()
    security.set_must_change_password(app_module.DB_PATH, admin.id, True)
    login_as(client, admin)
    r = client.get("/api/whoami")
    assert r.status_code == 200  # exempt path
    assert client.get("/api/dashboard").status_code == 403
