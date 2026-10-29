"""
Four small audit fixes:

  M2  deleting an account also deletes its user_security_flags row
  M3  the Excel import routes no longer echo the parser's exception text
  M4  Postgres REAL lockout columns are widened to DOUBLE PRECISION on upgrade
  M5  failure counting is one atomic upsert, so no process-wide lock is needed
"""
import io
import shutil
import tempfile
import threading
import time

import pytest
from sqlalchemy import text
from werkzeug.security import generate_password_hash

import security
from conftest import _persist, login_as, make_admin, make_student, student_user


def _flag_rows(app_module, user_id):
    """Raw rows for a user in user_security_flags (bypasses security.py's cache)."""
    with app_module.app.app_context():
        return app_module.db.session.execute(
            text("SELECT must_change_password FROM user_security_flags WHERE user_id = :u"),
            {"u": user_id}).fetchall()


def _rate_row(app_module, key):
    with app_module.app.app_context():
        return app_module.db.session.execute(
            text("SELECT fail_count, locked_until FROM login_rate_limits WHERE rate_key = :k"),
            {"k": key}).fetchone()


# --------------------------------------------------------------------------
# M2 - security flags go with the account
# --------------------------------------------------------------------------
def test_delete_user_removes_security_flag_row(app_module, client):
    from models import User
    admin = make_admin()
    other = _persist(User(username="lib", name="Lib", role="librarian",
                          password_hash=generate_password_hash("pass-word-123")))
    security.set_must_change_password(app_module.DB_PATH, other.id, True)
    assert _flag_rows(app_module, other.id)

    login_as(client, admin)
    assert client.delete(f"/api/users/{other.id}").status_code == 200
    assert _flag_rows(app_module, other.id) == []


def test_delete_member_removes_student_security_flag_row(app_module, client):
    admin = make_admin()
    member = make_student()
    user = student_user(member)
    security.set_must_change_password(app_module.DB_PATH, user.id, True)
    assert _flag_rows(app_module, user.id)

    login_as(client, admin)
    assert client.delete(f"/api/members/{member.id}").status_code == 200
    assert _flag_rows(app_module, user.id) == []
    # The cached flag went too, so a new account reusing the id starts clean.
    assert security.must_change_password(app_module.DB_PATH, user.id) is False


