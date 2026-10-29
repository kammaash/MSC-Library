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

# psycopg2 is only imported lazily, inside _get_connection(), and only when
# DATABASE_URL is actually set — so a plain local SQLite run never needs it
# installed at all, matching how app.py's own DB config works.
_DATABASE_URL = os.environ.get("DATABASE_URL")


# Set by app.py (use_engine) to the SQLAlchemy engine the rest of the app
# uses. Connections are then borrowed from its pool instead of opened fresh:
# on Neon every new connection is a TLS handshake, and this module used to
# open one to three of them on every page. close() hands a borrowed
# connection back to the pool, which rolls back anything left uncommitted.
_engine = None


def use_engine(engine):
    global _engine
    _engine = engine


def _get_connection(db_path):
    """
    Returns a DB-API connection to whichever database the app is actually
    using: psycopg2/Postgres in production (DATABASE_URL set), or the same
    sqlite3/db_path as always for local development. Borrowed from the app's
    pool once use_engine() has been called.
    """
    if _engine is not None:
        return _engine.raw_connection()
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
        if _DATABASE_URL:
            # An earlier version of this table declared the two time columns
            # as REAL. On Postgres that is a 4-byte float, which cannot hold
            # a Unix timestamp to the second (about 1.76 billion needs more
            # than its 24 bits of precision), so lock expiry times would be
            # off by tens of seconds. CREATE TABLE IF NOT EXISTS leaves an
            # existing table alone, so widen the columns here. The query
            # only finds columns that are still `real`, which makes this a
            # no-op once they are double precision. It is deliberately not
            # wrapped in a try/except: this runs at startup, and a failed
            # ALTER should stop the deploy rather than leave imprecise locks.
            narrow = _exec(
                conn,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ? "
                "AND column_name IN ('window_start', 'locked_until') AND data_type = 'real'",
                ("login_rate_limits",),
            ).fetchall()
            for (column,) in narrow:
                # The names come from the fixed list in the query above, never
                # from input, so building the statement by formatting is safe.
                _exec(conn, f"ALTER TABLE login_rate_limits ALTER COLUMN {column} "
                            "TYPE DOUBLE PRECISION")
        conn.commit()
    finally:
        conn.close()


# In-process caches for values read on (nearly) every request: the
# app_security flags (school name and address, shown on every page) and each
# user's must_change_password flag (checked before every signed-in request).
# Every write goes through this module, which updates or drops the cached
# entry, so they stay correct as long as one process owns the database —
# which the deployment requires anyway (gunicorn --workers 1; a LAN install
# is one waitress process). Anything that changes these tables behind this
# module's back (tests, a manual SQL fix) must call clear_caches().
_MISSING = object()
_flag_cache = {}
_must_change_cache = {}


def clear_caches():
    _flag_cache.clear()
    _must_change_cache.clear()


