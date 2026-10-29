"""
/healthz, for an uptime service that keeps a Render Free instance awake.
It must answer without touching the database, or the pings would keep
Neon's Free-plan compute running around the clock.
"""
import pytest

import security
from conftest import make_admin


@pytest.fixture
def no_database(app_module, monkeypatch):
    """Counts every SQL statement the app runs, and fails any connection
    security.py would open for itself."""
    from sqlalchemy import event
    statements = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    with app_module.app.app_context():
        engine = app_module.db.engine
    event.listen(engine, "before_cursor_execute", count)

    def refuse(path):
        raise AssertionError("security.py opened a database connection")

    monkeypatch.setattr(security, "_get_connection", refuse)
    yield statements
    event.remove(engine, "before_cursor_execute", count)


def _ping(app_module, method="GET"):
    # A fresh client each time: an uptime service keeps no cookies.
    return app_module.app.test_client().open("/healthz", method=method)


def test_healthz_answers_ok_without_the_database(app_module, no_database):
    r = _ping(app_module)
    assert r.status_code == 200
    assert r.get_data(as_text=True) == "ok"
    assert r.mimetype == "text/plain"
    assert no_database == []


def test_healthz_head_request(app_module, no_database):
    r = _ping(app_module, "HEAD")
    assert r.status_code == 200
    assert no_database == []


def _setup_not_done(app_module, monkeypatch):
    """A brand-new database: the setup check would redirect to /setup."""
    calls = []
    monkeypatch.setattr(app_module, "_SETUP_COMPLETE_CACHE", False)
    monkeypatch.setattr(security, "setup_is_complete", lambda path: calls.append(path) or False)
    return calls


def test_healthz_works_before_setup(app_module, monkeypatch, no_database):
    # Every other page redirects to /setup, but the pinger must still get
    # its 200, and must not trigger the setup check's database query.
    checks = _setup_not_done(app_module, monkeypatch)
    r = _ping(app_module)
    assert r.status_code == 200
    assert checks == []
    assert no_database == []


def test_healthz_after_setup_still_needs_no_database(app_module, monkeypatch, no_database):
    # Also before the process has cached "setup is complete", as right after
    # Render wakes the instance up.
    make_admin()
    monkeypatch.setattr(app_module, "_SETUP_COMPLETE_CACHE", False)
    no_database.clear()
    for _ in range(3):
        assert _ping(app_module).status_code == 200
    assert no_database == []


def test_other_pages_still_go_to_setup(app_module, client, monkeypatch):
    # The exemption is only for /healthz.
    _setup_not_done(app_module, monkeypatch)
    for path in ("/login", "/healthzx", "/"):
        r = client.get(path)
        assert r.status_code == 302, path
        assert "/setup" in r.headers["Location"]
