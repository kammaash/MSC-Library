"""
security.py — Smart-Lib 360° hardening helpers
---------------------------------------
Added to address three gaps flagged in a security/reliability review:

  1. Hardcoded default passwords (admin123 / lib123 / view123) shipped in
     every install -> replaced with a first-run Setup Wizard, plus a
     "must change password" flag any admin can set on an account.
  2. No login throttling -> a simple in-memory rate limiter that locks out
     a username+IP combination after repeated failed attempts.
  3. Manual-only backups -> see backup_scheduler.py for the automatic
     daily backup job; this file just gives it a place to prune from.

Design choice: everything here is stored in a small hand-managed set of
tables (``app_security`` etc.) managed directly by this module, NOT
through the SQLAlchemy ``models.py`` file. That file wasn't included in
the project export this was built from, so rather than guess at its
exact structure (and risk breaking the real one when it's put back),
every new piece of state needed for these features lives in its own
table that this module creates and manages itself.

Database portability: every function here still takes a ``db_path``
argument, unchanged from the original SQLite-only design, so nothing
calling into this module (see app.py) needed to change. But when the
app is deployed against Postgres (Render + Neon), DATABASE_URL is set
in the environment, and this module detects that and transparently
opens a psycopg2 connection instead, ignoring db_path — it's the same
database the rest of the app uses via SQLAlchemy, not a separate
SQLite file that would be silently wiped on every Render restart.
Locally (DATABASE_URL unset), behavior is 100% unchanged: it still
uses db_path with sqlite3, exactly as before.
"""
import os
import sqlite3
import time
import threading

# psycopg2 is only imported lazily, inside _get_connection(), and only when
# DATABASE_URL is actually set — so a plain local SQLite run never needs it
# installed at all, matching how app.py's own DB config works.
_DATABASE_URL = os.environ.get("DATABASE_URL")


def _get_connection(db_path):
    """
    Returns a DB-API connection to whichever database the app is actually
    using: psycopg2/Postgres in production (DATABASE_URL set), or the same
    sqlite3/db_path as always for local development.
    """
    if _DATABASE_URL:
        import psycopg2
        return psycopg2.connect(_DATABASE_URL)
    return sqlite3.connect(db_path, timeout=15)


def _exec(conn, sql, params=()):
    """
    Runs one statement and returns a cursor-like object supporting
    .fetchone()/.fetchall(), on either connection type. sqlite3 lets you
    call .execute() straight on the connection; psycopg2 requires a
    cursor. This also rewrites "?" placeholders to psycopg2's "%s" style
    when needed, so every query below only has to be written once.
    """
    if _DATABASE_URL:
        cur = conn.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        return cur
    return conn.execute(sql, params)