def _get_flag(db_path, key, default=None):
    cached = _flag_cache.get(key, _MISSING)
    if cached is not _MISSING:
        return default if cached is None else cached
    conn = _get_connection(db_path)
    try:
        row = _exec(conn, "SELECT value FROM app_security WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    value = row[0] if row else None
    _flag_cache[key] = value
    return default if value is None else value


def _set_flag(db_path, key, value):
    _flag_cache.pop(key, None)
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
    _must_change_cache.pop(user_id, None)
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
    cached = _must_change_cache.get(user_id)
    if cached is not None:
        return cached
    conn = _get_connection(db_path)
    try:
        row = _exec(
            conn, "SELECT must_change_password FROM user_security_flags WHERE user_id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    _must_change_cache[user_id] = bool(row and row[0])
    return _must_change_cache[user_id]


def forget_user(db_path, user_id):
    """
    Removes a deleted account's row and cached flag. Without it the row
    outlives the account, and because a new account can be given the same
    id (SQLite reuses the highest id after a delete), the new account would
    inherit the old one's "must change password" state.
    """
    _must_change_cache.pop(user_id, None)
    conn = _get_connection(db_path)
    try:
        _exec(conn, "DELETE FROM user_security_flags WHERE user_id = ?", (user_id,))
        conn.commit()
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
# does. Counting is a single atomic INSERT ... ON CONFLICT DO UPDATE (see
# _count_failure), so two near-simultaneous requests cannot both read
# "4 attempts" and both write back "5": the database serialises them on the
# row, across threads and across processes alike. There used to be an
# in-process lock held around the whole read-then-write; on Neon that made
# every sign-in wait behind a network round trip, and it never helped with
# more than one process anyway.
#
# Three counters share that table, because a lock keyed only on
# (IP, username) has two holes on an internet-facing login page: from one
# address an attacker can try one date of birth against every admission
# number in the school and never trip a lock, and from many addresses one
# student can be attacked indefinitely. So every failed attempt counts
# against all three of:
#
#   - the (IP, username) pair: 5 failures in 15 minutes -> 15-minute lock.
#     This is what a student mistyping their own PIN normally runs into.
#   - the account itself, whatever address the attempts come from:
#     10 failures in an hour -> locked. A student's card then stays locked
#     until a librarian unlocks it from the member list (or issues a new
#     PIN set-up code); a staff account locks for an hour instead, because
#     a lock that waits for someone to lift it could leave the school with
#     no admin able to sign in at all. Only accounts that exist
#     are counted, so random usernames do not fill the table.
#   - the client address across all usernames: 40 failures in 15 minutes
#     -> 15-minute lock. Only failures count, never successful sign-ins,
#     and the number is deliberately generous: a whole computer lab
#     reaches a cloud deployment through one school NAT address, and a
#     class of young students mistyping dates of birth must not lock
#     everyone else out.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60  # 15 minutes

ACCOUNT_MAX_FAILURES = 10
ACCOUNT_WINDOW_SECONDS = 60 * 60
STAFF_ACCOUNT_LOCK_SECONDS = 60 * 60

IP_MAX_FAILURES = 40
IP_WINDOW_SECONDS = 15 * 60
IP_LOCK_SECONDS = 15 * 60

# Stored in locked_until for a lock that only a librarian can lift.
UNTIL_CLEARED = -1.0


def _key(ip, username):
    return f"{ip}|{(username or '').strip().lower()}"


def _account_key(account):
    return f"account|{(account or '').strip().lower()}"


def _ip_key(ip):
    return f"ip|{ip}"


def _lock_remaining(locked_until, now):
    """None when not locked, 0 for a lock that waits for a librarian,
    otherwise the seconds left."""
    if locked_until is None:
        return None
    if locked_until == UNTIL_CLEARED:
        return 0
    if now < locked_until:
        return max(1, int(locked_until - now))
    return None


def login_block(db_path, ip, pair_name, account):
    """
    Checks the three counters for a sign-in attempt, in one connection.
    `pair_name` names the (IP, username) counter (e.g. "student:A100" for a
    login, "pinreq:A100" for a PIN reset request); `account` names the
    account counter shared by every route that guesses at that account.

    Returns None when the attempt may go ahead, otherwise (reason, seconds)
    where reason is "pair", "account" or "ip" and seconds is how long the
    lock has left, or None for a lock that waits for a librarian.
    """
    now = time.time()
    checks = ((_key(ip, pair_name), "pair"), (_account_key(account), "account"), (_ip_key(ip), "ip"))
    conn = _get_connection(db_path)
    try:
        for key, reason in checks:
            row = _exec(conn, "SELECT locked_until FROM login_rate_limits WHERE rate_key = ?",
                        (key,)).fetchone()
            remaining = _lock_remaining(row[0], now) if row else None
            if remaining is not None:
                return reason, (remaining or None)
        return None
    finally:
        conn.close()


def _count_failure(conn, key, now, max_failures, window_seconds, lock_seconds):
    """Adds one failure to a counter and returns the new count. A lock that
    has expired, or a window that has run out, starts a fresh count.
    lock_seconds=None locks until clear_account_lock() is called.

    The whole read-modify-write is one statement, so concurrent failures for
    the same key cannot lose an update and no lock is needed around it. The
    CASE expressions below all read the row as it was before this statement:
      - a lock still in force (locked_until = -1, waiting for a librarian, or
        a timed lock not yet expired): row left untouched. login_block
        refuses those attempts first, but requests sent in parallel can pass
        it before the lock lands, and they must not reset the count;
      - no lock and still inside the window: count + 1, same window start,
        and a lock is set when the new count reaches the maximum;
      - anything else (window ran out, or a timed lock): back to a count of 1
        in a new window, which itself locks if the maximum is 1.
    Columns are table-qualified because Postgres rejects bare names in an
    upsert's SET clause as ambiguous with `excluded`."""
    t = "login_rate_limits"
    cleared = int(UNTIL_CLEARED)
    lock_value = UNTIL_CLEARED if lock_seconds is None else now + lock_seconds
    first_lock = lock_value if max_failures <= 1 else None
    counting = f"{t}.locked_until IS NULL AND ? - {t}.window_start < ?"
    held = f"({t}.locked_until = {cleared} OR {t}.locked_until > ?)"
    sql = (
        f"INSERT INTO {t} (rate_key, fail_count, window_start, locked_until) "
        "VALUES (?, 1, ?, ?) "
        "ON CONFLICT(rate_key) DO UPDATE SET "
        f"fail_count = CASE WHEN {held} THEN {t}.fail_count "
        f"WHEN {counting} THEN {t}.fail_count + 1 ELSE 1 END, "
        f"window_start = CASE WHEN {held} THEN {t}.window_start "
        f"WHEN {counting} THEN {t}.window_start ELSE ? END, "
        f"locked_until = CASE WHEN {held} THEN {t}.locked_until "
        f"WHEN {counting} THEN (CASE WHEN {t}.fail_count + 1 >= ? THEN ? ELSE NULL END) "
        "ELSE ? END"
    )
    params = (
        key, now, first_lock,            # the VALUES row, used when the key is new
        now, now, window_seconds,        # fail_count: lock in force? inside the window?
        now, now, window_seconds, now,   # window_start: same, else a new start
        now, now, window_seconds,        # locked_until: same
        max_failures, lock_value,        # ... reached the maximum -> lock
        first_lock,                      # new window of 1 failure
    )
    if sqlite3.sqlite_version_info >= (3, 35, 0):
        return _exec(conn, sql + " RETURNING fail_count", params).fetchone()[0]
    # SQLite older than 3.35 has no RETURNING. The upsert has just taken the
    # database's write lock for this transaction, so reading the row back
    # here sees exactly the value it wrote.
    _exec(conn, sql, params)
    return _exec(conn, f"SELECT fail_count FROM {t} WHERE rate_key = ?", (key,)).fetchone()[0]


def record_login_failure(db_path, ip, pair_name, account, account_exists=True,
                         account_lock_seconds=None):
    """
    Counts one failed attempt against all three counters (the account one
    only when the account exists) and returns how many attempts are left
    before the pair or the account locks, whichever comes first.
    account_lock_seconds=None means a student-style lock that waits for a
    librarian; staff accounts pass STAFF_ACCOUNT_LOCK_SECONDS.
    """
    now = time.time()
    conn = _get_connection(db_path)
    try:
        pair_count = _count_failure(conn, _key(ip, pair_name), now,
                                    MAX_ATTEMPTS, LOCKOUT_SECONDS, LOCKOUT_SECONDS)
        remaining = MAX_ATTEMPTS - pair_count
        if account_exists:
            account_count = _count_failure(conn, _account_key(account), now, ACCOUNT_MAX_FAILURES,
                                           ACCOUNT_WINDOW_SECONDS, account_lock_seconds)
            remaining = min(remaining, ACCOUNT_MAX_FAILURES - account_count)
        _count_failure(conn, _ip_key(ip), now, IP_MAX_FAILURES, IP_WINDOW_SECONDS, IP_LOCK_SECONDS)
        conn.commit()
        return max(0, remaining)
    finally:
        conn.close()


def record_login_success(db_path, ip, pair_name, account):
    """Resets the pair and account counters after a successful sign-in. The
    per-address counter is left alone: otherwise an attacker could reset it
    by signing in to their own account between guesses."""
    conn = _get_connection(db_path)
    try:
        _exec(conn, "DELETE FROM login_rate_limits WHERE rate_key IN (?, ?)",
              (_key(ip, pair_name), _account_key(account)))
        conn.commit()
    finally:
        conn.close()


def clear_account_lock(db_path, account):
    """What the librarian's Unlock button does: forgets the account's
    failures, including a lock that would otherwise never lift."""
    conn = _get_connection(db_path)
    try:
        _exec(conn, "DELETE FROM login_rate_limits WHERE rate_key = ?", (_account_key(account),))
        conn.commit()
    finally:
        conn.close()


def locked_accounts(db_path):
    """Names of the accounts currently locked (as passed to login_block),
    for the member list's "Locked" marker."""
    now = time.time()
    conn = _get_connection(db_path)
    try:
        # The pattern is a parameter, not inline SQL: psycopg2 would read
        # the "%" in a literal as a placeholder.
        rows = _exec(
            conn, "SELECT rate_key, locked_until FROM login_rate_limits "
                  "WHERE rate_key LIKE ? AND locked_until IS NOT NULL",
            ("account|%",),
        ).fetchall()
    finally:
        conn.close()
    return {key[len("account|"):] for key, until in rows if _lock_remaining(until, now) is not None}