# --------------------------------------------------------------------------
# M3 - fixed message for unreadable workbooks
# --------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["/api/import/books", "/api/import/members"])
def test_unreadable_workbook_gets_fixed_message(app_module, client, url):
    login_as(client, make_admin())
    garbage = b"definitely-not-a-zip-\x00\x01 SECRET-PARSER-DETAIL"
    r = client.post(url, data={"file": (io.BytesIO(garbage), "bad.xlsx")},
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert r.get_json()["error"] == ("Could not read this file as an Excel workbook. "
                                     "Please save it as .xlsx and try again.")
    body = r.get_data(as_text=True)
    # None of the library's own wording ("File is not a zip file", the class
    # name, ...) may reach the browser.
    assert "zip" not in body.lower()
    assert "Error" not in body
    assert "Exception" not in body


# --------------------------------------------------------------------------
# M5 - atomic counting
# --------------------------------------------------------------------------
def _run_threads(target, count):
    barrier = threading.Barrier(count)
    errors = []

    def worker(n):
        try:
            barrier.wait()
            target(n)
        except Exception as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_concurrent_failures_for_one_account_stop_at_the_threshold(app_module):
    # 20 different addresses guessing at one student account: every pair and
    # address counter sees a single failure, and the account counter must
    # reach exactly the maximum (10) and then stay put, locked until a
    # librarian clears it (-1). A lost update would leave it below 10.
    _run_threads(lambda n: security.record_login_failure(
        app_module.DB_PATH, f"10.0.0.{n}", "student:A100", "A100"), 20)

    count, locked_until = _rate_row(app_module, security._account_key("A100"))
    assert count == security.ACCOUNT_MAX_FAILURES
    assert locked_until == security.UNTIL_CLEARED
    assert security.login_block(app_module.DB_PATH, "10.9.9.9", "student:A100", "A100") == ("account", None)
    for n in range(20):
        assert _rate_row(app_module, security._ip_key(f"10.0.0.{n}"))[0] == 1
        assert _rate_row(app_module, security._key(f"10.0.0.{n}", "student:A100"))[0] == 1


def test_concurrent_failures_from_one_address_all_count(app_module):
    # One address trying 20 different accounts stays under the address cap
    # (40) and every attempt must be counted: exactly 20, no lock.
    _run_threads(lambda n: security.record_login_failure(
        app_module.DB_PATH, "10.1.1.1", f"student:B{n}", f"B{n}"), 20)

    count, locked_until = _rate_row(app_module, security._ip_key("10.1.1.1"))
    assert count == 20
    assert locked_until is None


def test_count_failure_without_returning_support(app_module, monkeypatch):
    # SQLite older than 3.35 has no RETURNING; the fallback reads the row back.
    monkeypatch.setattr(security.sqlite3, "sqlite_version_info", (3, 34, 0))
    for _ in range(3):
        security.record_login_failure(app_module.DB_PATH, "10.2.2.2", "student:C1", "C1")
    assert _rate_row(app_module, security._account_key("C1"))[0] == 3
    assert _rate_row(app_module, security._key("10.2.2.2", "student:C1"))[0] == 3


# --------------------------------------------------------------------------
# M4 / M5 on a real Postgres (skipped without pgserver)
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def pg_uri():
    pgserver = pytest.importorskip("pgserver")
    pytest.importorskip("psycopg2")
    data_dir = tempfile.mkdtemp(prefix="smartlib-pg-")
    server = pgserver.get_server(data_dir, cleanup_mode="stop")
    try:
        yield server.get_uri()
    finally:
        server.cleanup()
        shutil.rmtree(data_dir, ignore_errors=True)


def _column_types(uri):
    import psycopg2
    conn = psycopg2.connect(uri)
    try:
        cur = conn.cursor()
        cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = 'login_rate_limits' ORDER BY column_name")
        return dict(cur.fetchall())
    finally:
        conn.close()


def test_real_lockout_columns_are_widened_on_postgres(pg_uri, monkeypatch):
    import psycopg2

    conn = psycopg2.connect(pg_uri)
    try:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS login_rate_limits")
        cur.execute("CREATE TABLE login_rate_limits (rate_key TEXT PRIMARY KEY, "
                    "fail_count INTEGER NOT NULL DEFAULT 0, "
                    "window_start REAL NOT NULL DEFAULT 0, locked_until REAL)")
        conn.commit()
    finally:
        conn.close()
    before = _column_types(pg_uri)
    assert before["window_start"] == "real" and before["locked_until"] == "real"

    # Point security.py at the scratch server for this test only, without
    # importing app (which would need DATABASE_URL set for the whole process).
    monkeypatch.setattr(security, "_DATABASE_URL", pg_uri)
    monkeypatch.setattr(security, "_engine", None)

    security.ensure_security_tables("unused")
    after = _column_types(pg_uri)
    assert after["window_start"] == "double precision"
    assert after["locked_until"] == "double precision"
    assert after["fail_count"] == "integer"

    # A second run finds nothing to change: no ALTER is issued.
    statements = []
    real_exec = security._exec

    def spy(conn, sql, params=()):
        statements.append(sql)
        return real_exec(conn, sql, params)

    monkeypatch.setattr(security, "_exec", spy)
    security.ensure_security_tables("unused")
    assert not [s for s in statements if "ALTER" in s]
    assert _column_types(pg_uri) == after
    monkeypatch.setattr(security, "_exec", real_exec)

    # The widened column keeps an epoch timestamp to the sub-second.
    now = 1761700000.125
    conn = security._get_connection("unused")
    try:
        security._count_failure(conn, "k", now, 5, 900, 900)
        conn.commit()
        assert security._exec(conn, "SELECT window_start FROM login_rate_limits "
                                    "WHERE rate_key = ?", ("k",)).fetchone()[0] == now
    finally:
        conn.close()


def test_atomic_counting_on_postgres(pg_uri, monkeypatch):
    monkeypatch.setattr(security, "_DATABASE_URL", pg_uri)
    monkeypatch.setattr(security, "_engine", None)
    security.ensure_security_tables("unused")
    conn = security._get_connection("unused")
    try:
        security._exec(conn, "DELETE FROM login_rate_limits")
        conn.commit()
    finally:
        conn.close()

    _run_threads(lambda n: security.record_login_failure(
        "unused", f"10.0.0.{n}", "student:A100", "A100"), 20)

    conn = security._get_connection("unused")
    try:
        count, locked_until = security._exec(
            conn, "SELECT fail_count, locked_until FROM login_rate_limits WHERE rate_key = ?",
            (security._account_key("A100"),)).fetchone()
    finally:
        conn.close()
    assert count == security.ACCOUNT_MAX_FAILURES
    assert locked_until == security.UNTIL_CLEARED
    assert security.login_block("unused", "10.9.9.9", "student:A100", "A100") == ("account", None)


# --------------------------------------------------------------------------
# A failure recorded while a timed lock is in force (requests sent in
# parallel can pass login_block before the lock lands) must not reset it.
# --------------------------------------------------------------------------
def test_failure_during_a_timed_lock_keeps_the_lock(app_module):
    import security
    path = app_module.DB_PATH
    for _ in range(security.MAX_ATTEMPTS):
        security.record_login_failure(path, "10.7.7.7", "staff:race", "staff:race", account_exists=False)
    blocked = security.login_block(path, "10.7.7.7", "staff:race", "staff:race")
    assert blocked and blocked[0] == "pair"
    # A straggler that slipped past the check:
    security.record_login_failure(path, "10.7.7.7", "staff:race", "staff:race", account_exists=False)
    assert security.login_block(path, "10.7.7.7", "staff:race", "staff:race") == blocked or \
        security.login_block(path, "10.7.7.7", "staff:race", "staff:race")[0] == "pair"