# --------------------------------------------------------------------------
# Low-level table setup
# --------------------------------------------------------------------------
def ensure_security_tables(db_path):
    """
    Creates the two tables this module needs if they don't already exist.
    Safe to call on every startup (CREATE TABLE IF NOT EXISTS).
    """
    conn = _get_connection(db_path)
    try:
        _exec(conn, """
            CREATE TABLE IF NOT EXISTS app_security (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        _exec(conn, """
            CREATE TABLE IF NOT EXISTS user_security_flags (
                user_id INTEGER PRIMARY KEY,
                must_change_password INTEGER NOT NULL DEFAULT 0
            )
        """)
        _exec(conn, """
            CREATE TABLE IF NOT EXISTS login_rate_limits (
                rate_key TEXT PRIMARY KEY,
                fail_count INTEGER NOT NULL DEFAULT 0,
                window_start DOUBLE PRECISION NOT NULL DEFAULT 0,
                locked_until DOUBLE PRECISION
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _get_flag(db_path, key, default=None):
    conn = _get_connection(db_path)
    try:
        row = _exec(conn, "SELECT value FROM app_security WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default
    finally:
        conn.close()


def _set_flag(db_path, key, value):
    conn = _get_connection(db_path)
    try:
        _exec(
            conn,
            "INSERT INTO app_security (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# First-run Setup Wizard state
# --------------------------------------------------------------------------
def setup_is_complete(db_path):
    return _get_flag(db_path, "setup_complete", "0") == "1"


def mark_setup_complete(db_path, school_name="", school_address=""):
    _set_flag(db_path, "setup_complete", "1")
    if school_name:
        _set_flag(db_path, "school_name", school_name)
    if school_address:
        _set_flag(db_path, "school_address", school_address)


def get_school_name(db_path, fallback="Montessori Senior Secondary School"):
    return _get_flag(db_path, "school_name", fallback) or fallback


def get_school_address(db_path, fallback="A-camp, Kurnool"):
    return _get_flag(db_path, "school_address", fallback) or fallback


# --------------------------------------------------------------------------
# Forced "must change password" flag
# --------------------------------------------------------------------------
def set_must_change_password(db_path, user_id, flag=True):
    conn = _get_connection(db_path)
    try:
        _exec(
            conn,
            "INSERT INTO user_security_flags (user_id, must_change_password) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET must_change_password = excluded.must_change_password",
            (user_id, 1 if flag else 0),
        )
        conn.commit()
    finally:
        conn.close()


def must_change_password(db_path, user_id):
    conn = _get_connection(db_path)
    try:
        row = _exec(
            conn, "SELECT must_change_password FROM user_security_flags WHERE user_id = ?", (user_id,)
        ).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()


def clear_must_change_password(db_path, user_id):
    set_must_change_password(db_path, user_id, False)


# --------------------------------------------------------------------------
# Password strength check — shared by the setup wizard, forced change
# screen, and admin-side password resets so the rule is consistent
# everywhere instead of duplicated per-route.
# --------------------------------------------------------------------------
def password_is_strong_enough(pw):
    """
    Deliberately modest for a school-desk context (staff will be typing
    this on a shared keyboard, sometimes reading it off a sticky note
    for the first week) — but enough to rule out defaults like
    'admin123' style patterns and single-word passwords.
    Returns (ok: bool, message: str).
    """
    if not pw or len(pw) < 8:
        return False, "Password must be at least 8 characters long."
    has_letter = any(c.isalpha() for c in pw)
    has_digit = any(c.isdigit() for c in pw)
    if not (has_letter and has_digit):
        return False, "Password must contain both letters and numbers."
    if pw.lower() in ("password", "admin123", "password123", "librarian123", "changeme"):
        return False, "That password is too common — please choose another."
    return True, ""


# --------------------------------------------------------------------------
# Login rate limiting
# --------------------------------------------------------------------------
# Originally purely in-memory: a dict that reset to empty on every process
# restart. That's a real gap, not just a theoretical one — an attacker
# who can force/wait for a server restart (or who just tries right after
# a routine restart, e.g. the school PC being switched off overnight)
# gets a fresh 5 attempts, indefinitely. It's now backed by the
# `login_rate_limits` table in the app's actual database, so lockouts
# survive a restart the same way everything else in this app already
# does. The in-process lock is kept alongside it purely to avoid two
# near-simultaneous requests both reading "4 attempts" and both writing
# back "5" instead of one of them seeing the other's update — note this
# only coordinates threads within one process; see the module docstring
# for the multi-process caveat under gunicorn.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60  # 15 minutes

_lock = threading.Lock()


def _key(ip, username):
    return f"{ip}|{(username or '').strip().lower()}"


def is_locked_out(db_path, ip, username):
    key = _key(ip, username)
    now = time.time()
    with _lock:
        conn = _get_connection(db_path)
        try:
            row = _exec(
                conn,
                "SELECT fail_count, window_start, locked_until FROM login_rate_limits WHERE rate_key = ?",
                (key,),
            ).fetchone()
            if not row:
                return False, 0
            fail_count, window_start, locked_until = row
            if locked_until and now < locked_until:
                return True, int(locked_until - now)
            if locked_until and now >= locked_until:
                # Lock has expired — clear the record so the next failed
                # attempt starts a fresh window instead of instantly
                # re-locking on the old count.
                _exec(conn, "DELETE FROM login_rate_limits WHERE rate_key = ?", (key,))
                conn.commit()
            return False, 0
        finally:
            conn.close()


def record_failed_attempt(db_path, ip, username):
    key = _key(ip, username)
    now = time.time()
    with _lock:
        conn = _get_connection(db_path)
        try:
            row = _exec(
                conn, "SELECT fail_count, window_start FROM login_rate_limits WHERE rate_key = ?", (key,)
            ).fetchone()
            if row and (now - row[1]) < LOCKOUT_SECONDS:
                fail_count = row[0] + 1
                window_start = row[1]
            else:
                fail_count = 1
                window_start = now
            locked_until = (now + LOCKOUT_SECONDS) if fail_count >= MAX_ATTEMPTS else None
            _exec(
                conn,
                "INSERT INTO login_rate_limits (rate_key, fail_count, window_start, locked_until) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(rate_key) DO UPDATE SET "
                "fail_count = excluded.fail_count, window_start = excluded.window_start, "
                "locked_until = excluded.locked_until",
                (key, fail_count, window_start, locked_until),
            )
            conn.commit()
        finally:
            conn.close()


def _get_fail_count(db_path, ip, username):
    """
    Read-only lookup of the current fail_count for a key, purely so the
    login route can tell a student how many attempts they have left
    before a lock — doesn't record anything itself (record_failed_attempt
    already did that just before this is called).
    """
    key = _key(ip, username)
    conn = _get_connection(db_path)
    try:
        row = _exec(
            conn, "SELECT fail_count FROM login_rate_limits WHERE rate_key = ?", (key,)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def clear_attempts(db_path, ip, username):
    key = _key(ip, username)
    with _lock:
        conn = _get_connection(db_path)
        try:
            _exec(conn, "DELETE FROM login_rate_limits WHERE rate_key = ?", (key,))
            conn.commit()
        finally:
            conn.close()
