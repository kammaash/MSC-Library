"""
Smart-Lib 360\u00b0 (Smart Library & Learning Resource Management System)
Backend: Python Flask + SQLite (permanent database, via SQLAlchemy)

Run with:
    pip install -r requirements.txt
    python app.py

First run: there is no default admin account. Opening the app for the
first time on a fresh database.db redirects to the Setup Wizard
(/setup), where the school creates its own admin username and password.
"""
import os
import posixpath
import hashlib
import io
import json
import csv
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, date, timedelta

from flask import (
    Flask, request, jsonify, render_template, redirect, url_for,
    send_file, send_from_directory, session, flash, abort
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy.exc import IntegrityError
from sqlalchemy import inspect as sa_inspect, text as sa_text

from models import (
    db, User, Book, Member, Issue, FinePayment, LostBook, StockCheck,
    Periodical, Visitor, Donation, OnlineBook, LOAN_DAYS, FINE_PER_DAY,
    Bookmark, Favorite, ReadHistory, Certificate, DigitalLibraryVisit,
    PinResetRequest
)
import security
import backup_scheduler
from storage import get_storage

def _get_base_dir():
    """
    Where database.db, uploads/, and reports/ live — this must be a
    real, persistent folder next to wherever the app is running from.
    When built into a standalone .exe with PyInstaller, sys.executable
    points at the .exe itself; os.path.dirname(__file__) would instead
    point at PyInstaller's temporary extraction folder, which gets wiped
    out between runs — using that for the database would silently lose
    all data on every restart, so this deliberately branches for that case.

    LIBRARY_DATA_DIR, when set, overrides both. It exists so the automated
    tests can run against a throwaway directory instead of opening the real
    database.db next to app.py. It is checked first so it also wins inside
    a frozen build.
    """
    override = os.environ.get("LIBRARY_DATA_DIR")
    if override:
        os.makedirs(override, exist_ok=True)
        return os.path.abspath(override)
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = _get_base_dir()

# Where uploaded/generated files live: BASE_DIR/uploads on local disk
# (offline mode) or a Cloudflare R2 bucket (cloud mode), chosen from the R2_*
# environment variables. Always reach it through this module global, never a
# captured copy, so tests can swap it.
storage = get_storage(BASE_DIR)

# Public https address of the deployed site (no trailing slash). Empty in
# offline mode, where QR codes fall back to the server's LAN address.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# "Sign in with Microsoft" for students, through the school's Entra ID
# (Azure AD) tenant. Like the R2 settings, this is all-or-nothing: none of
# the three ENTRA_* variables means the feature is off and the routes below
# answer 404; all three means on; anything in between is a startup error,
# because a half-configured sign-in would fail confusingly for students.
# Empty strings count as unset. Entra also needs PUBLIC_BASE_URL, since the
# redirect address Microsoft sends the browser back to must be an exact,
# registered https address and cannot be guessed from the request.
_ENTRA_ENV_VARS = ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET")


def _entra_config(env):
    """Returns (enabled, tenant_id, client_id, client_secret) from env, or raises RuntimeError."""
    values = {name: (env.get(name) or "").strip() for name in _ENTRA_ENV_VARS}
    missing = [name for name, value in values.items() if not value]
    if len(missing) == len(_ENTRA_ENV_VARS):
        return False, "", "", ""
    if missing:
        raise RuntimeError(
            "Incomplete Microsoft sign-in configuration: missing " + ", ".join(missing)
            + ". Set all of " + ", ".join(_ENTRA_ENV_VARS) + " to enable it, or none of them to turn it off."
        )
    if not (env.get("PUBLIC_BASE_URL") or "").strip().rstrip("/"):
        raise RuntimeError(
            "Microsoft sign-in configuration is missing PUBLIC_BASE_URL. Set it to the site's "
            "public https address (e.g. https://library.example.org) so Microsoft can send "
            "students back to the right place."
        )
    return True, values["ENTRA_TENANT_ID"], values["ENTRA_CLIENT_ID"], values["ENTRA_CLIENT_SECRET"]


ENTRA_ENABLED, ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET = _entra_config(os.environ)

# How long a presigned R2 link for an image or backup stays valid. Short,
# because the browser follows the redirect immediately.
FILE_LINK_TTL_SECONDS = 300

# How long a presigned R2 link for a digital library item (PDF, video, audio)
# stays valid. Much longer than the image links above, because a video keeps
# fetching byte ranges from the same link for as long as it plays. The
# LIBRARY_LINK_TTL_SECONDS override exists so the expiry flow can be tested
# without waiting half an hour.
LINK_TTL_SECONDS = int(os.environ.get("LIBRARY_LINK_TTL_SECONDS", "1800"))


def _get_or_create_secret_key():
    """
    Every install needs its own random Flask session-signing key. The
    original code fell back to the literal string "change-this-secret-key"
    whenever LIBRARY_SECRET_KEY wasn't set in the environment — and since
    START_LIBRARY.bat never set it, every real install was silently
    running on that exact, publicly-visible placeholder. Anyone who knows
    it can forge a valid login session for any account, including Admin.

    Fix: generate a random key the first time this app ever starts on a
    given machine, and persist it to a small local file next to
    database.db so the same key is reused on every restart (a key that
    changes on every restart would just log everyone out constantly).
    LIBRARY_SECRET_KEY, if an admin sets it explicitly, still wins — this
    is only the fallback for the common case where nobody configured one.
    """
    env_key = os.environ.get("LIBRARY_SECRET_KEY")
    if env_key:
        return env_key
    key_path = os.path.join(BASE_DIR, "instance_secret.key")
    try:
        if os.path.exists(key_path):
            with open(key_path, "r", encoding="utf-8") as f:
                existing = f.read().strip()
            if existing:
                return existing
        new_key = secrets.token_hex(32)
        with open(key_path, "w", encoding="utf-8") as f:
            f.write(new_key)
        try:
            os.chmod(key_path, 0o600)  # best-effort; no-op on most Windows filesystems
        except Exception:
            pass
        return new_key
    except Exception:
        # Folder isn't writable for some reason. A random per-process key
        # (sessions won't survive a restart) is still far safer than
        # silently falling back to the old fixed placeholder string.
        return secrets.token_hex(32)

# templates/ and static/ are bundled *inside* a frozen .exe and extracted
# to a temporary folder (sys._MEIPASS) at startup — that's fine, since
# those are read-only app assets, not data that needs to persist.
if getattr(sys, "frozen", False):
    _BUNDLE_DIR = sys._MEIPASS
    _TEMPLATE_FOLDER = os.path.join(_BUNDLE_DIR, "templates")
    _STATIC_FOLDER = os.path.join(_BUNDLE_DIR, "static")
else:
    _TEMPLATE_FOLDER = None  # Flask's normal defaults
    _STATIC_FOLDER = None


def _logo_paths():
    """
    Resolves the two branding logo files (school logo + product logo)
    the same bundle-aware way Flask's own static route already does via
    _STATIC_FOLDER above. Used by both the certificate PDF and the
    report letterheads so there's exactly one place this can go wrong,
    instead of each PDF generator building its own path and risking the
    same "works in dev, silently missing in the packaged .exe" bug.
    """
    images_dir = _STATIC_FOLDER if _STATIC_FOLDER else os.path.join(BASE_DIR, "static")
    return (
        os.path.join(images_dir, "images", "logo_main.png"),
        os.path.join(images_dir, "images", "logo_smartlib360.png"),
    )

_CACHED_LAN_IP = None
def get_lan_ip():
    """
    The server computer's actual network address — the one other
    computers/phones on the same Wi-Fi can use to reach it. Used for the
    startup banner AND for QR codes, so a QR code always works from a
    phone regardless of whether the person who added the item was
    browsing via 127.0.0.1/localhost on the server PC itself (the most
    common case, since the Librarian is usually sitting at the server).
    Cached after the first call since a PC's LAN IP doesn't change
    mid-session.
    """
    global _CACHED_LAN_IP
    if _CACHED_LAN_IP:
        return _CACHED_LAN_IP
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # doesn't actually send anything, just picks the right network interface
        ip = s.getsockname()[0]
        s.close()
        _CACHED_LAN_IP = ip
        return ip
    except Exception:
        return "127.0.0.1"


def get_hostname():
    """
    This computer's Windows network name (e.g. "LIBRARY-PC"). On a normal
    school LAN, other Windows computers can resolve this name automatically
    (via NetBIOS) without needing the IP address at all — so
    http://LIBRARY-PC:5000 works from any other computer on the same
    network, and stays working even if the server's IP changes later
    (e.g. after a router reboot reassigns DHCP addresses).
    """
    import socket
    try:
        return socket.gethostname()
    except Exception:
        return None


if _TEMPLATE_FOLDER:
    app = Flask(__name__, template_folder=_TEMPLATE_FOLDER, static_folder=_STATIC_FOLDER)
else:
    app = Flask(__name__)


def _apply_proxy_fix(flask_app, env) -> bool:
    """Trust one reverse proxy's X-Forwarded-* headers, only when asked to.

    Returns True when ProxyFix was applied. Off by default on purpose: on a
    school LAN nothing sits in front of the app, so X-Forwarded-For is pure
    client-supplied input and trusting it would let anyone dodge their login
    lockout by faking a new address (see the comment in login(), which stays
    true for those installs). On Render every request arrives via Render's
    proxy, so without this request.remote_addr would be the proxy's address
    for everybody and one student's failed attempts would lock out the whole
    school. The deployment sets LIBRARY_TRUST_PROXY=1 and the app can then
    only be reached through that proxy, which sets the headers itself.
    """
    if env.get("LIBRARY_TRUST_PROXY") != "1":
        return False
    from werkzeug.middleware.proxy_fix import ProxyFix
    # x_host stays 0: nothing needs the forwarded host, and trusting it would
    # let a client choose request.host, which _force_https uses as the target
    # of its redirect.
    flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_for=1, x_proto=1, x_host=0)
    return True


_apply_proxy_fix(app, os.environ)
app.config["SECRET_KEY"] = _get_or_create_secret_key()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# A LAN deployment (the normal case) is plain HTTP — forcing Secure
# cookies there would silently break login, since the browser would
# refuse to send the cookie back at all. An internet-facing deployment
# behind HTTPS (e.g. Nginx + Let's Encrypt) should set
# LIBRARY_FORCE_HTTPS_COOKIES=1 so the session cookie is only ever sent
# over an encrypted connection. The same flag now also switches on the
# HTTP->HTTPS redirect and HSTS header just below — all three only make
# sense together, and only for an internet-facing install.
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("LIBRARY_FORCE_HTTPS_COOKIES") == "1"
_FORCE_HTTPS = app.config["SESSION_COOKIE_SECURE"]

# --------------------------------------------------------------------------
# Force HTTPS at the app level, not just at Nginx
# --------------------------------------------------------------------------
# Previously this app relied entirely on Nginx to redirect HTTP -> HTTPS —
# correct when the reverse proxy config is set up right, but that means a
# misconfigured or accidentally-reverted Nginx config (or someone standing
# up a new proxy in front of this later) could silently start serving the
# login page over plain HTTP with no warning. Waitress itself only ever
# speaks plain HTTP — TLS is terminated at Nginx (see
# deploy/nginx_smartlib360.conf), which sets X-Forwarded-Proto so the app
# can tell what scheme the browser actually used. Since the app is bound to
# 127.0.0.1 in production (LIBRARY_BIND_HOST=127.0.0.1) and only reachable
# through that proxy, trusting this header here is safe — nothing outside
# Nginx can reach the app directly to spoof it.
#
# Gated on the same LIBRARY_FORCE_HTTPS_COOKIES flag as the Secure cookie
# above: a LAN install never sets it, so this redirect and header are
# simply never added there — zero behavior change for existing LAN setups.
@app.before_request
def _force_https():
    if not _FORCE_HTTPS:
        return None
    proto = request.headers.get("X-Forwarded-Proto", "http")
    if proto == "https":
        return None
    target = f"https://{request.host}{request.full_path}"
    if target.endswith("?"):
        target = target[:-1]
    return redirect(target, code=301)


# Content-Security-Policy: which origins a page may load scripts, images,
# media, frames and XHR from. script-src 'self' with no 'unsafe-inline' means
# an injected <script> or onclick="" never runs, which is why the templates
# keep their code in static/js/ and app.js attaches every handler itself.
# style-src does allow inline styles: the markup uses style="" throughout, and
# that is not a script-execution risk. epub.js renders a book through blob:
# URLs (pages, images, stylesheets), and in R2 mode PDFs, videos, audio, QR
# codes and EPUB downloads come from presigned links on the bucket's own
# host, so that origin is allowed for those kinds of content only.
# Sent with the HTTPS flag, like the other hardening headers, and only on
# HTML pages: a policy on the PDF response itself can stop the browser's
# built-in PDF viewer from rendering it.
_SEND_CSP = _FORCE_HTTPS


def _file_link_origin():
    """
    scheme://host of the presigned file links, or "" in local mode, where
    files are served by this app. Taken from the R2 client's endpoint
    (https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com; links are path-style
    on that host) so it always matches the links actually handed out.
    """
    client = getattr(storage, "client", None)
    endpoint = getattr(getattr(client, "meta", None), "endpoint_url", None)
    if not endpoint:
        return ""
    parts = urlsplit(endpoint)
    return f"{parts.scheme}://{parts.netloc}"


def _content_security_policy():
    files = _file_link_origin()
    extra = f" {files}" if files else ""
    return "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' blob:",
        f"img-src 'self' data: blob:{extra}",
        f"media-src 'self' blob:{extra}",
        f"frame-src 'self' blob:{extra}",
        f"connect-src 'self'{extra}",
        "font-src 'self' data: blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'self'",
    ])


@app.after_request
def _set_security_headers(response):
    if _SEND_CSP and response.mimetype == "text/html":
        response.headers["Content-Security-Policy"] = _content_security_policy()
    if _FORCE_HTTPS:
        # Tells the browser to remember, for a year, to always use HTTPS for
        # this exact site — even a bookmarked or typed http:// link gets
        # rewritten to https:// by the browser itself before the request is
        # ever sent, closing the brief window a redirect alone leaves open
        # on someone's very first visit (and on that first visit, before
        # this header has been seen once, the 301 redirect above still
        # covers it).
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Defense-in-depth headers that only make sense once real students
        # are browsing this from arbitrary home networks/devices, not a
        # school LAN — cheap to add, no downside for a LAN install skipping
        # them, so kept behind the same flag as everything else here.
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# Persistent file logging, in addition to the console window. The console
# disappears the moment the process exits/crashes, which makes an actual
# crash undiagnosable after the fact — this writes the same messages (plus
# every unhandled exception, via the error handler further down) to a
# rotating log file next to the database, so a future incident leaves
# evidence someone can actually look at.
_LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_file_handler = RotatingFileHandler(
    os.path.join(_LOG_DIR, "app.log"), maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_file_handler.setLevel(logging.INFO)
app.logger.addHandler(_file_handler)
app.logger.setLevel(logging.INFO)
logging.getLogger("waitress").addHandler(_file_handler)

DB_PATH = os.path.join(BASE_DIR, "database.db")
# In production (Render), DATABASE_URL is set to Neon's Postgres connection
# string as an environment variable. Locally, it's unset, so this falls back
# to the same SQLite file the app has always used — no behavior change for
# local/offline running via RUN_SmartLib360.bat.
def _sqlalchemy_database_uri(raw: str) -> str:
    """Turn the DATABASE_URL a provider hands out into the URL SQLAlchemy needs.

    Render and Neon give "postgres://" or "postgresql://" URLs. Plain
    "postgresql://" selects whichever driver SQLAlchemy defaults to, which is
    psycopg2 in 2.0 but psycopg v3 from 2.1 (not installed here), so the
    driver is named explicitly. Anything else — SQLite, or a URL that already
    names a driver — is returned unchanged.
    """
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg2://" + raw[len(prefix):]
    return raw


_database_url = os.environ.get("DATABASE_URL")
if _database_url:
    # security.py keeps using the raw DATABASE_URL (psycopg2.connect wants the
    # plain form, not the "+psycopg2" SQLAlchemy spelling).
    app.config["SQLALCHEMY_DATABASE_URI"] = _sqlalchemy_database_uri(_database_url)
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + DB_PATH
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Applies to every real connection the app actually uses for requests —
# unlike a one-off PRAGMA on a throwaway connection, this is passed straight
# to the underlying driver's connect() call by SQLAlchemy for each connection
# it opens. The SQLite and Postgres drivers use different keyword names for
# the same idea, so this has to branch on which database is actually in use —
# passing SQLite's "timeout" straight to psycopg2 raises a TypeError.
if _database_url:
    # connect_timeout is seconds-to-establish-a-connection for psycopg2, not
    # a per-query timeout — there's no exact equivalent to SQLite's lock-wait
    # timeout here, so this just guards against a hung connection attempt.
    #
    # Neon closes idle connections (and scales its compute to zero), so a
    # pooled connection can be dead by the time a request picks it up.
    # pool_pre_ping tests each connection first and replaces a dead one;
    # pool_recycle retires connections before Neon's idle cut-off.
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"connect_timeout": 15},
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }
else:
    # Without this, a burst of near-simultaneous writes (e.g. a whole class
    # logging in and each triggering a small write) can hit "database is
    # locked" well before 15 seconds of actual contention.
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"timeout": 15}}
# Largest single request body we'll accept — covers the biggest allowed
# Digital Library file (400 MB video) plus room for a bulk-import batch of
# several files at once. Setting this explicitly avoids relying on
# whatever a given Werkzeug version's built-in default happens to be.
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB

# The software's own product identity — distinct from the school's name
# (which is per-install and captured via the Setup Wizard, see below).
# Kept as constants in one place so a future rebrand only touches these
# two lines instead of hunting through every template.
PRODUCT_NAME = "Smart-Lib 360\u00b0"
PRODUCT_DESCRIPTOR = "Smart Library & Learning Resource Management System"
PRODUCT_TAGLINE = "Manage \u2022 Connect \u2022 Learn \u2022 Grow"


@app.context_processor
def inject_school_name():
    """
    Makes {{ school_name }}, {{ school_address }}, {{ product_name }}, and
    {{ product_tagline }} available in every template automatically.
    School name/address come from whatever was entered in the first-run
    Setup Wizard (security.py's app_security table); product name/tagline
    are fixed software branding, not per-school. Falls back to sensible
    defaults if setup hasn't been run yet or the DB isn't ready (e.g.
    during CLI scripts / early startup).
    """
    try:
        return {
            "school_name": security.get_school_name(DB_PATH),
            "school_address": security.get_school_address(DB_PATH),
            "product_name": PRODUCT_NAME,
            "product_descriptor": PRODUCT_DESCRIPTOR,
            "product_tagline": PRODUCT_TAGLINE,
        }
    except Exception:
        return {
            "school_name": "Montessori Senior Secondary School",
            "school_address": "A-camp, Kurnool",
            "product_name": PRODUCT_NAME,
            "product_descriptor": PRODUCT_DESCRIPTOR,
            "product_tagline": PRODUCT_TAGLINE,
        }
app.config["MAX_FORM_PARTS"] = 5000  # generous headroom for large bulk-import batches
app.config["UPLOAD_BARCODES"] = os.path.join(BASE_DIR, "uploads", "barcodes")
app.config["UPLOAD_QRCODES"] = os.path.join(BASE_DIR, "uploads", "qrcodes")
app.config["UPLOAD_BACKUPS"] = os.path.join(BASE_DIR, "uploads", "backups")
# Scratch space for incoming digital-library uploads (see _save_digital_upload).
_UPLOAD_WORK_DIR = os.path.join(BASE_DIR, "upload_tmp")
app.config["REPORTS_DIR"] = os.path.join(BASE_DIR, "reports", "pdf")

# Online Books (PDF library) upload limits
EBOOK_MAX_MB = 40
EBOOK_ALLOWED_EXT = {".pdf"}
RENEWAL_LIMIT = 2  # max times a single loan can be renewed before it must be returned/reissued
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=6)

# ---------------------------------------------------------------------------
# Session safety: every time the application process starts (i.e. every time
# python app.py / START_LIBRARY.bat is run), this changes. Any login session
# issued by a previous run of the app — including one a browser silently
# restored after being closed and reopened — is treated as stale and forces
# a fresh login instead of walking straight back in.
# ---------------------------------------------------------------------------
SERVER_INSTANCE_ID = secrets.token_hex(8)


@app.before_request
def _enforce_fresh_session():
    if "_user_id" in session and session.get("srv") != SERVER_INSTANCE_ID:
        session.clear()


# --------------------------------------------------------------------------
# CSRF protection
# --------------------------------------------------------------------------
# The app previously had none: every POST/PUT/DELETE relied only on the
# session cookie, so a malicious page visited in another tab could submit
# requests to this app using the signed-in user's own session (classic
# CSRF). Flask's default SameSite=Lax cookie blocks the crudest version of
# this, but it isn't a substitute for a real token check.
#
# This uses the standard "synchronizer token" pattern: every session gets
# a random token (stored server-side in the session, never guessable from
# outside), every page render hands that token to its own JavaScript via
# a data-csrf attribute, and every mutating request must echo it back in
# an X-CSRF-Token header. A page on another site has no way to read that
# attribute or the session cookie, so it cannot produce a valid token.
@app.before_request
def _csrf_protect():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["_csrf_token"] = token
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        sent = request.headers.get("X-CSRF-Token")
        if not sent or not secrets.compare_digest(sent, token):
            return jsonify({
                "error": "Your session could not be verified (it may have expired). "
                         "Please refresh the page and try again.",
                "csrf_failed": True,
            }), 400
    return None


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": session.get("_csrf_token", "")}


# Routes reachable even while setup hasn't run yet, or while the signed-in
# user still has a forced password change pending. Static assets and
# uploaded files (logos etc.) must stay reachable too, or the wizard/login
# pages themselves render broken.
_SETUP_EXEMPT_PREFIXES = ("/setup", "/static", "/uploads")
_PASSWORD_CHANGE_EXEMPT_PREFIXES = ("/static", "/uploads", "/logout")
_PASSWORD_CHANGE_EXEMPT_PATHS = ("/change-password", "/api/account/change-password", "/api/whoami")


# Once the setup flag has been seen as complete it stays complete for the life
# of this process, so remember that instead of asking the database on every
# request (on Postgres each check opened a brand-new connection).
_SETUP_COMPLETE_CACHE = False


@app.before_request
def _require_first_run_setup():
    """
    On a brand-new install with zero accounts, every request except the
    setup wizard itself gets redirected there — replacing the old
    behaviour of silently creating admin/admin123, lib123, and view123
    on first launch. This is the school's one chance to pick their own
    admin credentials before the system is usable at all.
    """
    if request.path.startswith(_SETUP_EXEMPT_PREFIXES):
        return None
    global _SETUP_COMPLETE_CACHE
    if _SETUP_COMPLETE_CACHE:
        return None
    if security.setup_is_complete(DB_PATH):
        _SETUP_COMPLETE_CACHE = True
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Initial setup has not been completed.", "setup_required": True}), 403
    return redirect(url_for("setup_wizard"))


@app.before_request
def _enforce_password_change():
    """
    If the signed-in account has been flagged (by the setup wizard, or by
    an admin resetting someone's password) as needing a new password
    before doing anything else, every other route is blocked until they
    change it — closing the gap where a forced reset could otherwise be
    skipped indefinitely.
    """
    if not current_user.is_authenticated:
        return None
    if request.path.startswith(_PASSWORD_CHANGE_EXEMPT_PREFIXES) or request.path in _PASSWORD_CHANGE_EXEMPT_PATHS:
        return None
    if not security.must_change_password(DB_PATH, current_user.id):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Password change required.", "must_change_password": True}), 403
    return redirect(url_for("change_password_page"))


@app.after_request
def _prevent_page_caching(response):
    """
    On a shared school computer, the browser's own cache can otherwise
    serve a PREVIOUS person's already-rendered page (with their role's
    menus baked in) to the NEXT person who logs in, if they don't do a
    hard refresh — this is what caused students to briefly see Admin
    menu options. Every response now explicitly tells the browser never
    to reuse a cached copy, so each login always gets a fresh render
    for whoever is actually signed in.
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.errorhandler(Exception)
def _handle_unexpected_error(e):
    """
    Any API route that raises an unhandled exception returns a clear JSON
    error instead of an HTML error page — an HTML page breaks the frontend's
    res.json() call and shows a vague "Import failed" / "Request failed"
    message with no way to tell what actually happened.
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            # e.g. 413 Payload Too Large, 400 Bad Request from a malformed
            # upload — these used to fall through as a raw HTML error page,
            # which broke the frontend's JSON parsing. Now they come back
            # as a real, readable message instead.
            return jsonify({"error": e.description or str(e)}), e.code
        return e
    # The exception text can contain SQL, bound parameters or hostnames, so it
    # goes to the log only, never into the response.
    app.logger.exception("Unhandled error on %s", request.path)
    if request.path.startswith("/api/"):
        return jsonify({"error": "Something went wrong. Please try again."}), 500
    raise e

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# --------------------------------------------------------------------------
# Access control helpers
# --------------------------------------------------------------------------
def role_required(*roles):
    from functools import wraps

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return jsonify({"error": "Not authenticated"}), 401
            if current_user.role not in roles:
                return jsonify({"error": "Forbidden — insufficient role"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def today_iso():
    return date.today().isoformat()


def ddmmyyyy_from_iso(iso_str):
    if not iso_str:
        return ""
    y, m, d = iso_str.split("-")
    return f"{d}{m}{y}"


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------
def _finish_student_login(member):
    """
    Signs the browser in as this student, provisioning their login account
    on first use. Shared by the PIN login above and Sign in with Microsoft
    below: whichever way a student proved who they are, they end up with
    exactly the same kind of session.
    """
    user = User.query.filter_by(member_id=member.id, role="student").first()
    if not user:
        # auto-provision a student login tied to the member record
        user = User(username=f"student-{member.member_id}", password_hash="", name=member.name,
                    role="student", member_id=member.id)
        db.session.add(user)
        db.session.commit()
    # Drop whatever the browser had before signing in (a pre-login session
    # id/contents an attacker might have planted: session fixation). The next
    # request issues a fresh CSRF token.
    session.clear()
    login_user(user)
    session.permanent = False  # dies when the browser fully closes — no staying logged in on a shared school PC
    session["srv"] = SERVER_INSTANCE_ID
    return user


# Shown on the login page after a failed Microsoft sign-in. The page is sent
# only a short code in ?ms_error=; the wording lives here, so nothing a
# visitor puts in the address bar is ever displayed back to them.
_MS_ERROR_MESSAGES = {
    "not_linked": "Your Microsoft account isn't linked to a library card yet. "
                  "Ask the librarian to add your school email to your library card.",
    "wrong_school": "Please sign in with your school Microsoft account.",
    "failed": "Microsoft sign-in didn't complete. Please try again.",
}


def _sign_in_blocked_response(client_ip, pair_name, account):
    """The 429 for a sign-in attempt that a lock refuses, or None."""
    blocked = security.login_block(DB_PATH, client_ip, pair_name, account)
    if not blocked:
        return None
    reason, seconds = blocked
    if seconds is None:
        return jsonify({"error": "This library card is locked after too many failed sign-in attempts. "
                                 "Please ask the librarian to unlock it."}), 429
    minutes = max(1, seconds // 60)
    return jsonify({
        "error": f"Too many failed sign-in attempts. Please try again in about {minutes} minute(s)."
    }), 429


# Letters and digits that cannot be confused when read aloud or copied off
# a slip of paper (no 0/O, 1/I/L).
_SETUP_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_SETUP_CODE_LENGTH = 6


def _new_setup_code():
    return "".join(secrets.choice(_SETUP_CODE_ALPHABET) for _ in range(_SETUP_CODE_LENGTH))


def _normalize_setup_code(value):
    """Uppercased, with the spaces and dashes people add when copying it."""
    return re.sub(r"[\s-]", "", str(value or "")).upper()


def _student_account(member):
    """The account name the sign-in counters use for this student."""
    return f"student:{member.admission_no or ''}"


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template(
            "login.html",
            entra_enabled=ENTRA_ENABLED,
            ms_error_message=_MS_ERROR_MESSAGES.get(request.args.get("ms_error", "")),
        )

    data = request.get_json(silent=True) or request.form
    # Anything but "student" is the staff form. Normalised so the counter
    # names below cannot be dodged by sending an invented role_mode.
    role_mode = "student" if data.get("role_mode") == "student" else "staff"
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    # Deliberately NOT trusting the X-Forwarded-For header here: this app
    # has no reverse proxy in front of it on a school LAN, so that header
    # is pure client-supplied input — anyone can set it via devtools or a
    # browser extension to get a "fresh" identity and bypass their own
    # lockout. request.remote_addr is the real TCP connection address and
    # can't be spoofed the same way. On Render, LIBRARY_TRUST_PROXY=1 turns on
    # ProxyFix (see _apply_proxy_fix), which makes remote_addr the real client
    # IP taken from Render's own proxy hop rather than the proxy's address.
    client_ip = request.remote_addr or "unknown"
    account = f"{role_mode}:{username}"

    # Login throttling: three counters (this address + username, the
    # account from any address, and this address across all usernames);
    # see the comment above security.MAX_ATTEMPTS.
    blocked = _sign_in_blocked_response(client_ip, account, account)
    if blocked:
        return blocked

    if role_mode == "student":
        member = Member.query.filter_by(admission_no=username, type="Student").first()

        def failed(message, **extra):
            remaining = security.record_login_failure(
                DB_PATH, client_ip, account, account, account_exists=member is not None)
            if extra and remaining > 0:
                message += f" ({remaining} attempt{'s' if remaining != 1 else ''} left before sign-in is locked.)"
            return jsonify({"error": message, "attempts_remaining": remaining, **extra}), 401

        if not member or not member.dob or ddmmyyyy_from_iso(member.dob) != password:
            return failed("Admission number or date of birth does not match any student.")

        # DOB alone identifies a child but isn't a real secret — it's
        # often semi-public (shared casually, on social media, guessable
        # alongside a sequential admission number). A PIN is required in
        # addition to it. Creating that PIN (the first time, or after a
        # reset) needs a one-time set-up code the librarian hands over in
        # person; otherwise whoever knew the DOB first could claim the card.
        if not member.pin_hash:
            if not member.pin_setup_code:
                return jsonify({"error": "Your library card needs a set-up code before you can sign in. "
                                         "Please ask the librarian for one."}), 403
            setup_code = _normalize_setup_code(data.get("setup_code"))
            if not setup_code:
                # DOB was correct; ask the frontend to show the set-up step.
                return jsonify({"ok": False, "need_setup_code": True,
                                "message": "First-time sign in — enter the set-up code from your "
                                           "librarian and create a 4 to 6 digit PIN."}), 200
            # Bytes, not str: compare_digest raises on non-ASCII text.
            if not secrets.compare_digest(setup_code.encode(), member.pin_setup_code.encode()):
                return failed("That set-up code is not right. Please check it with the librarian.",
                              need_setup_code=True)
            new_pin = (data.get("new_pin") or "").strip()
            confirm_pin = (data.get("confirm_pin") or "").strip()
            if not new_pin.isdigit() or not (4 <= len(new_pin) <= 6):
                return jsonify({"error": "PIN must be 4 to 6 digits.", "need_setup_code": True}), 400
            if new_pin != confirm_pin:
                return jsonify({"error": "PIN and confirmation do not match.", "need_setup_code": True}), 400
            member.pin_hash = generate_password_hash(new_pin)
            member.pin_setup_code = None
            db.session.commit()
        else:
            pin = (data.get("pin") or "").strip()
            if not pin:
                # DOB was correct; ask the frontend to show the PIN box.
                return jsonify({"ok": False, "need_pin": True, "message": "Please enter your PIN."}), 200
            if not check_password_hash(member.pin_hash, pin):
                # Tell the student how many tries are left before the
                # lock, and explicitly send need_pin back so the frontend
                # knows to keep the PIN box open (not send them back to
                # square one) and can clear the wrong PIN they just typed
                # instead of leaving it sitting there.
                return failed("Incorrect PIN. Please try again.", need_pin=True)
        security.record_login_success(DB_PATH, client_ip, account, account)

        user = _finish_student_login(member)
        return jsonify({"ok": True, "user": {"name": user.name, "role": user.role}})

    user = User.query.filter_by(username=username).first()
    if not user or not check_password_hash(user.password_hash, password):
        security.record_login_failure(DB_PATH, client_ip, account, account, account_exists=user is not None,
                                      account_lock_seconds=security.STAFF_ACCOUNT_LOCK_SECONDS)
        return jsonify({"error": "Invalid username or password."}), 401
    security.record_login_success(DB_PATH, client_ip, account, account)
    # Same session-fixation hygiene as the student path above.
    session.clear()
    login_user(user)
    session.permanent = False  # dies when the browser fully closes — no staying logged in on a shared school PC
    session["srv"] = SERVER_INSTANCE_ID
    must_change = security.must_change_password(DB_PATH, user.id)
    return jsonify({
        "ok": True,
        "user": {"name": user.name, "role": user.role},
        "must_change_password": must_change,
    })


# --------------------------------------------------------------------------
# Sign in with Microsoft (students only)
# --------------------------------------------------------------------------
# Standard OpenID Connect authorization-code flow, done by msal. The browser
# is sent to Microsoft, the school account signs in there, and Microsoft
# redirects back to /auth/microsoft/callback with a one-time code. Both
# routes are GET and need no existing login, and no before_request hook above
# blocks them once setup is complete (the setup and password-change hooks only
# act on a fresh install or an already signed-in account). The callback is a
# GET on purpose: response_mode=form_post would make Microsoft POST back
# cross-site, and the SameSite=Lax session cookie holding the flow state
# would not be sent with that request.
#
# Staff and admin accounts keep their username and password; only members
# whose library card is type "Student" can come in this way.
def _entra_app():
    # Imported here so offline installs that never enable Microsoft sign-in
    # don't need msal loaded. Tests replace this whole function.
    import msal
    return msal.ConfidentialClientApplication(
        ENTRA_CLIENT_ID,
        client_credential=ENTRA_CLIENT_SECRET,
        authority=f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}",
    )


def _microsoft_failure(code):
    return redirect(url_for("login", ms_error=code))


@app.route("/auth/microsoft/login")
def microsoft_login():
    if not ENTRA_ENABLED:
        abort(404)
    # The callback clears the session and signs in as a student. A staff
    # member who clicks the button while signed in must not be silently
    # downgraded to a student session, so send them back to the app.
    if current_user.is_authenticated and current_user.role != "student":
        return redirect("/")
    flow = _entra_app().initiate_auth_code_flow(
        ["User.Read"],
        redirect_uri=PUBLIC_BASE_URL + "/auth/microsoft/callback",
        # School computers are shared: always show the account picker, so a
        # student is never silently signed in as the previous student whose
        # Microsoft session is still open in that browser.
        prompt="select_account",
    )
    # Holds the state, nonce and PKCE verifier for this one attempt. The
    # callback removes it as soon as it reads it, so a callback URL cannot
    # be replayed.
    session["entra_flow"] = flow
    return redirect(flow["auth_uri"])


@app.route("/auth/microsoft/callback")
def microsoft_callback():
    if not ENTRA_ENABLED:
        abort(404)

    # No stored flow means this browser never started a sign-in (or it was
    # already used, or the session expired). Refuse before talking to Microsoft.
    flow = session.pop("entra_flow", None)
    if not flow:
        return _microsoft_failure("failed")

    # msal checks the returned state against the flow and the nonce in the ID
    # token, and the token itself arrives over TLS straight from Microsoft's
    # token endpoint in exchange for our one-time code and PKCE verifier. The
    # installed msal does NOT verify the ID token's signature, so the tenant
    # and audience claims are checked below ourselves. msal raises or returns
    # an "error" if its own checks fail. Only the error code is logged: never
    # the token, the code, or Microsoft's free-text description.
    try:
        result = _entra_app().acquire_token_by_auth_code_flow(flow, request.args.to_dict())
    except Exception as e:
        app.logger.warning("Microsoft sign-in failed: %s", type(e).__name__)
        return _microsoft_failure("failed")
    if not isinstance(result, dict) or "error" in result:
        app.logger.warning("Microsoft sign-in failed: %s",
                           result.get("error") if isinstance(result, dict) else "unexpected result")
        return _microsoft_failure("failed")

    claims = result.get("id_token_claims") or {}
    tid = str(claims.get("tid") or "").strip()
    oid = str(claims.get("oid") or "").strip()
    if not tid or not oid:
        return _microsoft_failure("failed")
    # A personal or other school's Microsoft account can sign in to this app
    # registration only if the registration allows it; refuse here regardless.
    if tid.lower() != ENTRA_TENANT_ID.lower():
        return _microsoft_failure("wrong_school")
    # The token must have been issued to this application, not another one.
    if str(claims.get("aud") or "") != ENTRA_CLIENT_ID:
        return _microsoft_failure("failed")

    email = str(claims.get("preferred_username") or claims.get("email") or "").strip().lower()

    # Match on the stable object id first. Once an account is bound to a
    # library card, a renamed email in Entra makes no difference, and the
    # email is never consulted again for that account.
    bound = Member.query.filter_by(entra_oid=oid).all()
    if bound:
        candidates = bound
        bind_oid = False
    elif email:
        # First sign-in: find the card by the school email the librarian
        # recorded. Compared case-insensitively because rows can predate
        # the lowercase rule.
        candidates = Member.query.filter(
            db.func.lower(Member.email) == email, Member.type == "Student").all()
        bind_oid = True
    else:
        candidates = []
        bind_oid = False

    # Exactly one Student card, or nobody gets in: zero is simply not
    # linked, and more than one (should never happen, uniqueness is
    # enforced when saving) is ambiguous, so it is refused too.
    if len(candidates) != 1 or candidates[0].type != "Student":
        return _microsoft_failure("not_linked")
    member = candidates[0]
    # An email already bound to a different Microsoft account is never
    # silently re-pointed; the librarian must change the email first.
    if bind_oid and member.entra_oid and member.entra_oid != oid:
        return _microsoft_failure("not_linked")
    if bind_oid:
        member.entra_oid = oid
        db.session.commit()

    # Start from an empty session before signing in, so nothing planted in
    # the browser's earlier session (session fixation) carries over. A new
    # CSRF token is issued on the next request.
    session.clear()
    _finish_student_login(member)
    return redirect("/")


@app.route("/api/pin-reset-request", methods=["POST"])
def request_pin_reset():
    """
    Self-service "Forgot my PIN?" — reachable from the login page,
    before the student is signed in. Re-checks admission no. + DOB
    (same as a normal login attempt) so this can't be spammed by
    someone who knows neither, and shares the same rate limiter as
    /login so it can't be brute-forced either.

    Deliberately does NOT clear the PIN itself. See the PinResetRequest
    model docstring for why: DOB + admission number are exactly what
    this feature is meant to be protected against, so a bare
    self-service reset using only those two would just reopen the same
    hole the PIN was added to close. This only files the request; a
    librarian/admin has to approve it after actually verifying who's
    asking (see /api/pin-reset-requests/<id>/approve below).
    """
    data = request.get_json(silent=True) or {}
    username = (data.get("admission_no") or "").strip()
    dob_input = (data.get("dob") or "").strip()
    client_ip = request.remote_addr or "unknown"
    # Its own (IP, username) counter, but the same account and per-address
    # counters as /login: a DOB guessed here is a DOB guessed there.
    pair_name, account = f"pinreq:{username}", f"student:{username}"

    blocked = _sign_in_blocked_response(client_ip, pair_name, account)
    if blocked:
        return blocked

    member = Member.query.filter_by(admission_no=username, type="Student").first()
    if not member or not member.dob or ddmmyyyy_from_iso(member.dob) != dob_input:
        security.record_login_failure(DB_PATH, client_ip, pair_name, account, account_exists=member is not None)
        return jsonify({"error": "Admission number or date of birth does not match any student."}), 401

    if not member.pin_hash:
        return jsonify({"error": "This card doesn't have a PIN yet — ask the librarian for a set-up code, "
                                 "then sign in normally and you'll be asked to create one."}), 400

    existing = PinResetRequest.query.filter_by(member_id=member.member_id, status="Pending").first()
    if existing:
        return jsonify({"ok": True, "message": "A request is already pending for you — ask your librarian to approve it, then log in again to set a new PIN."})

    req = PinResetRequest(member_id=member.member_id, status="Pending",
                           requested_at=date.today().isoformat())
    db.session.add(req)
    db.session.commit()
    return jsonify({"ok": True, "message": "Request sent. Show your ID to a librarian/admin so they can approve it, then log in again to set a new PIN."})


@app.route("/api/pin-reset-requests", methods=["GET"])
@login_required
@role_required("admin", "librarian")
def list_pin_reset_requests():
    """Pending requests only, newest first — this is meant to be checked like a small inbox, not an audit log (approved/denied ones stay in the DB but aren't listed here)."""
    requests = PinResetRequest.query.filter_by(status="Pending").order_by(PinResetRequest.id.desc()).all()
    out = []
    for r in requests:
        member = Member.query.filter_by(member_id=r.member_id).first()
        out.append({
            "id": r.id,
            "member_id": r.member_id,
            "member_name": member.name if member else "(deleted member)",
            "class_dept": member.class_dept if member else "",
            "admission_no": member.admission_no if member else "",
            "requested_at": r.requested_at,
        })
    return jsonify(out)


@app.route("/api/pin-reset-requests/<int:req_id>/approve", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def approve_pin_reset_request(req_id):
    """
    Confirms you've actually verified this is the real student (in
    person, or by calling the parent number on file) before clearing
    their PIN — same trust check as the existing per-member "Reset PIN"
    action, just triggered from a request instead of picked from the
    member list directly.
    """
    req = PinResetRequest.query.get_or_404(req_id)
    if req.status != "Pending":
        return jsonify({"error": "This request has already been reviewed."}), 400
    member = Member.query.filter_by(member_id=req.member_id).first()
    if not member:
        return jsonify({"error": "That member no longer exists."}), 404
    code = _reset_student_pin(member)
    req.status = "Approved"
    req.reviewed_by = current_user.name
    req.reviewed_at = date.today().isoformat()
    db.session.commit()
    return jsonify({"ok": True, "pin_setup_code": code})


@app.route("/api/pin-reset-requests/<int:req_id>/deny", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def deny_pin_reset_request(req_id):
    req = PinResetRequest.query.get_or_404(req_id)
    if req.status != "Pending":
        return jsonify({"error": "This request has already been reviewed."}), 400
    req.status = "Denied"
    req.reviewed_by = current_user.name
    req.reviewed_at = date.today().isoformat()
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    return jsonify({"ok": True})


def _save_school_logo(logo_file):
    """
    Shared by the Setup Wizard and the Manage Users > School Branding
    panel. Saves an uploaded image over static/images/logo_main.png,
    resized so an oversized photo doesn't blow up the layout. Raises on
    failure — callers decide whether that's fatal for their route.
    """
    from PIL import Image
    img = Image.open(logo_file.stream)
    img = img.convert("RGBA")
    max_width = 600
    if img.width > max_width:
        ratio = max_width / float(img.width)
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
    logo_path = os.path.join(app.static_folder, "images", "logo_main.png")
    os.makedirs(os.path.dirname(logo_path), exist_ok=True)
    img.save(logo_path, "PNG")


@app.route("/api/branding", methods=["POST"])
@login_required
@role_required("admin")
def update_branding():
    """
    Lets an Admin set/change the institution name, address, and logo at
    any time after initial setup — not just during the one-time wizard.
    This is what makes the "existing database, setup auto-marked
    complete without ever capturing a name" case (see init_db()'s
    upgrade-safety branch) recoverable through the UI, instead of
    leaving an install permanently stuck on generic placeholder text.
    """
    data = request.form
    school_name = (data.get("school_name") or "").strip()
    school_address = (data.get("school_address") or "").strip()
    if not school_name:
        return jsonify({"error": "Institution name is required."}), 400

    security.mark_setup_complete(DB_PATH, school_name=school_name, school_address=school_address)

    logo_file = request.files.get("school_logo")
    if logo_file and logo_file.filename:
        try:
            _save_school_logo(logo_file)
        except Exception as e:
            app.logger.warning(f"Branding logo upload failed: {e}")
            return jsonify({"ok": True, "warning": "Name/address saved, but the logo file could not be processed."})

    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# First-run Setup Wizard — replaces the old hardcoded admin/admin123
# (+ librarian/lib123, viewer/view123) default accounts. Runs exactly
# once, on a database with zero User rows; see _require_first_run_setup
# above and security.setup_is_complete().
# --------------------------------------------------------------------------
def _setup_token_problem(supplied):
    """
    On an internet-facing deployment (LIBRARY_FORCE_HTTPS_COOKIES=1) the
    wizard is the one page where whoever reaches a fresh database first
    becomes admin, so it also needs LIBRARY_SETUP_TOKEN, a secret set in the
    host's environment and typed into the address as /setup?token=...
    Returns the error message, or None when the request may proceed. With
    the flag on and no token configured, setup is refused outright rather
    than left open. A LAN install (flag off) needs no token.
    """
    if not _FORCE_HTTPS:
        return None
    expected = os.environ.get("LIBRARY_SETUP_TOKEN") or ""
    if not expected:
        return "Setup is disabled: set LIBRARY_SETUP_TOKEN in the server's environment first."
    # Bytes, not str: compare_digest raises on non-ASCII text.
    if not secrets.compare_digest(str(supplied or "").encode(), expected.encode()):
        return "This setup link is missing its token or the token is wrong."
    return None


@app.route("/setup", methods=["GET", "POST"])
def setup_wizard():
    if security.setup_is_complete(DB_PATH):
        return redirect(url_for("login"))

    if request.method == "GET":
        problem = _setup_token_problem(request.args.get("token"))
        if problem:
            return problem, 400
        return render_template("setup.html", setup_token=request.args.get("token") or "")

    # The token can be guessed at, so POST shares the sign-in counters
    # (this address's failures, and a pair counter of its own).
    client_ip = request.remote_addr or "unknown"
    blocked = _sign_in_blocked_response(client_ip, "setup", "setup")
    if blocked:
        return blocked
    data = request.get_json(silent=True) or request.form
    problem = _setup_token_problem(data.get("setup_token"))
    if problem:
        security.record_login_failure(DB_PATH, client_ip, "setup", "setup", account_exists=False)
        return jsonify({"error": problem}), 400

    school_name = (data.get("school_name") or "").strip()
    school_address = (data.get("school_address") or "").strip()
    admin_name = (data.get("admin_name") or "").strip()
    admin_username = (data.get("admin_username") or "").strip()
    admin_password = data.get("admin_password") or ""
    confirm_password = data.get("confirm_password") or ""

    if not school_name or not admin_name or not admin_username:
        return jsonify({"error": "School name, your name, and a username are all required."}), 400
    if admin_password != confirm_password:
        return jsonify({"error": "Passwords do not match."}), 400
    ok, msg = security.password_is_strong_enough(admin_password)
    if not ok:
        return jsonify({"error": msg}), 400
    if User.query.filter_by(username=admin_username).first():
        return jsonify({"error": "That username is already taken."}), 400

    admin = User(username=admin_username, name=admin_name, role="admin",
                 password_hash=generate_password_hash(admin_password))
    db.session.add(admin)
    db.session.commit()
    security.mark_setup_complete(DB_PATH, school_name=school_name, school_address=school_address)

    # Optional school logo, uploaded as part of the same one-time wizard.
    # Saved over the existing logo_main.png so every template that already
    # references that filename picks it up automatically — no template
    # changes needed. Resized so an oversized photo doesn't blow up the
    # layout; failures here are non-fatal, since the admin account itself
    # is already created and shouldn't be lost over a bad image file.
    logo_file = request.files.get("school_logo")
    if logo_file and logo_file.filename:
        try:
            _save_school_logo(logo_file)
        except Exception as e:
            app.logger.warning(f"Setup logo upload failed (non-fatal, admin account still created): {e}")

    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Forced password change — reached automatically (see
# _enforce_password_change) whenever the signed-in account is flagged as
# needing a new password: right after the setup wizard's admin account
# logs in for the first time isn't flagged (they just chose it), but any
# account an admin creates via Manage Users, or resets the password on,
# is flagged so the new password isn't something the admin now knows too.
# --------------------------------------------------------------------------
@app.route("/change-password")
@login_required
def change_password_page():
    if not security.must_change_password(DB_PATH, current_user.id):
        return redirect(url_for("index"))
    return render_template("change_password.html")


@app.route("/api/account/change-password", methods=["POST"])
@login_required
def change_own_password():
    data = request.get_json() or {}
    current_password = data.get("current_password") or ""
    new_password = data.get("new_password") or ""
    confirm_password = data.get("confirm_password") or ""

    if not check_password_hash(current_user.password_hash, current_password):
        return jsonify({"error": "Current password is incorrect."}), 400
    if new_password != confirm_password:
        return jsonify({"error": "New passwords do not match."}), 400
    ok, msg = security.password_is_strong_enough(new_password)
    if not ok:
        return jsonify({"error": msg}), 400

    current_user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    security.clear_must_change_password(DB_PATH, current_user.id)
    return jsonify({"ok": True})


@app.route("/")
@login_required
def index():
    return render_template("index.html", user=current_user)


def _send_stored(key, *, mimetype=None, download_name=None, expires_in=FILE_LINK_TTL_SECONDS):
    """
    Send a stored file to the browser, whichever backend holds it.
    Local disk: stream it from this server. R2: redirect to a short-lived
    presigned link so the bytes never pass through this server.
    Inline by default; a download_name makes the browser save it instead.
    A bad key (e.g. one containing "..") or a missing file is a 404.
    """
    try:
        if storage.remote:
            return redirect(storage.presigned_url(
                key, expires_in, content_type=mimetype, download_name=download_name), 302)
        path = storage.local_path(key)
    except ValueError:
        abort(404)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype=mimetype, as_attachment=bool(download_name),
                     download_name=download_name)


@app.route("/uploads/<path:subpath>")
@login_required
def serve_upload(subpath):
    """
    Serves QR codes, barcodes, and other generated files that live under
    the uploads/ folder — e.g. /uploads/qrcodes/online_5.png. There was
    previously no route for this at all, which is why QR code and barcode
    images appeared broken/blank in the browser even though the files
    were being generated correctly on disk.
    Requires login, same as everything else in the app, and only ever
    serves keys under uploads/ (storage rejects path traversal).

    Backups also live under uploads/ but hold every table, children's records
    included, so they are never served from here: admins get them through
    /api/backup/download. The check is on the normalised, lower-cased path
    because Windows file systems ignore case and treat "\\" as a separator.
    """
    folder = posixpath.normpath(subpath.replace("\\", "/")).lstrip("/").lower()
    if folder == "backups" or folder.startswith("backups/"):
        abort(404)
    # Member QR codes carry "MemberID|Name|Class", so a student may fetch
    # only their own, plus the digital items' QR codes (which only point at
    # an item). Staff and viewers see them all. 404 rather than 403, so the
    # answer does not reveal which member ids exist.
    if folder.startswith("qrcodes/") and current_user.role not in ("admin", "librarian", "viewer"):
        key = f"uploads/{subpath}"
        own = (current_user.member_id is not None
               and Member.query.filter_by(id=current_user.member_id, qr_path=key).first() is not None)
        if not own and OnlineBook.query.filter_by(qr_path=key).first() is None:
            abort(404)
    return _send_stored(f"uploads/{subpath}")


@app.route("/api/whoami")
@login_required
def whoami():
    return jsonify({"name": current_user.name, "role": current_user.role,
                     "username": current_user.username})


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.route("/api/dashboard")
@login_required
def dashboard():
    total_books = db.session.query(db.func.coalesce(db.func.sum(Book.copies), 0)).scalar()
    distinct_titles = Book.query.count()
    members = Member.query.count()
    active_issues = Issue.query.filter_by(status="Active").all()
    issued = len(active_issues)
    overdue = sum(1 for i in active_issues if i.computed_status() == "Overdue")
    lost_books = Book.query.filter_by(status="Lost").count()
    fine_due = sum(i.current_fine() for i in active_issues if i.computed_status() == "Overdue")
    fine_collected = db.session.query(db.func.coalesce(db.func.sum(FinePayment.amount), 0))\
        .filter_by(status="Paid").scalar()

    # subject-wise pie
    subject_counts = {}
    for b in Book.query.all():
        subject_counts[b.subject or "General"] = subject_counts.get(b.subject or "General", 0) + b.copies

    # monthly issues (last 6 months)
    monthly = {}
    for i in Issue.query.all():
        ym = i.issue_date[:7] if i.issue_date else "unknown"
        monthly[ym] = monthly.get(ym, 0) + 1

    # most borrowed
    borrow_counts = {}
    for i in Issue.query.all():
        borrow_counts[i.accession_no] = borrow_counts.get(i.accession_no, 0) + 1
    top_books = sorted(borrow_counts.items(), key=lambda x: -x[1])[:5]
    top_books_named = []
    for acc, cnt in top_books:
        b = Book.query.filter_by(accession_no=acc).first()
        top_books_named.append({"title": b.title if b else acc, "count": cnt})

    # Stock summary — the "how many present / issued / returned" picture a
    # Librarian actually thinks in terms of day to day.
    total_returns_ever = Issue.query.filter_by(status="Returned").count()
    available_now = max(0, int(total_books or 0) - issued - lost_books)

    return jsonify({
        "total_books": int(total_books or 0),
        "distinct_titles": distinct_titles,
        "members": members,
        "issued": issued,
        "overdue": overdue,
        "lost_books": lost_books,
        "available_now": available_now,
        "total_returns_ever": total_returns_ever,
        "fine_due": fine_due,
        "fine_collected": int(fine_collected or 0),
        "subject_counts": subject_counts,
        "monthly_issues": monthly,
        "top_books": top_books_named,
    })


# --------------------------------------------------------------------------
# Books (Catalog)
# --------------------------------------------------------------------------
@app.route("/api/books", methods=["GET"])
@login_required
def list_books():
    q = (request.args.get("q") or "").strip().lower()
    books = Book.query.all()
    if q:
        def matches(b):
            haystack = " ".join(filter(None, [
                b.title, b.author, b.isbn, b.accession_no, b.rack_no, b.subject
            ])).lower()
            return q in haystack
        books = [b for b in books if matches(b)]
    return jsonify([b.to_dict() for b in books])


def next_accession_no():
    count = Book.query.count() + 1
    while True:
        candidate = f"BK{count:04d}"
        if not Book.query.filter_by(accession_no=candidate).first():
            return candidate
        count += 1


# How the length check names a field; others are shown as the column name
# with spaces ("publisher" -> "Publisher").
_FIELD_LABELS = {
    "accession_no": "Accession number", "isbn": "ISBN", "class_name": "Class",
    "rack_no": "Rack number", "member_id": "Member ID", "class_dept": "Class/Dept",
    "parent_phone": "Parent's phone", "admission_no": "Admission number",
    "dob": "Date of birth", "lost_date": "Lost date", "username": "Username",
}


def _length_problem(model, values):
    """
    Checks text about to be stored against the model's VARCHAR lengths
    (models.py). Returns a readable message for the first value that is too
    long, or None. SQLite ignores declared lengths, so offline this never
    mattered; Postgres rejects the whole statement, which surfaced as a
    generic 500. `values` maps column names to what will be stored; None
    and names that are not columns are skipped.
    """
    columns = model.__table__.columns
    for name, value in values.items():
        if value is None or name not in columns:
            continue
        length = getattr(columns[name].type, "length", None)
        if length and len(str(value)) > length:
            label = _FIELD_LABELS.get(name, name.replace("_", " ").capitalize())
            return f"{label} is too long (at most {length} characters)."
    return None


def _whole_number(value, label, default=0):
    """
    Parses a count or amount from JSON. Returns (number, None), or
    (None, error message) when it is not a whole number from 0 to 1,000,000.
    A missing value gives `default`. Checked here because int() on junk is a
    500, and a huge number is a database range error (also a 500) on Postgres.
    """
    if value is None or value == "":
        return default, None
    problem = f"{label} must be a whole number between 0 and 1000000."
    if isinstance(value, bool):
        return None, problem
    if isinstance(value, float):
        if not value.is_integer():
            return None, problem
        value = int(value)
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None, problem
    if not 0 <= number <= 1_000_000:
        return None, problem
    return number, None


@app.route("/api/books", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def add_book():
    data = request.get_json()
    copies, err = _whole_number(data.get("copies"), "Copies", default=1)
    if err:
        return jsonify({"error": err}), 400
    acc = data.get("accession_no") or next_accession_no()
    problem = _length_problem(Book, {"accession_no": acc, **{
        f: data.get(f) for f in ("isbn", "title", "author", "publisher", "edition", "subject",
                                 "class_name", "rack_no")}})
    if problem:
        return jsonify({"error": problem}), 400
    book = Book(
        accession_no=acc, isbn=data.get("isbn"), title=data["title"], author=data.get("author"),
        publisher=data.get("publisher"), edition=data.get("edition"), subject=data.get("subject"),
        class_name=data.get("class_name"), rack_no=data.get("rack_no"),
        copies=copies, status="Available",
    )
    db.session.add(book)
    db.session.commit()
    generate_barcode_for_book(book)
    db.session.commit()
    return jsonify(book.to_dict()), 201


@app.route("/api/books/<int:book_id>", methods=["PUT"])
@login_required
@role_required("admin", "librarian")
def update_book(book_id):
    book = Book.query.get_or_404(book_id)
    data = request.get_json()
    new_copies = None
    if "copies" in data:
        new_copies, err = _whole_number(data["copies"], "Copies", default=1)
        if err:
            return jsonify({"error": err}), 400
    problem = _length_problem(Book, {f: data[f] for f in ("isbn", "title", "author", "publisher", "edition",
                                                          "subject", "class_name", "rack_no", "status")
                                     if f in data})
    if problem:
        return jsonify({"error": problem}), 400
    for field in ["isbn", "title", "author", "publisher", "edition", "subject",
                  "class_name", "rack_no"]:
        if field in data:
            setattr(book, field, data[field])
    if new_copies is not None:
        book.copies = new_copies
    if "status" in data:
        book.status = data["status"]
    db.session.commit()
    return jsonify(book.to_dict())


@app.route("/api/books/<int:book_id>", methods=["DELETE"])
@login_required
@role_required("admin", "librarian")
def delete_book(book_id):
    book = Book.query.get_or_404(book_id)
    if Issue.query.filter_by(accession_no=book.accession_no, status="Active").first():
        return jsonify({"error": "Cannot delete — a copy is currently issued."}), 400
    db.session.delete(book)
    db.session.commit()
    return jsonify({"ok": True})


def _barcode_key(accession_no):
    """
    Storage key for a book's barcode image. Accession numbers are typed by
    staff and could contain characters such as "/" that must not end up in a
    file name or storage key, so the name goes through secure_filename. That
    maps different numbers to the same name ("B#1", "B:1" and "B1" all become
    "B1"), and the later book would silently overwrite the earlier one's
    image. When the name had to change, a short hash of the real accession
    number is appended so distinct numbers keep distinct files; plain numbers
    keep the old <accession>.png name.
    """
    name = secure_filename(accession_no)
    if name != accession_no:
        digest = hashlib.sha1(accession_no.encode("utf-8")).hexdigest()[:8]
        name = f"{name}_{digest}"
    return f"uploads/barcodes/{name}.png"


def generate_barcode_for_book(book):
    try:
        import barcode
        from barcode.writer import ImageWriter
        if not secure_filename(book.accession_no):
            raise ValueError(f"accession number {book.accession_no!r} has no usable file name")
        code = barcode.get("code128", book.accession_no, writer=ImageWriter())
        buf = io.BytesIO()
        code.write(buf)  # PNG bytes in memory; we choose the storage key ourselves
        book.barcode_path = storage.save_bytes(
            buf.getvalue(), _barcode_key(book.accession_no), "image/png")
    except Exception as e:
        app.logger.warning(f"Barcode generation failed: {e}")


# --------------------------------------------------------------------------
# Members
# --------------------------------------------------------------------------
def next_member_id():
    count = Member.query.count() + 1
    while True:
        candidate = f"MSS-M-{count:04d}"
        if not Member.query.filter_by(member_id=candidate).first():
            return candidate
        count += 1


@app.route("/api/members", methods=["GET"])
@login_required
@role_required("admin", "librarian", "viewer")
def list_members():
    q = (request.args.get("q") or "").strip().lower()
    members = Member.query.all()
    if q:
        def matches(m):
            haystack = " ".join(filter(None, [m.name, m.member_id, m.class_dept, m.admission_no])).lower()
            return q in haystack
        members = [m for m in members if matches(m)]
    out = [m.to_dict() for m in members]
    # The codes themselves and the lock state go to admin/librarian only
    # (the ones who can act on them); viewers get the plain member data.
    if current_user.role in ("admin", "librarian"):
        locked = security.locked_accounts(DB_PATH)
        for m, row in zip(members, out):
            row["pin_setup_code"] = m.pin_setup_code
            row["signin_locked"] = m.type == "Student" and _student_account(m).lower() in locked
    return jsonify(out)


EMAIL_IN_USE_MESSAGE = "That email is already on another library card."


def _normalize_email(value):
    """Trimmed, lowercased school email, or None when blank."""
    if value is None:
        return None
    return str(value).strip().lower() or None


def _email_in_use(email, exclude_id=None):
    """True if another member already has this (already lowercased) email."""
    query = Member.query.filter(db.func.lower(Member.email) == email)
    if exclude_id is not None:
        query = query.filter(Member.id != exclude_id)
    return query.first() is not None


@app.route("/api/members", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def add_member():
    data = request.get_json()
    email = _normalize_email(data.get("email"))
    if email and len(email) > 254:
        return jsonify({"error": "That email address is too long."}), 400
    if email and _email_in_use(email):
        return jsonify({"error": EMAIL_IN_USE_MESSAGE}), 409
    mid = data.get("member_id") or next_member_id()
    problem = _length_problem(Member, {"member_id": mid, **{
        f: data.get(f) for f in ("name", "type", "class_dept", "phone", "parent_phone", "admission_no", "dob")}})
    if problem:
        return jsonify({"error": problem}), 400
    member = Member(
        member_id=mid, name=data["name"], type=data.get("type", "Student"),
        class_dept=data.get("class_dept"), phone=data.get("phone"),
        parent_phone=data.get("parent_phone"), admission_no=data.get("admission_no"),
        dob=data.get("dob"), email=email,
    )
    db.session.add(member)
    db.session.commit()
    generate_qr_for_member(member)
    db.session.commit()
    return jsonify(member.to_dict()), 201


@app.route("/api/members/<int:mid>", methods=["PUT"])
@login_required
@role_required("admin", "librarian")
def update_member(mid):
    member = Member.query.get_or_404(mid)
    data = request.get_json()
    problem = _length_problem(Member, {f: data[f] for f in ("name", "type", "class_dept", "phone", "parent_phone",
                                                            "admission_no", "dob") if f in data})
    if problem:
        return jsonify({"error": problem}), 400
    if "email" in data:
        email = _normalize_email(data["email"])
        if email != ((member.email or "").lower() or None):
            if email and len(email) > 254:
                return jsonify({"error": "That email address is too long."}), 400
            if email and _email_in_use(email, exclude_id=member.id):
                return jsonify({"error": EMAIL_IN_USE_MESSAGE}), 409
            member.email = email
            # The Microsoft account linked to this card was matched through
            # the old email; a different email means a different person may
            # be meant, so the link is dropped and re-forms on their next
            # Microsoft sign-in. Saving the same email leaves it alone.
            member.entra_oid = None
    for field in ["name", "type", "class_dept", "phone", "parent_phone", "admission_no", "dob"]:
        if field in data:
            setattr(member, field, data[field])
    db.session.commit()
    return jsonify(member.to_dict())


def _reset_student_pin(member):
    """
    Clears the student's PIN, gives the card a fresh set-up code and lifts
    any sign-in lock, so the student can create a new PIN as soon as the
    librarian hands over the code. Returns the code. The caller commits.
    """
    member.pin_hash = None
    member.pin_setup_code = _new_setup_code()
    security.clear_account_lock(DB_PATH, _student_account(member))
    return member.pin_setup_code


@app.route("/api/members/<int:mid>/reset-pin", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def reset_member_pin(mid):
    """
    Clears a student's PIN and issues a new set-up code — the "forgot my
    PIN" recovery a librarian would handle in person. Only someone holding
    that code (and the DOB) can create the new PIN, so do this only once
    you've verified who's asking, and give the code to the student directly.
    """
    member = Member.query.get_or_404(mid)
    if member.type != "Student":
        return jsonify({"error": "Only student accounts have a PIN."}), 400
    code = _reset_student_pin(member)
    db.session.commit()
    return jsonify({"ok": True, "pin_setup_code": code})


@app.route("/api/members/<int:mid>/pin-code", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def issue_member_pin_code(mid):
    """
    Issues (or replaces) the set-up code for a student who has no PIN yet.
    A student who already has one goes through Reset PIN instead, so a
    stray click here can never take a working PIN away.
    """
    member = Member.query.get_or_404(mid)
    if member.type != "Student":
        return jsonify({"error": "Only student accounts have a PIN."}), 400
    if member.pin_hash:
        return jsonify({"error": "This student already has a PIN. Use Reset PIN to replace it."}), 400
    code = _reset_student_pin(member)
    db.session.commit()
    return jsonify({"ok": True, "pin_setup_code": code})


@app.route("/api/members/pin-codes", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def issue_pin_codes_in_bulk():
    """
    Issues a set-up code to every student (optionally just one class) who
    has neither a PIN nor a code yet — what go-live day needs, when most
    cards have never been signed in to. Existing codes are left alone so a
    slip already handed out stays valid. The codes then show in the member
    list, which can be filtered by class and printed.
    """
    data = request.get_json(silent=True) or {}
    class_dept = (data.get("class_dept") or "").strip()
    query = Member.query.filter(Member.type == "Student", Member.pin_hash.is_(None),
                                Member.pin_setup_code.is_(None))
    if class_dept:
        query = query.filter(Member.class_dept == class_dept)
    issued = 0
    for member in query.all():
        member.pin_setup_code = _new_setup_code()
        issued += 1
    db.session.commit()
    return jsonify({"ok": True, "issued": issued})


@app.route("/api/members/<int:mid>/unlock", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def unlock_member_sign_in(mid):
    """Lifts the lock a student's card gets after too many failed sign-ins
    (see security.ACCOUNT_MAX_FAILURES); it does not lift on its own."""
    member = Member.query.get_or_404(mid)
    security.clear_account_lock(DB_PATH, _student_account(member))
    return jsonify({"ok": True})


@app.route("/api/members/<int:mid>", methods=["DELETE"])
@login_required
@role_required("admin", "librarian")
def delete_member(mid):
    member = Member.query.get_or_404(mid)
    if Issue.query.filter_by(member_id=member.member_id, status="Active").first():
        return jsonify({"error": "Cannot delete — member has an active loan."}), 400
    # Postgres enforces foreign keys (SQLite does not), so everything that
    # points at this member has to go, or be unlinked, before the member row.
    # A student's own sign-in account goes with them; any other kind of
    # account (a staff login that was linked to a member) is kept, just
    # unlinked. Loans, fines, lost-book and certificate records are library
    # history and are deliberately kept.
    User.query.filter_by(member_id=member.id, role="student").delete(synchronize_session=False)
    User.query.filter_by(member_id=member.id).update({"member_id": None}, synchronize_session=False)
    for model in (Bookmark, Favorite, ReadHistory, DigitalLibraryVisit, PinResetRequest):
        model.query.filter_by(member_id=member.member_id).delete(synchronize_session=False)
    qr_key = member.qr_path
    db.session.delete(member)
    db.session.commit()
    # Only now is it safe to drop the file: the record that named it is gone.
    storage.delete(qr_key)
    return jsonify({"ok": True})


def generate_qr_for_member(member):
    try:
        import qrcode
        payload = f"MemberID:{member.member_id}|Name:{member.name}|Class:{member.class_dept or ''}"
        img = qrcode.make(payload)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        member.qr_path = storage.save_bytes(
            buf.getvalue(), f"uploads/qrcodes/{member.member_id}.png", "image/png")
    except Exception as e:
        app.logger.warning(f"QR generation failed: {e}")


# --------------------------------------------------------------------------
# Issue / Return / Renewal
# --------------------------------------------------------------------------
@app.route("/api/issues", methods=["GET"])
@login_required
@role_required("admin", "librarian", "viewer")
def list_issues():
    status_filter = request.args.get("status")  # active | overdue | all
    issues = Issue.query.all()
    if status_filter == "active":
        issues = [i for i in issues if i.status == "Active"]
    elif status_filter == "overdue":
        issues = [i for i in issues if i.computed_status() == "Overdue"]
    return jsonify([i.to_dict() for i in issues])


@app.route("/api/issues", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def issue_book():
    data = request.get_json()
    acc = data["accession_no"]
    # JSON may carry the id as a number; Postgres will not compare that to
    # the text column (a 500), so always query with a clean string.
    member_id = str(data.get("member_id") or "").strip()
    book = Book.query.filter_by(accession_no=acc).first()
    member = Member.query.filter_by(member_id=member_id).first()
    if not book or not member:
        return jsonify({"error": "Book or member not found."}), 404
    if book.computed_status() != "Available":
        return jsonify({"error": "No copies available to issue."}), 400

    issue_date = data.get("issue_date") or today_iso()
    due_date = data.get("due_date") or (date.fromisoformat(issue_date) + timedelta(days=LOAN_DAYS)).isoformat()
    issue = Issue(accession_no=acc, member_id=member_id, issue_date=issue_date,
                   due_date=due_date, status="Active")
    db.session.add(issue)
    db.session.commit()
    return jsonify(issue.to_dict()), 201


@app.route("/api/issues/<int:issue_id>/return", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def return_book(issue_id):
    issue = Issue.query.get_or_404(issue_id)
    if issue.status != "Active":
        return jsonify({"error": "Already returned."}), 400
    issue.return_date = today_iso()
    issue.fine = issue.current_fine()
    issue.status = "Returned"
    if issue.fine > 0:
        db.session.add(FinePayment(issue_id=issue.id, member_id=issue.member_id,
                                    amount=issue.fine, status="Pending"))
    db.session.commit()
    return jsonify(issue.to_dict())


@app.route("/api/issues/<int:issue_id>/renew", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def renew_book(issue_id):
    issue = Issue.query.get_or_404(issue_id)
    if issue.status != "Active":
        return jsonify({"error": "Cannot renew a returned book."}), 400
    if (issue.renewal_count or 0) >= RENEWAL_LIMIT:
        return jsonify({
            "error": f"This loan has already been renewed {RENEWAL_LIMIT} time(s) — the maximum allowed. "
                     "Please return the book so it can be reissued if needed."
        }), 400
    extra_days = int((request.get_json(silent=True) or {}).get("extra_days", LOAN_DAYS))
    issue.due_date = (date.fromisoformat(issue.due_date) + timedelta(days=extra_days)).isoformat()
    issue.renewal_count = (issue.renewal_count or 0) + 1
    db.session.commit()
    return jsonify(issue.to_dict())


@app.route("/api/fines", methods=["GET"])
@login_required
@role_required("admin", "librarian", "viewer")
def list_fines():
    payments = FinePayment.query.all()
    return jsonify([{
        "id": p.id, "issue_id": p.issue_id, "member_id": p.member_id,
        "amount": p.amount, "paid_date": p.paid_date, "status": p.status
    } for p in payments])


@app.route("/api/fines/<int:fid>/pay", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def pay_fine(fid):
    payment = FinePayment.query.get_or_404(fid)
    payment.status = "Paid"
    payment.paid_date = today_iso()
    db.session.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Lost books / Stock verification / Periodicals / Visitors / Donations
# --------------------------------------------------------------------------
@app.route("/api/lost-books", methods=["GET", "POST"])
@login_required
def lost_books():
    # Read access to the registers is staff-only: the student/member role
    # must not be able to list other members' personal data or loan records.
    if current_user.role not in ("admin", "librarian", "viewer"):
        return jsonify({"error": "Forbidden — insufficient role"}), 403
    if request.method == "POST":
        if current_user.role not in ("admin", "librarian"):
            return jsonify({"error": "Forbidden — insufficient role"}), 403
        data = request.get_json()
        cost, err = _whole_number(data.get("cost"), "Cost", default=0)
        if err:
            return jsonify({"error": err}), 400
        problem = _length_problem(LostBook, {f: data.get(f) for f in ("accession_no", "member_id", "lost_date")})
        if problem:
            return jsonify({"error": problem}), 400
        record = LostBook(accession_no=data["accession_no"], member_id=data.get("member_id"),
                           lost_date=data.get("lost_date") or today_iso(),
                           cost=cost, remarks=data.get("remarks"))
        db.session.add(record)
        book = Book.query.filter_by(accession_no=data["accession_no"]).first()
        if book:
            book.status = "Lost"
        db.session.commit()
        return jsonify({"ok": True}), 201
    records = LostBook.query.all()
    return jsonify([{
        "id": r.id, "accession_no": r.accession_no, "member_id": r.member_id,
        "lost_date": r.lost_date, "cost": r.cost, "remarks": r.remarks
    } for r in records])


@app.route("/api/stock-check", methods=["GET", "POST"])
@login_required
@role_required("admin", "librarian")
def stock_check():
    if request.method == "POST":
        data = request.get_json()
        record = StockCheck(accession_no=data["accession_no"], check_date=today_iso(),
                             found_status=data["found_status"], remarks=data.get("remarks"))
        db.session.add(record)
        db.session.commit()
        return jsonify({"ok": True}), 201
    records = StockCheck.query.order_by(StockCheck.id.desc()).limit(200).all()
    return jsonify([{
        "id": r.id, "accession_no": r.accession_no, "check_date": r.check_date,
        "found_status": r.found_status, "remarks": r.remarks
    } for r in records])


@app.route("/api/periodicals", methods=["GET", "POST"])
@login_required
def periodicals():
    # Read access to the registers is staff-only: the student/member role
    # must not be able to list other members' personal data or loan records.
    if current_user.role not in ("admin", "librarian", "viewer"):
        return jsonify({"error": "Forbidden — insufficient role"}), 403
    if request.method == "POST":
        if current_user.role not in ("admin", "librarian"):
            return jsonify({"error": "Forbidden — insufficient role"}), 403
        data = request.get_json()
        record = Periodical(name=data["name"], type=data.get("type", "Newspaper"),
                             entry_date=data.get("entry_date") or today_iso(),
                             remarks=data.get("remarks"))
        db.session.add(record)
        db.session.commit()
        return jsonify({"ok": True}), 201
    records = Periodical.query.order_by(Periodical.id.desc()).limit(200).all()
    return jsonify([{
        "id": r.id, "name": r.name, "type": r.type,
        "entry_date": r.entry_date, "remarks": r.remarks
    } for r in records])


@app.route("/api/visitors", methods=["GET", "POST"])
@login_required
def visitors():
    # Read access to the registers is staff-only: the student/member role
    # must not be able to list other members' personal data or loan records.
    if current_user.role not in ("admin", "librarian", "viewer"):
        return jsonify({"error": "Forbidden — insufficient role"}), 403
    if request.method == "POST":
        if current_user.role not in ("admin", "librarian"):
            return jsonify({"error": "Forbidden — insufficient role"}), 403
        data = request.get_json()
        record = Visitor(name=data["name"], purpose=data.get("purpose"),
                          visit_time=data.get("visit_time") or datetime.now().isoformat(timespec="minutes"))
        db.session.add(record)
        db.session.commit()
        return jsonify({"ok": True}), 201
    records = Visitor.query.order_by(Visitor.id.desc()).limit(200).all()
    return jsonify([{
        "id": r.id, "name": r.name, "purpose": r.purpose,
        "visit_time": r.visit_time, "check_out_time": r.check_out_time,
    } for r in records])


@app.route("/api/visitors/<int:vid>/checkout", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def checkout_visitor(vid):
    record = Visitor.query.get_or_404(vid)
    record.check_out_time = datetime.now().isoformat(timespec="minutes")
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/donations", methods=["GET", "POST"])
@login_required
def donations():
    # Read access to the registers is staff-only: the student/member role
    # must not be able to list other members' personal data or loan records.
    if current_user.role not in ("admin", "librarian", "viewer"):
        return jsonify({"error": "Forbidden — insufficient role"}), 403
    if request.method == "POST":
        if current_user.role not in ("admin", "librarian"):
            return jsonify({"error": "Forbidden — insufficient role"}), 403
        data = request.get_json()
        record = Donation(donor_name=data["donor_name"], book_title=data["book_title"],
                           quantity=int(data.get("quantity", 1)),
                           donation_date=data.get("donation_date") or today_iso())
        db.session.add(record)
        db.session.commit()
        return jsonify({"ok": True}), 201
    records = Donation.query.order_by(Donation.id.desc()).limit(200).all()
    return jsonify([{
        "id": r.id, "donor_name": r.donor_name, "book_title": r.book_title,
        "quantity": r.quantity, "donation_date": r.donation_date
    } for r in records])


# --------------------------------------------------------------------------
# Digital Library — multi-format repository (PDF, EPUB, Word,
# PowerPoint, audio, video), Smart Search, OCR/content search, QR codes.
# View-only for PDFs; audio/video stream inline; office/ebook formats are
# downloaded to open (browsers can't render those natively).
# --------------------------------------------------------------------------

# extension -> (category, mimetype). Category drives size limits + how a
# file is served (inline preview vs. download).
DIGITAL_LIBRARY_TYPES = {
    ".pdf":  ("document", "application/pdf"),
    ".epub": ("ebook",    "application/epub+zip"),
    ".doc":  ("office",   "application/msword"),
    ".docx": ("office",   "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".ppt":  ("office",   "application/vnd.ms-powerpoint"),
    ".pptx": ("office",   "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ".mp4":  ("video",    "video/mp4"),
    ".webm": ("video",    "video/webm"),
    ".mp3":  ("audio",    "audio/mpeg"),
    ".wav":  ("audio",    "audio/wav"),
    ".m4a":  ("audio",    "audio/mp4"),
}
DIGITAL_LIBRARY_MAX_MB = {
    "document": 60, "ebook": 60, "office": 60, "video": 400, "audio": 150,
}


def _extract_content_text(path, ext):
    """
    Pulls searchable text out of a document so Smart/Content/OCR Search can
    find it later. Never raises — a file that can't be read for text is
    simply not full-text-searchable (title/author/subject still are).
    Returns (text, ocr_was_used).
    """
    text = ""
    try:
        if ext == ".pdf":
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                pages = pdf.pages[:300]
                text = "\n".join((p.extract_text() or "") for p in pages)
            if len(text.strip()) < 40:
                # Little/no embedded text usually means scanned page images —
                # try OCR if Tesseract + Poppler are installed on this PC.
                # If they aren't, we quietly skip rather than fail the upload.
                try:
                    import pytesseract
                    from pdf2image import convert_from_path
                    images = convert_from_path(path, first_page=1, last_page=15)
                    ocr_text = "\n".join(pytesseract.image_to_string(img) for img in images)
                    if len(ocr_text.strip()) > len(text.strip()):
                        return ocr_text, True
                except Exception as ocr_err:
                    app.logger.info(f"OCR skipped for {path}: {ocr_err}")
        elif ext == ".docx":
            import docx
            d = docx.Document(path)
            text = "\n".join(p.text for p in d.paragraphs)
        elif ext == ".pptx":
            from pptx import Presentation
            prs = Presentation(path)
            chunks = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text:
                        chunks.append(shape.text)
            text = "\n".join(chunks)
        # .doc / .ppt (legacy binary formats) and .epub are not extracted —
        # titles/author/subject remain searchable for those.
    except Exception as e:
        app.logger.warning(f"Content extraction failed for {path}: {e}")
    return text, False


def _generate_qr_for_online_book(book):
    try:
        import qrcode
        # Deliberately NOT using request.host_url here — if the Librarian is
        # browsing via 127.0.0.1/localhost on the server PC itself (the
        # normal case), a QR code built from that address would only ever
        # open on that same PC, never on a phone. In the cloud the public
        # site address (PUBLIC_BASE_URL) works from anywhere; offline,
        # get_lan_ip() resolves to an address other devices on the network
        # can reach.
        if PUBLIC_BASE_URL:
            url = f"{PUBLIC_BASE_URL}/api/online-books/{book.id}/view"
        else:
            url = f"http://{get_lan_ip()}:5000/api/online-books/{book.id}/view"
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        book.qr_path = storage.save_bytes(
            buf.getvalue(), f"uploads/qrcodes/online_{book.id}.png", "image/png")
    except Exception as e:
        app.logger.warning(f"QR generation failed for online book {book.id}: {e}")


def _smart_search_rank(items, q, field_weights):
    """
    Lightweight local relevance ranking (no internet/API needed): exact
    phrase matches score highest, then individual-word matches, weighted
    by which field they appear in (title > author/subject > full text).
    field_weights: list of (attr_name, weight).
    """
    q = (q or "").strip().lower()
    if not q:
        return items
    tokens = [t for t in q.split() if t]
    scored = []
    for item in items:
        score = 0
        for attr, weight in field_weights:
            text = (getattr(item, attr, "") or "").lower()
            if not text:
                continue
            if q in text:
                score += 10 * weight
            score += sum(2 * weight for t in tokens if t in text)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: -pair[0])
    return [item for _, item in scored]


def _convert_to_pdf_preview(source_path, ext, out_dir):
    """
    Converts a Word/PowerPoint file to PDF using LibreOffice running
    headless, so it can be read in-browser like a normal PDF instead of
    forcing a download every time. This is entirely optional — if
    LibreOffice isn't installed on this PC, this quietly does nothing and
    the file just stays download-only, exactly like before. No error is
    shown to the person uploading; this is a nice-to-have, not something
    that should ever block an upload.

    The PDF is written into out_dir (a temporary directory owned by the
    caller, which uploads it to storage and then deletes the directory).
    Returns the absolute path to the generated PDF, or None.
    """
    if ext not in (".doc", ".docx", ".ppt", ".pptx"):
        return None
    soffice = shutil.which("soffice") or shutil.which("soffice.exe")
    if not soffice:
        return None
    try:
        result = subprocess.run(
            [soffice, "--headless", "--norestore", "--convert-to", "pdf", "--outdir", out_dir, source_path],
            capture_output=True, timeout=90,
        )
        if result.returncode != 0:
            app.logger.warning(f"LibreOffice conversion failed for {source_path}: {result.stderr[:300]}")
            return None
        # LibreOffice names its output after the source file.
        generated = os.path.join(out_dir, os.path.splitext(os.path.basename(source_path))[0] + ".pdf")
        if os.path.exists(generated):
            return generated
    except Exception as e:
        app.logger.warning(f"LibreOffice conversion failed for {source_path}: {e}")
    return None



    """
    Lightweight local relevance ranking (no internet/API needed): exact
    phrase matches score highest, then individual-word matches, weighted
    by which field they appear in (title > author/subject > full text).
    field_weights: list of (attr_name, weight).
    """
    q = (q or "").strip().lower()
    if not q:
        return items
    tokens = [t for t in q.split() if t]
    scored = []
    for item in items:
        score = 0
        for attr, weight in field_weights:
            text = (getattr(item, attr, "") or "").lower()
            if not text:
                continue
            if q in text:
                score += 10 * weight
            score += sum(2 * weight for t in tokens if t in text)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: -pair[0])
    return [item for _, item in scored]


def _save_digital_upload(file, subject_hint=None, category_hint=None, title_hint=None):
    """
    Validates + stores one uploaded file, extracts text, returns an OnlineBook
    (uncommitted) or raises ValueError.

    The upload is first written to a throwaway temp directory, because text
    extraction and the LibreOffice conversion need a real file on disk — and
    in the cloud the app's own disk is not where files are meant to live.
    Only then are the original (and the PDF preview, if one was made) handed
    to storage. The temp directory is always removed afterwards. If storing
    the second file fails, the first is deleted again and the error
    propagates, so a failed upload never leaves an orphaned object behind.
    """
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in DIGITAL_LIBRARY_TYPES:
        allowed = ", ".join(sorted(DIGITAL_LIBRARY_TYPES))
        raise ValueError(f"'{file.filename}' — unsupported file type. Allowed: {allowed}")

    file_category, mime = DIGITAL_LIBRARY_TYPES[ext]
    # secure_filename drops non-ASCII letters, so a name such as
    # "తెలుగు పుస్తకం.pdf" collapses to just "pdf" (the dot goes too). Anything
    # that does not still end in the real extension gets a neutral name.
    safe_name = secure_filename(file.filename)
    if not safe_name or os.path.splitext(safe_name)[1].lower() != ext:
        safe_name = f"file{ext}"
    token = secrets.token_hex(6)
    stored_name = f"{token}_{safe_name}"
    saved_keys = []
    # The working copy lives next to uploads/ (not in the system temp
    # folder) so that, in local mode, save_file(move=True) can rename it into
    # place: a 400 MB video is then written once, and needs no free space on
    # the system drive. In R2 mode the file is uploaded and removed below.
    os.makedirs(_UPLOAD_WORK_DIR, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="smartlib-upload-", dir=_UPLOAD_WORK_DIR)
    try:
        source_path = os.path.join(tmp_dir, stored_name)
        file.save(source_path)

        size_kb = os.path.getsize(source_path) // 1024
        limit_mb = DIGITAL_LIBRARY_MAX_MB[file_category]
        if size_kb / 1024 > limit_mb:
            raise ValueError(f"'{file.filename}' is {size_kb/1024:.1f} MB — the limit for this file type is {limit_mb} MB.")

        content_text, ocr_used = _extract_content_text(source_path, ext)
        preview_dir = os.path.join(tmp_dir, "preview")
        os.makedirs(preview_dir)
        preview_source = _convert_to_pdf_preview(source_path, ext, preview_dir)

        # move=True: nothing reads either temp file after this, and the
        # finally below tolerates them being gone.
        rel_path = storage.save_file(source_path, f"uploads/pdfs/{stored_name}", mime, move=True)
        saved_keys.append(rel_path)
        preview_pdf_rel = None
        if preview_source:
            stem = os.path.splitext(safe_name)[0]
            preview_pdf_rel = storage.save_file(
                preview_source, f"uploads/pdfs/{token}_{stem}.pdf", "application/pdf", move=True)
            saved_keys.append(preview_pdf_rel)
    except BaseException:
        for key in saved_keys:
            storage.delete(key)
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    title = (title_hint or "").strip()
    if not title:
        # Prefer the name the uploader actually chose (keeps non-ASCII text
        # such as Telugu); safe_name is only the fallback.
        original = os.path.basename(file.filename.replace("\\", "/"))
        title = os.path.splitext(original)[0].replace("_", " ").replace("-", " ").strip()
        title = re.sub(r"\s+", " ", title).title()
        if not title:
            title = os.path.splitext(safe_name)[0].replace("_", " ").replace("-", " ").strip()
            title = re.sub(r"\s+", " ", title).title() or safe_name

    return OnlineBook(
        title=title, subject=(subject_hint or None), category=(category_hint or None), pdf_path=rel_path,
        file_type=ext.lstrip("."), file_size_kb=size_kb,
        content_text=content_text or None, ocr_applied=ocr_used,
        added_date=today_iso(), preview_pdf_path=preview_pdf_rel,
    )


def _delete_online_book_files(book, include_qr=True):
    """Removes a digital item's stored objects. Call only once the database
    no longer refers to them (i.e. after the commit)."""
    for key in (book.pdf_path, book.preview_pdf_path, book.qr_path if include_qr else None):
        storage.delete(key)


# --------------------------------------------------------------------------
# Books for my class — a digital item can be aimed at one school grade (1-12).
# --------------------------------------------------------------------------

_ROMAN_GRADES = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6,
                 "VII": 7, "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12}
# "7-A", "7A", "12 Science", "Class 10", "Std XII", "VII-B": an optional
# Class/Std/Standard prefix, then a leading Arabic number or Roman numeral.
# The Roman token must stand alone (so "Vision" is not Class V) and the
# Arabic one must not run into another digit (so "100" is not Class 10).
_GRADE_RE = re.compile(r"^\s*(?:(?:class|std|standard)\.?\s*)?(?:(\d{1,2})(?!\d)|([ivx]+)(?![a-z0-9]))", re.I)


def _grade_from_class(class_dept):
    """
    Turn a member's class text (free-form: "7-A", "VII", "Class 10", "Std XII")
    into a grade 1-12, or None when it doesn't start with one (e.g. "LKG",
    a staff department). None means "don't filter" to the caller.
    """
    m = _GRADE_RE.match(class_dept or "")
    if not m:
        return None
    grade = int(m.group(1)) if m.group(1) else _ROMAN_GRADES.get(m.group(2).upper())
    return grade if grade and 1 <= grade <= 12 else None


def _parse_grade_target(raw):
    """Form value -> grade 1-12, or None for blank ("All classes"). Raises ValueError otherwise."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        grade = int(raw)
    except ValueError:
        grade = 0
    if not 1 <= grade <= 12:
        raise ValueError("Class must be between 1 and 12.")
    return grade


@app.route("/api/online-books", methods=["GET"])
@login_required
def list_online_books():
    q = (request.args.get("q") or "").strip()
    subject_filter = (request.args.get("subject") or "").strip()
    category_filter = (request.args.get("category") or "").strip()
    books = OnlineBook.query.all()
    # A student sees books for their own class (plus untargeted ones) unless
    # they tick "Show books for all classes". Staff always see everything, and
    # so does a student whose class text doesn't name a grade.
    member = _current_student_member()
    if member and request.args.get("all_grades") != "1":
        grade = _grade_from_class(member.class_dept)
        if grade is not None:
            books = [b for b in books if b.grade_target is None or b.grade_target == grade]
    if subject_filter:
        books = [b for b in books if (b.subject or "") == subject_filter]
    if category_filter:
        books = [b for b in books if (b.category or "") == category_filter]
    if q:
        books = _smart_search_rank(
            books, q,
            [("title", 5), ("author", 3), ("subject", 3), ("category", 3), ("content_text", 1)],
        )
    else:
        books = sorted(books, key=lambda b: (b.title or "").lower())
    return jsonify([b.to_dict() for b in books])


def _download_name(title, file_type):
    """
    "<title>.<type>" for send_file(download_name=...). Local mode puts the
    name into a response header, and Flask raises on CR/LF in it, so control
    characters and the quote/backslash characters that break a quoted
    filename are removed. Non-ASCII text is kept.
    """
    cleaned = "".join(ch for ch in (title or "") if ord(ch) >= 32 and ch not in '"\\')
    return f"{cleaned.strip() or 'download'}.{file_type}"


def _online_book_target(book):
    """
    Which stored file the reader should get for a digital item, and how:
    returns (key, mimetype, inline, download_name). The auto-converted PDF
    preview wins when one exists; otherwise the original is served, inline
    for formats a browser can render and as a download for the rest (Word/
    PowerPoint). Shared by /view and /link so both always agree.
    """
    if book.preview_pdf_path:
        return book.preview_pdf_path, "application/pdf", True, None
    ext = "." + (book.file_type or "pdf")
    category, mimetype = DIGITAL_LIBRARY_TYPES.get(ext, ("other", "application/octet-stream"))
    inline = category in ("document", "video", "audio", "ebook")
    download_name = None if inline else _download_name(book.title, book.file_type)
    return book.pdf_path, mimetype, inline, download_name


@app.route("/api/online-books/<int:book_id>/view")
@login_required
def view_online_book(book_id):
    """
    Streams a digital item inline when the browser can render it natively
    (PDF / audio / video / EPUB via epub.js on the frontend). Word/
    PowerPoint files are served as their auto-converted PDF preview when
    one exists (see _convert_to_pdf_preview); if no preview was generated
    (LibreOffice not installed on this PC), they download instead, since
    browsers can't display those formats on their own.
    """
    book = OnlineBook.query.get_or_404(book_id)
    # The in-app reader re-requests the file (e.g. to renew an expiring video
    # link) with ?src=app; that is not the student opening the item again.
    if request.args.get("src") != "app":
        _student = _current_student_member()
        if _student:
            _log_read_history(_student, book_id)

    key, mimetype, inline, download_name = _online_book_target(book)

    response = _send_stored(key, mimetype=mimetype, download_name=download_name,
                            expires_in=LINK_TTL_SECONDS)
    # Locally these headers apply to the file itself; in R2 mode the response
    # is only a redirect, so they are harmless there (and keep the redirect,
    # whose URL is a time-limited credential, out of the browser cache).
    if inline:
        response.headers["Content-Disposition"] = "inline"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/online-books/<int:book_id>/link")
def online_book_link(book_id):
    """
    Gives the in-app reader a URL for a digital item, plus when it stops
    working. Locally that is the streaming /view route (no expiry). On R2 it
    is a presigned link valid for LINK_TTL_SECONDS; the reader warns the
    student shortly before then and calls this again with ?renew=1 to get a
    fresh one, which needs a valid login, so a signed-out reader cannot keep
    going. A renewal is not a new "open", so it is not logged.

    Deliberately not @login_required: that would answer a signed-out reader
    with the login page's HTML redirect, which the reader cannot act on. It
    gets a JSON 401 it can turn into a "please sign in again" prompt.
    """
    if not current_user.is_authenticated:
        return jsonify({"error": "Please sign in again to keep reading.",
                        "login_required": True}), 401
    book = db.session.get(OnlineBook, book_id)
    if book is None:
        return jsonify({"error": "Item not found."}), 404

    student = _current_student_member()
    if student and request.args.get("renew") != "1":
        _log_read_history(student, book_id)

    key, mimetype, _inline, download_name = _online_book_target(book)
    if storage.remote:
        try:
            url = storage.presigned_url(key, LINK_TTL_SECONDS, content_type=mimetype,
                                        download_name=download_name)
        except ValueError:
            # The stored key is unusable (e.g. contains ".."): same answer as
            # for a missing item, as /view gives.
            return jsonify({"error": "Item not found."}), 404
        expires_in = LINK_TTL_SECONDS
        warn_at = max(expires_in - 300, expires_in // 2)
    else:
        url = f"/api/online-books/{book_id}/view?src=app"
        expires_in = warn_at = None

    progress = 0
    if student:
        row = ReadHistory.query.filter_by(member_id=student.member_id,
                                          online_book_id=book_id).first()
        progress = (row.progress_seconds or 0) if row else 0

    response = jsonify({
        "url": url,
        "expires_in": expires_in,
        "warn_at": warn_at,
        # The reader renders what it is handed, so report the preview's type.
        "file_type": "pdf" if book.preview_pdf_path else book.file_type,
        "progress_seconds": progress,
    })
    # The URL is a time-limited credential: keep it out of caches.
    response.headers["Cache-Control"] = "no-store"
    return response


MAX_PROGRESS_SECONDS = 86400


@app.route("/api/online-books/<int:book_id>/progress", methods=["POST"])
def save_online_book_progress(book_id):
    """
    Remembers how far a student got in a video/audio item so the reader can
    resume there. Students only; staff have no read history. Same JSON 401
    as /link for a signed-out reader.
    """
    if not current_user.is_authenticated:
        return jsonify({"error": "Please sign in again to keep reading.",
                        "login_required": True}), 401
    student = _current_student_member()
    if not student:
        return jsonify({"error": "Only students have a reading history."}), 403

    body = request.get_json(silent=True)
    seconds = body.get("seconds") if isinstance(body, dict) else None
    # bool is an int subclass in Python, but true/false is not a position.
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
        return jsonify({"error": "seconds must be a whole number, 0 or more."}), 400
    # A day is far beyond any real item; the cap also keeps the value inside
    # the 32-bit INTEGER column, which Postgres enforces (SQLite does not).
    if seconds > MAX_PROGRESS_SECONDS:
        return jsonify({"error": f"seconds must be {MAX_PROGRESS_SECONDS} or less."}), 400
    if db.session.get(OnlineBook, book_id) is None:
        return jsonify({"error": "Item not found."}), 404

    row = ReadHistory.query.filter_by(member_id=student.member_id,
                                      online_book_id=book_id).first()
    if row is None:
        # Normally /link already created the row, but an item opened some
        # other way (a QR code, say) has none yet. Create it as a first open
        # rather than failing and losing the student's place.
        today = today_iso()
        row = ReadHistory(member_id=student.member_id, online_book_id=book_id,
                          first_opened=today, last_opened=today, open_count=1)
        db.session.add(row)
    row.progress_seconds = seconds
    try:
        db.session.commit()
    except IntegrityError:
        # A concurrent request created the row first; update that one.
        db.session.rollback()
        row = ReadHistory.query.filter_by(member_id=student.member_id,
                                          online_book_id=book_id).first()
        row.progress_seconds = seconds
        db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/online-books/<int:book_id>/download-original")
@login_required
@role_required("admin", "librarian")
def download_original_online_book(book_id):
    """Staff-only: get the original Word/PowerPoint file (not the PDF preview), e.g. to edit it."""
    book = OnlineBook.query.get_or_404(book_id)
    category, mimetype = DIGITAL_LIBRARY_TYPES.get("." + (book.file_type or ""), ("other", "application/octet-stream"))
    return _send_stored(book.pdf_path, mimetype=mimetype,
                        download_name=_download_name(book.title, book.file_type), expires_in=LINK_TTL_SECONDS)


@app.route("/api/online-books", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def add_online_book():
    """Add one digital item. Expects multipart/form-data: title, author, subject, category, file."""
    title = (request.form.get("title") or "").strip()
    author = (request.form.get("author") or "").strip()
    subject = (request.form.get("subject") or "").strip()
    category = (request.form.get("category") or "").strip()
    try:
        grade_target = _parse_grade_target(request.form.get("grade_target"))
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400

    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"error": "Please choose a file to upload."}), 400
    # Checked before the upload is stored, so a long title costs no upload.
    problem = _length_problem(OnlineBook, {"title": title, "author": author, "subject": subject,
                                           "category": category})
    if problem:
        return jsonify({"error": problem}), 400

    try:
        book = _save_digital_upload(request.files["file"], subject_hint=subject, category_hint=category, title_hint=title)
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        app.logger.exception("Digital Library upload failed while saving the file")
        return jsonify({"error": "Could not save the file. Please try again."}), 500

    try:
        if author:
            book.author = author
        book.grade_target = grade_target
        db.session.add(book)
        db.session.flush()  # need book.id before generating its QR code
        _generate_qr_for_online_book(book)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        # The database has no record of this item, so nothing will ever point
        # at the objects _save_digital_upload stored: delete them now rather
        # than leave orphans in the bucket. The keys are still on the (now
        # detached) object after the rollback.
        _delete_online_book_files(book)
        app.logger.exception("Digital Library upload failed while saving to the database")
        return jsonify({"error": "Could not save the file. Please try again."}), 500
    return jsonify(book.to_dict()), 201


@app.route("/api/online-books/<int:book_id>", methods=["PUT"])
@login_required
@role_required("admin", "librarian")
def edit_online_book(book_id):
    """Update title/author/subject, and optionally replace the file itself."""
    book = OnlineBook.query.get_or_404(book_id)
    problem = _length_problem(OnlineBook, {f: (request.form.get(f) or "").strip()
                                           for f in ("title", "author", "subject", "category")})
    if problem:
        return jsonify({"error": problem}), 400

    # Validate before touching the record. A form that omits grade_target
    # leaves the class as it was; an empty value means "All classes".
    if "grade_target" in request.form:
        try:
            book.grade_target = _parse_grade_target(request.form["grade_target"])
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400

    title = (request.form.get("title") or "").strip()
    if title:
        book.title = title
    book.author = (request.form.get("author") or "").strip() or None
    book.subject = (request.form.get("subject") or "").strip() or None
    book.category = (request.form.get("category") or "").strip() or None

    new_file = request.files.get("file")
    if new_file and new_file.filename:
        try:
            replacement = _save_digital_upload(new_file, subject_hint=book.subject, category_hint=book.category, title_hint=book.title)
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        except Exception as e:
            app.logger.exception("Digital Library edit failed while saving the replacement file")
            return jsonify({"error": "Could not save the file. Please try again."}), 500

        old_keys = [book.pdf_path, book.preview_pdf_path]
        book.pdf_path = replacement.pdf_path
        book.file_type = replacement.file_type
        book.file_size_kb = replacement.file_size_kb
        book.content_text = replacement.content_text
        book.ocr_applied = replacement.ocr_applied
        book.preview_pdf_path = replacement.preview_pdf_path
        _generate_qr_for_online_book(book)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        if new_file and new_file.filename:
            # The record still points at the old files, so the replacement's
            # objects are the orphans. (The QR key is shared by old and new.)
            _delete_online_book_files(replacement, include_qr=False)
        raise
    # Only now is it safe to drop the old files: the database no longer
    # refers to them. (Deleting first and failing to commit would leave a
    # record pointing at nothing.)
    if new_file and new_file.filename:
        for key in old_keys:
            if key not in (book.pdf_path, book.preview_pdf_path):
                storage.delete(key)
    return jsonify(book.to_dict())


@app.route("/api/online-books/<int:book_id>", methods=["DELETE"])
@login_required
@role_required("admin")
def delete_online_book(book_id):
    book = OnlineBook.query.get_or_404(book_id)
    # Copy the keys first: once the row is deleted the book object is detached.
    keys = (book.pdf_path, book.preview_pdf_path, book.qr_path)
    # Students' bookmarks, favourites and reading history point at the book;
    # Postgres refuses to delete it while they exist (SQLite does not check).
    for model in (Bookmark, Favorite, ReadHistory):
        model.query.filter_by(online_book_id=book.id).delete(synchronize_session=False)
    db.session.delete(book)
    db.session.commit()
    for key in keys:
        storage.delete(key)
    return jsonify({"ok": True})


@app.route("/api/online-books/bulk-import", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def bulk_import_online_books():
    """
    Add many files at once (any mix of supported formats). Titles are
    derived from filenames; fix up individually afterwards with Edit.
    """
    files = request.files.getlist("files")
    if not files or not any(f.filename for f in files):
        return jsonify({"error": "Please choose one or more files."}), 400

    shared_subject = (request.form.get("subject") or "").strip() or None
    shared_category = (request.form.get("category") or "").strip() or None
    try:
        shared_grade = _parse_grade_target(request.form.get("grade_target"))
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    created, skipped = 0, []
    created_books = []
    for f in files:
        if not f.filename:
            continue
        book = None
        try:
            book = _save_digital_upload(f, subject_hint=shared_subject, category_hint=shared_category)
            book.grade_target = shared_grade
            # A SAVEPOINT per file: if this file's insert or QR step fails on
            # Postgres the transaction would otherwise be aborted and every
            # later file (and the final commit) would fail with it. On error
            # the savepoint rolls back just this file's rows.
            with db.session.begin_nested():
                db.session.add(book)
                db.session.flush()
                _generate_qr_for_online_book(book)
            created += 1
            created_books.append(book)
        except Exception as file_err:
            # _save_digital_upload cleans up after itself when storing fails;
            # if we got past it, the objects it stored are now orphans.
            if book is not None:
                _delete_online_book_files(book)
            if isinstance(file_err, ValueError):
                skipped.append(str(file_err))
            else:
                app.logger.warning("Bulk import: %s could not be saved: %s", f.filename, file_err)
                skipped.append(f"{f.filename} (could not be saved)")

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        # Nothing was recorded, so every object stored for this request is an
        # orphan; remove them all before reporting the failure.
        for book in created_books:
            _delete_online_book_files(book)
        app.logger.exception("Digital Library bulk import failed while saving to the database")
        return jsonify({"error": "Could not save the files. Please try again."}), 500
    return jsonify({"ok": True, "created": created, "skipped": skipped, "total": len(files)})


@app.route("/api/online-books/regenerate-qr-codes", methods=["POST"])
@login_required
@role_required("admin")
def regenerate_all_qr_codes():
    """
    Rebuilds the QR code for every Digital Library item using the
    server's current network address. Use this once after updating to
    this version if QR codes generated earlier don't open on a phone —
    those were built using whatever address was in the browser bar at
    the time (often 127.0.0.1/localhost, since that's what the Librarian
    sees when working directly on the server PC), which only ever works
    on that same PC. New items get a working QR code automatically from
    now on; this fixes ones added before this update.
    """
    books = OnlineBook.query.all()
    for book in books:
        _generate_qr_for_online_book(book)
    db.session.commit()
    return jsonify({"ok": True, "updated": len(books)})


# --------------------------------------------------------------------------
# Student Engagement — My Library (bookmarks/favorites/history/recs),
# related-content suggestions on search, points & digital certificates.
# --------------------------------------------------------------------------

def _current_student_member():
    """Returns the Member row for the logged-in student, or None if the current user isn't a student."""
    if current_user.role != "student" or not current_user.member_id:
        return None
    return Member.query.get(current_user.member_id)


def _compute_points(member_id):
    """
    A simple, transparent points formula — no hidden weighting, easy to
    explain to a student who asks "how do I get more points":
      +10 for each physical book issued
      +5  for each physical book returned on time (no fine)
      +5  for each distinct digital item read (once ever, not per re-read)
      +2  for each distinct day the student visited the Digital Library

    Reopening something already read earns nothing further — see
    _log_read_history for how that's enforced at the database level, not
    just by convention. (An earlier version also added a small bonus for
    revisiting the same item several times; that was removed because it
    made the total drift upward just from reopening things, which ran
    against the "read it once, get the points once" rule this school
    wants.)
    """
    issues = Issue.query.filter_by(member_id=member_id).all()
    issued_points = len(issues) * 10
    ontime_points = sum(5 for i in issues if i.status == "Returned" and (i.fine or 0) == 0)

    distinct_read_points = ReadHistory.query.filter_by(member_id=member_id).count() * 5

    visit_points = DigitalLibraryVisit.query.filter_by(member_id=member_id).count() * 2

    return issued_points + ontime_points + distinct_read_points + visit_points


@app.route("/api/student/visit-digital-library", methods=["POST"])
@login_required
def visit_digital_library():
    """Called once when a student opens the Digital Library tab — awards a small, real point bonus for the first visit of the day (not repeated on every click within the same day)."""
    member = _current_student_member()
    if not member:
        return jsonify({"ok": True, "newly_awarded": False})
    today = today_iso()
    existing = DigitalLibraryVisit.query.filter_by(member_id=member.member_id, visit_date=today).first()
    if existing:
        return jsonify({"ok": True, "newly_awarded": False, "points": _compute_points(member.member_id)})
    db.session.add(DigitalLibraryVisit(member_id=member.member_id, visit_date=today))
    db.session.commit()
    return jsonify({"ok": True, "newly_awarded": True, "points": _compute_points(member.member_id)})


@app.route("/api/student/my-library")
@login_required
def student_my_library():
    """Everything for the student-facing 'My Library' panel: current loans, history, bookmarks, favorites, recently read, and recommendations."""
    member = _current_student_member()
    if not member:
        return jsonify({"error": "Only student accounts have a My Library page."}), 403

    issues = Issue.query.filter_by(member_id=member.member_id).order_by(Issue.id.desc()).all()
    my_books = [i.to_dict() for i in issues if i.status != "Returned"]
    history = [i.to_dict() for i in issues]

    bookmarks = Bookmark.query.filter_by(member_id=member.member_id).order_by(Bookmark.id.desc()).all()
    favorites = Favorite.query.filter_by(member_id=member.member_id).order_by(Favorite.id.desc()).all()
    recents = ReadHistory.query.filter_by(member_id=member.member_id).order_by(ReadHistory.last_opened.desc()).limit(12).all()

    def _book_or_none(book_id):
        b = OnlineBook.query.get(book_id)
        return b.to_dict() if b else None

    bookmark_books = [b for b in (_book_or_none(x.online_book_id) for x in bookmarks) if b]
    favorite_books = [b for b in (_book_or_none(x.online_book_id) for x in favorites) if b]
    recent_books = [b for b in (_book_or_none(x.online_book_id) for x in recents) if b]

    # Recommendations: subject overlap with whatever the student has favorited/read,
    # excluding things already in that set. Falls back to newest additions if the
    # student has no activity yet (nothing to base a recommendation on).
    seen_ids = {x.online_book_id for x in bookmarks + favorites + recents}
    interest_subjects = {b["subject"] for b in favorite_books + recent_books if b.get("subject")}
    if interest_subjects:
        recs_q = OnlineBook.query.filter(OnlineBook.subject.in_(interest_subjects), ~OnlineBook.id.in_(seen_ids or [0]))
    else:
        recs_q = OnlineBook.query.filter(~OnlineBook.id.in_(seen_ids or [0]))
    recommendations = [b.to_dict() for b in recs_q.order_by(OnlineBook.id.desc()).limit(8).all()]

    return jsonify({
        "my_books": my_books,
        "history": history,
        "bookmarks": bookmark_books,
        "favorites": favorite_books,
        "recently_read": recent_books,
        "recommendations": recommendations,
        "points": _compute_points(member.member_id),
    })


@app.route("/api/online-books/<int:book_id>/bookmark", methods=["POST", "DELETE"])
@login_required
def toggle_bookmark(book_id):
    member = _current_student_member()
    if not member:
        return jsonify({"error": "Only student accounts can bookmark items."}), 403
    existing = Bookmark.query.filter_by(member_id=member.member_id, online_book_id=book_id).first()
    if request.method == "DELETE" or existing:
        if existing:
            db.session.delete(existing)
            db.session.commit()
        return jsonify({"ok": True, "bookmarked": False})
    # Without this a missing item reaches the foreign key and is a 500 on Postgres.
    if db.session.get(OnlineBook, book_id) is None:
        return jsonify({"error": "Item not found."}), 404
    db.session.add(Bookmark(member_id=member.member_id, online_book_id=book_id, added_date=today_iso()))
    db.session.commit()
    return jsonify({"ok": True, "bookmarked": True})


@app.route("/api/online-books/<int:book_id>/favorite", methods=["POST", "DELETE"])
@login_required
def toggle_favorite(book_id):
    member = _current_student_member()
    if not member:
        return jsonify({"error": "Only student accounts can favorite items."}), 403
    existing = Favorite.query.filter_by(member_id=member.member_id, online_book_id=book_id).first()
    if request.method == "DELETE" or existing:
        if existing:
            db.session.delete(existing)
            db.session.commit()
        return jsonify({"ok": True, "favorited": False})
    # Without this a missing item reaches the foreign key and is a 500 on Postgres.
    if db.session.get(OnlineBook, book_id) is None:
        return jsonify({"error": "Item not found."}), 404
    db.session.add(Favorite(member_id=member.member_id, online_book_id=book_id, added_date=today_iso()))
    db.session.commit()
    return jsonify({"ok": True, "favorited": True})


@app.route("/api/student/points")
@login_required
def student_points():
    """Just the current point total — used by the frontend to detect a fresh increase right after reading something, so it can show a small celebratory toast."""
    member = _current_student_member()
    if not member:
        return jsonify({"points": 0})
    return jsonify({"points": _compute_points(member.member_id)})


@app.route("/api/student/my-flags")
@login_required
def student_my_flags():
    """Which digital item IDs the current student has bookmarked/favorited — lets the Digital Library grid show the right icon state on every card without a separate call per card."""
    member = _current_student_member()
    if not member:
        return jsonify({"bookmarked": [], "favorited": []})
    bm = [b.online_book_id for b in Bookmark.query.filter_by(member_id=member.member_id).all()]
    fav = [f.online_book_id for f in Favorite.query.filter_by(member_id=member.member_id).all()]
    return jsonify({"bookmarked": bm, "favorited": fav})


def _log_read_history(member, book_id):
    """
    Records that this student opened this digital item — powers Recently
    Read, and (via ReadHistory's row count) points. Points are awarded
    for a book exactly once, ever, the first time a student opens it —
    never for reopening it again, whether that's a genuine re-read
    tomorrow or the browser re-fetching the same file five times in one
    sitting. Never raises; a logging failure should never break reading.

    Point-safety: the /view route below is hit by the browser every time
    it (re-)requests the file — including a plain page refresh, and
    especially a <video>/<audio> tag, which the browser fetches via
    multiple separate HTTP Range requests just to buffer/seek one
    playback, not one request per view. Earlier versions of this
    function checked "has this been opened today?" before deciding
    whether to count it — but that check-then-write wasn't atomic, so
    several of those near-simultaneous Range requests could each see "no,
    not opened today yet" before any of them had saved, and each create
    an extra row — permanently inflating points, since points are based
    on ReadHistory's row count. The fix here is two-part: first, the
    database itself now enforces at most one ReadHistory row per
    (student, item) — see the unique constraint on the model — so it's
    no longer possible for concurrent requests to create duplicates no
    matter how they're timed. Second, points now come only from that one
    row ever existing, not from open_count, which is kept purely as a
    "how many times has this been opened" statistic for admin dashboards
    and no longer feeds into the points total at all.
    """
    try:
        today = today_iso()
        db.session.add(ReadHistory(member_id=member.member_id, online_book_id=book_id,
                                    first_opened=today, last_opened=today, open_count=1))
        db.session.commit()
    except IntegrityError:
        # The unique constraint fired — a row already exists (either from
        # a genuinely earlier read, or from a concurrent request that won
        # this exact race). Either way, no new points: just bump the
        # existing row's stats so Recently Read and the open-count
        # statistic stay accurate.
        db.session.rollback()
        try:
            row = ReadHistory.query.filter_by(member_id=member.member_id, online_book_id=book_id).first()
            if row:
                row.open_count = (row.open_count or 1) + 1
                row.last_opened = today
                db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"Could not update read history after race: {e}")
    except Exception as e:
        db.session.rollback()
        app.logger.warning(f"Could not log read history: {e}")


@app.route("/api/student/search-suggestions")
@login_required
def student_search_suggestions():
    """
    Related-content suggestions for a search term, across the Catalog AND
    Digital Library, grouped by type — e.g. searching "python basics"
    surfaces matching e-books, videos, and physical books together, plus
    the specific paragraph inside a document where the term was found.
    This is local keyword/subject matching, not a cloud AI model — it
    works fully offline, which matters since this school runs LAN-only
    with no internet.
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"books": [], "digital": [], "snippets": []})
    tokens = [t.lower() for t in q.split() if t]

    # Physical catalog matches (title/author/subject)
    catalog_matches = Book.query.filter(
        db.or_(*[Book.title.ilike(f"%{t}%") for t in tokens],
               *[Book.subject.ilike(f"%{t}%") for t in tokens])
    ).limit(6).all()

    # Digital Library matches, ranked the same way Smart Search ranks them
    all_digital = OnlineBook.query.all()
    digital_matches = _smart_search_rank(
        all_digital, q, [("title", 5), ("author", 3), ("subject", 3), ("content_text", 1)]
    )[:8]

    # Snippets: the actual sentence/paragraph inside a document where the
    # term was found, so a student can see it's relevant before opening it.
    snippets = []
    for b in digital_matches:
        if not b.content_text:
            continue
        text = b.content_text
        lower = text.lower()
        idx = -1
        for t in tokens:
            idx = lower.find(t)
            if idx != -1:
                break
        if idx == -1:
            continue
        start = max(0, idx - 80)
        end = min(len(text), idx + 160)
        snippet = text[start:end].strip().replace("\n", " ")
        snippets.append({"book_id": b.id, "title": b.title, "snippet": f"...{snippet}..."})

    return jsonify({
        "books": [b.to_dict() for b in catalog_matches],
        "digital": [b.to_dict() for b in digital_matches],
        "snippets": snippets[:5],
    })


@app.route("/api/leaderboard")
@login_required
def leaderboard():
    """Ranks all students by points — powers the Achievements/leaderboard view and Admin's certificate-awarding screen."""
    students = Member.query.filter_by(type="Student").all()
    ranked = sorted(
        ({"member_id": m.member_id, "name": m.name, "class_dept": m.class_dept, "points": _compute_points(m.member_id)}
         for m in students),
        key=lambda r: -r["points"]
    )
    return jsonify(ranked[:50])


@app.route("/api/certificates", methods=["GET", "POST"])
@login_required
def certificates():
    if request.method == "POST":
        if current_user.role not in ("admin", "librarian"):
            return jsonify({"error": "Forbidden — insufficient role"}), 403
        data = request.get_json()
        member_id = data.get("member_id")
        title = (data.get("title") or "").strip()
        if not member_id or not title:
            return jsonify({"error": "A student and a certificate title are required."}), 400
        cert = Certificate(member_id=member_id, title=title, awarded_date=today_iso(), issued_by=current_user.name)
        db.session.add(cert)
        db.session.commit()
        return jsonify({"id": cert.id, "ok": True}), 201

    # Students only ever see their own; staff can see everyone's.
    if current_user.role == "student":
        member = _current_student_member()
        rows = Certificate.query.filter_by(member_id=member.member_id).order_by(Certificate.id.desc()).all() if member else []
    else:
        rows = Certificate.query.order_by(Certificate.id.desc()).limit(200).all()
    return jsonify([{"id": c.id, "member_id": c.member_id, "title": c.title,
                     "awarded_date": c.awarded_date, "issued_by": c.issued_by} for c in rows])


@app.route("/api/certificates/<int:cert_id>/download")
@login_required
def download_certificate(cert_id):
    cert = Certificate.query.get_or_404(cert_id)
    member = Member.query.filter_by(member_id=cert.member_id).first()
    if current_user.role == "student":
        my_member = _current_student_member()
        if not my_member or my_member.member_id != cert.member_id:
            return jsonify({"error": "You can only download your own certificates."}), 403

    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas as pdf_canvas

    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=landscape(A4))
    width, height = landscape(A4)

    navy = colors.HexColor("#12294F")
    brass = colors.HexColor("#B8860B")

    c.setStrokeColor(brass)
    c.setLineWidth(6)
    c.rect(0.5 * inch, 0.5 * inch, width - 1 * inch, height - 1 * inch)
    c.setLineWidth(1.5)
    c.rect(0.65 * inch, 0.65 * inch, width - 1.3 * inch, height - 1.3 * inch)

    # School logos, top-left and top-right — same two images used in the
    # web app's masthead, so the certificate matches the school's branding.
    logo_main_path, logo_product_path = _logo_paths()
    logo_h = 0.85 * inch
    try:
        if os.path.exists(logo_main_path):
            c.drawImage(logo_main_path, 0.9 * inch, height - 1.55 * inch,
                        width=logo_h * 1.6, height=logo_h, preserveAspectRatio=True,
                        anchor='sw', mask='auto')
    except Exception as e:
        app.logger.warning(f"Certificate: could not draw main logo: {e}")
    try:
        if os.path.exists(logo_product_path):
            c.drawImage(logo_product_path, width - 0.9 * inch - logo_h, height - 1.55 * inch,
                        width=logo_h, height=logo_h, preserveAspectRatio=True,
                        anchor='sw', mask='auto')
    except Exception as e:
        app.logger.warning(f"Certificate: could not draw product logo: {e}")

    school_name = security.get_school_name(DB_PATH)
    school_address = security.get_school_address(DB_PATH)

    # School name — the most prominent line in the header. Font size is
    # capped to fit within the space between the two corner logos rather
    # than a fixed size, so a long school name (this one is 50+
    # characters) can't run into or under the logos — it steps down
    # instead of overflowing.
    safe_width = width - 2 * (0.9 * inch + logo_h * 1.6 + 0.35 * inch)
    name_text = school_name.upper()
    name_size = 20
    while name_size > 13 and c.stringWidth(name_text, "Helvetica-Bold", name_size) > safe_width:
        name_size -= 1
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", name_size)
    c.drawCentredString(width / 2, height - 1.25 * inch, name_text)

    if school_address:
        c.setFont("Helvetica", 10)
        c.setFillColor(colors.HexColor("#555555"))
        c.drawCentredString(width / 2, height - 1.48 * inch, school_address)

    # A thin gold rule separates the school identity from the product
    # identity below it, so the two don't visually run together.
    c.setStrokeColor(brass)
    c.setLineWidth(0.75)
    c.line(width / 2 - 1.3 * inch, height - 1.62 * inch, width / 2 + 1.3 * inch, height - 1.62 * inch)

    # Library/product name — highlighted as its own line, larger and in
    # the brand gold, with the descriptor beneath it in a smaller weight.
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(brass)
    c.drawCentredString(width / 2, height - 1.85 * inch, PRODUCT_NAME.upper())
    c.setFont("Helvetica", 9)
    c.setFillColor(navy)
    c.drawCentredString(width / 2, height - 2.03 * inch, PRODUCT_DESCRIPTOR.upper())

    c.setFont("Helvetica-Bold", 28)
    c.setFillColor(navy)
    c.drawCentredString(width / 2, height - 2.4 * inch, "Certificate of Achievement")

    c.setFont("Helvetica", 14)
    c.drawCentredString(width / 2, height - 3.1 * inch, "This certificate is proudly presented to")

    c.setFont("Helvetica-Bold", 26)
    c.setFillColor(brass)
    student_name = member.name if member else cert.member_id
    c.drawCentredString(width / 2, height - 3.8 * inch, student_name)

    c.setFont("Helvetica", 15)
    c.setFillColor(navy)
    c.drawCentredString(width / 2, height - 4.4 * inch, f"for being recognized as")
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(width / 2, height - 4.85 * inch, cert.title)

    c.setFont("Helvetica", 11)
    c.setFillColor(colors.HexColor("#666666"))
    c.drawCentredString(width / 2, 1.3 * inch, f"Awarded on {cert.awarded_date}")
    if cert.issued_by:
        c.drawCentredString(width / 2, 1.05 * inch, f"— {cert.issued_by}, {PRODUCT_NAME}")

    c.showPage()
    c.save()
    buf.seek(0)
    response = send_file(buf, mimetype="application/pdf", as_attachment=False,
                          download_name=f"certificate_{cert.id}.pdf")
    return response


# --------------------------------------------------------------------------
# Users / Role management
# --------------------------------------------------------------------------
@app.route("/api/users", methods=["GET"])
@login_required
@role_required("admin")
def list_users():
    users = User.query.filter(User.role != "student").all()
    return jsonify([{"id": u.id, "username": u.username, "name": u.name, "role": u.role, "phone": u.phone} for u in users])


@app.route("/api/users", methods=["POST"])
@login_required
@role_required("admin")
def add_user():
    data = request.get_json()
    problem = _length_problem(User, {"username": data.get("username"), "name": data.get("name"),
                                     "role": data.get("role"), "phone": (data.get("phone") or "").strip()})
    if problem:
        return jsonify({"error": problem}), 400
    if User.query.filter_by(username=data["username"]).first():
        return jsonify({"error": "Username already taken."}), 400
    user = User(username=data["username"], name=data["name"], role=data.get("role", "librarian"),
                phone=(data.get("phone") or "").strip() or None,
                password_hash=generate_password_hash(data["password"]))
    db.session.add(user)
    db.session.commit()
    # The admin set this password, not the person who'll actually use the
    # account — flag it so they're required to pick their own on first login.
    security.set_must_change_password(DB_PATH, user.id, True)
    return jsonify({"ok": True}), 201


@app.route("/api/users/<int:uid>", methods=["PUT"])
@login_required
@role_required("admin")
def edit_user(uid):
    """
    Update a staff account's name, mobile number, and/or role, and
    optionally reset their password. Username is not editable here
    (it's how they log in and other records may reference it) — delete
    and recreate the account if it genuinely needs a new username.
    """
    user = User.query.get_or_404(uid)
    data = request.get_json() or {}
    problem = _length_problem(User, {"name": (data.get("name") or "").strip(), "role": data.get("role"),
                                     "phone": (data.get("phone") or "").strip()})
    if problem:
        return jsonify({"error": problem}), 400

    new_name = (data.get("name") or "").strip()
    if new_name:
        user.name = new_name

    if "phone" in data:
        user.phone = (data.get("phone") or "").strip() or None

    new_role = (data.get("role") or "").strip()
    if new_role and new_role != user.role:
        if user.role == "admin" and new_role != "admin" and User.query.filter_by(role="admin").count() <= 1:
            return jsonify({"error": "At least one Admin account must remain — change another admin first."}), 400
        user.role = new_role

    new_password = data.get("password") or ""
    if new_password:
        ok, msg = security.password_is_strong_enough(new_password)
        if not ok:
            return jsonify({"error": msg}), 400
        user.password_hash = generate_password_hash(new_password)
        # Same reasoning as add_user(): an admin-chosen reset password
        # must be replaced by the account holder before it's used further.
        security.set_must_change_password(DB_PATH, user.id, True)

    db.session.commit()
    return jsonify({"ok": True, "id": user.id, "username": user.username, "name": user.name,
                     "role": user.role, "phone": user.phone})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@login_required
@role_required("admin")
def delete_user(uid):
    user = User.query.get_or_404(uid)
    if user.role == "admin" and User.query.filter_by(role="admin").count() <= 1:
        return jsonify({"error": "At least one Admin account must remain."}), 400
    db.session.delete(user)
    db.session.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Reports (PDF) — reportlab
# --------------------------------------------------------------------------
def _draw_report_letterhead(canvas_obj, doc_obj):
    """
    Draws the same letterhead (both logos, school name, product name) on
    every page of a report — called automatically by SimpleDocTemplate
    for each page via onFirstPage/onLaterPages, so a multi-page report
    (e.g. a full member list) stays branded on every sheet, not just
    the first.
    """
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4

    width, height = A4
    navy = colors.HexColor("#15356E")
    brass = colors.HexColor("#C99A2E")

    def fit_size(text, font, max_size, min_size, max_width):
        size = max_size
        while size > min_size and canvas_obj.stringWidth(text, font, size) > max_width:
            size -= 1
        return size

    logo_main_path, logo_product_path = _logo_paths()
    logo_h = 0.5 * inch
    logo_w = logo_h * 1.3
    canvas_obj.saveState()
    try:
        if os.path.exists(logo_main_path):
            canvas_obj.drawImage(logo_main_path, 0.5 * inch, height - 0.95 * inch,
                                  width=logo_w, height=logo_h, preserveAspectRatio=True,
                                  anchor='sw', mask='auto')
    except Exception as e:
        app.logger.warning(f"Report letterhead: could not draw main logo: {e}")
    try:
        if os.path.exists(logo_product_path):
            canvas_obj.drawImage(logo_product_path, width - 0.5 * inch - logo_h, height - 0.95 * inch,
                                  width=logo_h, height=logo_h, preserveAspectRatio=True,
                                  anchor='sw', mask='auto')
    except Exception as e:
        app.logger.warning(f"Report letterhead: could not draw product logo: {e}")

    # Everything in this vertical band (down to the logos' bottom edge)
    # must stay clear of the logos horizontally — the school name and
    # address are both width-capped for that reason. The longer product
    # name/descriptor line is placed *below* the logos instead of trying
    # to squeeze between them, since on a portrait page there usually
    # isn't enough clear width for a long descriptor at that height.
    school_name = security.get_school_name(DB_PATH)
    school_address = security.get_school_address(DB_PATH)
    safe_width = width - 2 * (0.5 * inch + logo_w + 0.3 * inch)

    name_text = school_name.upper()
    name_size = fit_size(name_text, "Helvetica-Bold", 16, 10, safe_width)
    canvas_obj.setFillColor(navy)
    canvas_obj.setFont("Helvetica-Bold", name_size)
    canvas_obj.drawCentredString(width / 2, height - 0.52 * inch, name_text)

    if school_address:
        addr_size = fit_size(school_address, "Helvetica", 8, 6, safe_width)
        canvas_obj.setFont("Helvetica", addr_size)
        canvas_obj.setFillColor(colors.HexColor("#666666"))
        canvas_obj.drawCentredString(width / 2, height - 0.68 * inch, school_address)

    # Product name — given its own clear band below the logos (rather
    # than squeezed beside them) so there's always genuine breathing
    # room, regardless of logo shape or descriptor length.
    product_line = f"{PRODUCT_NAME.upper()}  —  {PRODUCT_DESCRIPTOR.upper()}"
    product_size = fit_size(product_line, "Helvetica-Bold", 10, 7, width - 1.0 * inch)
    canvas_obj.setFont("Helvetica-Bold", product_size)
    canvas_obj.setFillColor(brass)
    canvas_obj.drawCentredString(width / 2, height - 1.18 * inch, product_line)

    # Gold accent rule under the full letterhead, separating it from the report body.
    canvas_obj.setStrokeColor(brass)
    canvas_obj.setLineWidth(1.2)
    canvas_obj.line(0.5 * inch, height - 1.30 * inch, width - 0.5 * inch, height - 1.30 * inch)

    # Footer — who generated it, when, and the page number, on every page.
    canvas_obj.setFont("Helvetica", 7.5)
    canvas_obj.setFillColor(colors.HexColor("#888888"))
    generated_by = current_user.name if current_user.is_authenticated else "—"
    canvas_obj.drawString(0.5 * inch, 0.45 * inch,
                           f"Generated by {generated_by} on {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    canvas_obj.drawRightString(width - 0.5 * inch, 0.45 * inch, f"Page {doc_obj.page}")
    canvas_obj.setStrokeColor(colors.HexColor("#CCCCCC"))
    canvas_obj.setLineWidth(0.5)
    canvas_obj.line(0.5 * inch, 0.62 * inch, width - 0.5 * inch, 0.62 * inch)
    canvas_obj.restoreState()


def _pdf_table_response(title, headers, rows, filename):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=1.55 * inch, bottomMargin=0.85 * inch,
                             leftMargin=0.5 * inch, rightMargin=0.5 * inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Heading1"], textColor=colors.HexColor("#15356E"),
        fontSize=17, leading=20, spaceAfter=2, alignment=1,
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle", parent=styles["Normal"], textColor=colors.HexColor("#888888"),
        fontSize=9, alignment=1, spaceAfter=16,
    )
    record_word = "record" if len(rows) == 1 else "records"
    elements = [
        Paragraph(title, title_style),
        Paragraph(f"As of {datetime.now().strftime('%d %B %Y, %I:%M %p')}  •  {len(rows)} {record_word}", subtitle_style),
    ]
    data = [headers] + rows
    table = Table(data, repeatRows=1, hAlign="CENTER")
    table.setStyle(TableStyle([
        # Header row — brand navy background, bold white text, sized up
        # from the body rows so column names read clearly at a glance,
        # with a gold accent rule underneath to separate it from the data.
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#15356E")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9.5),
        ("TOPPADDING", (0, 0), (-1, 0), 9),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 9),
        ("LEFTPADDING", (0, 0), (-1, 0), 8),
        ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.HexColor("#C99A2E")),
        # Body rows — comfortable padding (wider than the old cramped
        # default) so the table reads like a report, not a raw data dump.
        ("FONTSIZE", (0, 1), (-1, -1), 8.5),
        ("TOPPADDING", (0, 1), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
        ("LEFTPADDING", (0, 1), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9D9D9")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F4F8")]),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(table)
    doc.build(elements, onFirstPage=_draw_report_letterhead, onLaterPages=_draw_report_letterhead)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=filename)


@app.route("/api/reports/issue-register")
@login_required
@role_required("admin", "librarian", "viewer")
def report_issue_register():
    issues = Issue.query.all()
    rows = [[i.accession_no, i.member_id, i.issue_date, i.due_date,
             i.return_date or "-", i.computed_status()] for i in issues]
    return _pdf_table_response("Issue Register", ["Accession No", "Member", "Issued", "Due", "Returned", "Status"],
                                rows, "issue_register.pdf")


@app.route("/api/reports/overdue")
@login_required
@role_required("admin", "librarian", "viewer")
def report_overdue():
    issues = [i for i in Issue.query.filter_by(status="Active").all() if i.computed_status() == "Overdue"]
    rows = [[i.accession_no, i.member_id, i.due_date, str(i.current_fine())] for i in issues]
    return _pdf_table_response("Overdue Books", ["Accession No", "Member", "Due Date", "Fine (Rs.)"],
                                rows, "overdue.pdf")


@app.route("/api/reports/fine-register")
@login_required
@role_required("admin", "librarian", "viewer")
def report_fine_register():
    payments = FinePayment.query.all()
    rows = [[p.member_id, str(p.amount), p.status, p.paid_date or "-"] for p in payments]
    return _pdf_table_response("Fine Register", ["Member", "Amount (Rs.)", "Status", "Paid Date"],
                                rows, "fine_register.pdf")


@app.route("/api/reports/member-list")
@login_required
@role_required("admin", "librarian", "viewer")
def report_member_list():
    members = Member.query.all()
    rows = [[m.member_id, m.name, m.type, m.class_dept or "-"] for m in members]
    return _pdf_table_response("Member List", ["Member ID", "Name", "Type", "Class/Dept"],
                                rows, "member_list.pdf")


@app.route("/api/reports/stock-report")
@login_required
@role_required("admin", "librarian", "viewer")
def report_stock():
    books = Book.query.all()
    rows = [[b.accession_no, b.title, b.computed_status(), str(b.copies)] for b in books]
    return _pdf_table_response("Stock Report", ["Accession No", "Title", "Status", "Copies"],
                                rows, "stock_report.pdf")


@app.route("/api/books/duplicate-titles")
@login_required
@role_required("admin", "librarian")
def duplicate_titles():
    """
    Groups catalog entries by exact title text and returns any title that
    appears on more than one Accession Number. Read-only — this is a
    review tool, not an auto-merge. A repeated title isn't necessarily a
    mistake (a new batch of the same book often gets its own accession
    number rather than bumping an old entry's copy count), so the
    decision to merge or leave alone is left to a human.
    """
    from sqlalchemy import func
    dupe_titles = (
        db.session.query(Book.title)
        .group_by(Book.title)
        .having(func.count(Book.id) > 1)
        .all()
    )
    groups = []
    for (title,) in dupe_titles:
        entries = Book.query.filter_by(title=title).order_by(Book.accession_no).all()
        groups.append({
            "title": title,
            "count": len(entries),
            "total_copies": sum(b.copies or 1 for b in entries),
            "entries": [{"accession_no": b.accession_no, "author": b.author,
                         "rack_no": b.rack_no, "copies": b.copies} for b in entries],
        })
    groups.sort(key=lambda g: -g["count"])
    return jsonify(groups)


# --------------------------------------------------------------------------
# Excel import / export — pandas + openpyxl
# --------------------------------------------------------------------------
@app.route("/api/export/<entity>")
@login_required
@role_required("admin", "librarian", "viewer")
def export_excel(entity):
    import pandas as pd
    if entity == "books":
        rows = [b.to_dict() for b in Book.query.all()]
    elif entity == "members":
        rows = [m.to_dict() for m in Member.query.all()]
    elif entity == "issues":
        rows = [i.to_dict() for i in Issue.query.all()]
    else:
        return jsonify({"error": "Unknown entity"}), 400
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{entity}.xlsx",
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/api/import/books", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def import_books():
    import pandas as pd

    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"error": "No file was received. Please choose an Excel file and try again."}), 400

    file = request.files["file"]
    try:
        excel = pd.ExcelFile(file)
    except Exception as e:
        return jsonify({"error": f"Could not read this file as an Excel workbook: {e}"}), 400

    try:
        # --------------------------------------------------------------
        # The school's registers arrive in different shapes — a sheet
        # already named/shaped for import ("Import_Books" with
        # accession_no/title/... headers), or a raw accession register
        # (e.g. "Book Code", "Accession Num", "Category", "Rack",
        # "Publishers Name.", "Quantity"). Every sheet is scored against
        # known column-name aliases and the best-matching sheet is used
        # automatically.
        # --------------------------------------------------------------
        best_sheet, best_map, best_score = None, {}, -1
        for sheet_name in excel.sheet_names:
            try:
                preview = excel.parse(sheet_name, nrows=0)
            except Exception:
                continue
            colmap = _map_import_columns(preview.columns)
            score = len(colmap) + (5 if "title" in colmap else 0) + (3 if "accession_no" in colmap else 0)
            if score > best_score:
                best_sheet, best_map, best_score = sheet_name, colmap, score

        if best_sheet is None or "title" not in best_map:
            return jsonify({
                "error": "Couldn't find a sheet with recognisable book columns "
                         "(need at least a Title column, e.g. 'title' or 'Title')."
            }), 400

        df = excel.parse(best_sheet)
        total_rows = len(df)
        created, skipped_duplicate, skipped_blank, skipped_error = 0, 0, 0, 0
        skipped_too_long = 0
        first_error = None

        for row_num, row in df.iterrows():
            try:
                title = _import_cell(row, best_map, "title")
                if not title:
                    skipped_blank += 1
                    continue

                acc_raw = _import_cell(row, best_map, "accession_no")
                acc = _clean_accession(acc_raw) if acc_raw else next_accession_no()
                if Book.query.filter_by(accession_no=acc).first():
                    skipped_duplicate += 1
                    continue

                copies_raw = _import_cell(row, best_map, "copies")
                try:
                    copies = int(float(copies_raw)) if copies_raw else 1
                except (TypeError, ValueError):
                    copies = 1

                values = {"accession_no": acc, "title": title,
                          **{f: _import_cell(row, best_map, f) for f in (
                              "isbn", "author", "publisher", "edition", "subject", "class_name", "rack_no")}}
                # Skipped and counted, never truncated: a cut-off accession
                # number or title would be wrong data that looks right.
                problem = _length_problem(Book, values)
                if problem:
                    skipped_too_long += 1
                    if first_error is None:
                        first_error = f"row {row_num + 2}: {problem}"
                    continue
                book = Book(
                    accession_no=acc,
                    isbn=_import_cell(row, best_map, "isbn"),
                    title=title,
                    author=_import_cell(row, best_map, "author"),
                    publisher=_import_cell(row, best_map, "publisher"),
                    edition=_import_cell(row, best_map, "edition"),
                    subject=_import_cell(row, best_map, "subject"),
                    class_name=_import_cell(row, best_map, "class_name"),
                    rack_no=_import_cell(row, best_map, "rack_no"),
                    copies=max(1, copies),
                    status="Available",
                )
                # A SAVEPOINT per row: a plain db.session.rollback() would also
                # discard every earlier row of this import (while `created`
                # kept counting them), and on Postgres a failed statement
                # aborts the whole transaction. The savepoint undoes only
                # this row.
                with db.session.begin_nested():
                    db.session.add(book)
                    db.session.flush()
                    generate_barcode_for_book(book)
                created += 1
            except Exception as row_err:
                # one bad row (a stray formula cell, an odd data type, etc.)
                # should never abort the other 1,000+ good rows in the file
                skipped_error += 1
                app.logger.warning("Book import: row %s could not be saved: %s", row_num + 2, row_err)
                if first_error is None:
                    # Never echo the exception: on Postgres it carries the SQL
                    # statement and its parameters.
                    first_error = f"row {row_num + 2}: could not be saved"

        db.session.commit()
        response = {
            "ok": True,
            "sheet_used": best_sheet,
            "total_rows": total_rows,
            "created": created,
            "skipped_duplicate": skipped_duplicate,
            "skipped_blank": skipped_blank,
            "skipped_too_long": skipped_too_long,
            "skipped_error": skipped_error,
        }
        if first_error:
            response["first_error"] = first_error
        return jsonify(response)
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Book import failed")
        return jsonify({"error": "Import failed. Please check the file and try again."}), 500


@app.route("/api/import/members", methods=["POST"])
@login_required
@role_required("admin", "librarian")
def import_members():
    """
    Bulk-add students/staff from an Excel admission register. Auto-detects
    the right sheet/columns the same way book import does, so whatever
    header wording the school's real register uses (AdmissionNo, rollno,
    DOB, class, section, Mobile, etc.) gets matched automatically.
    Existing members are matched by Admission Number and skipped (not
    overwritten) — re-uploading the same register is always safe.
    """
    import pandas as pd

    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"error": "No file was received. Please choose an Excel file and try again."}), 400

    file = request.files["file"]
    try:
        excel = pd.ExcelFile(file)
    except Exception as e:
        return jsonify({"error": f"Could not read this file as an Excel workbook: {e}"}), 400

    try:
        best_sheet, best_map, best_score = None, {}, -1
        for sheet_name in excel.sheet_names:
            try:
                preview = excel.parse(sheet_name, nrows=0)
            except Exception:
                continue
            colmap = _map_import_columns(preview.columns, MEMBER_IMPORT_COLUMN_ALIASES)
            score = len(colmap) + (5 if "name" in colmap else 0) + (3 if "admission_no" in colmap else 0)
            if score > best_score:
                best_sheet, best_map, best_score = sheet_name, colmap, score

        if best_sheet is None or "name" not in best_map:
            return jsonify({
                "error": "Couldn't find a sheet with recognisable student columns "
                         "(need at least a Name column, e.g. 'Name' or 'Student Name')."
            }), 400

        df = excel.parse(best_sheet)
        total_rows = len(df)
        created, skipped_duplicate, skipped_blank, skipped_error = 0, 0, 0, 0
        skipped_duplicate_email = 0
        skipped_too_long = 0
        first_error = None

        for row_num, row in df.iterrows():
            try:
                name = _import_cell(row, best_map, "name")
                if not name:
                    skipped_blank += 1
                    continue

                admission_no = _import_cell(row, best_map, "admission_no")
                if admission_no and Member.query.filter_by(admission_no=admission_no).first():
                    skipped_duplicate += 1
                    continue

                # A school email can sit on only one library card (it is how
                # a Microsoft sign-in finds the student), so a row whose email
                # another card already has is skipped rather than imported.
                email = _normalize_email(_import_cell(row, best_map, "email"))
                if email and (len(email) > 254 or _email_in_use(email)):
                    skipped_duplicate_email += 1
                    continue

                class_val = _import_cell(row, best_map, "class_name")
                section_val = _import_cell(row, best_map, "section")
                class_dept = f"{class_val}-{section_val}" if class_val and section_val else (class_val or section_val or None)

                dob_iso = _parse_dob_to_iso(_import_cell(row, best_map, "dob"))

                phone = _import_cell(row, best_map, "phone")
                parent_phone = _import_cell(row, best_map, "parent_phone")

                member_id = _import_cell(row, best_map, "member_id") or next_member_id()
                if Member.query.filter_by(member_id=member_id).first():
                    member_id = next_member_id()

                # Skipped and counted, never truncated (see import_books).
                problem = _length_problem(Member, {
                    "member_id": member_id, "name": name, "class_dept": class_dept, "phone": phone,
                    "parent_phone": parent_phone, "admission_no": admission_no})
                if problem:
                    skipped_too_long += 1
                    if first_error is None:
                        first_error = f"row {row_num + 2}: {problem}"
                    continue
                member = Member(
                    member_id=member_id, name=name, type="Student",
                    class_dept=class_dept, phone=phone or None, parent_phone=parent_phone or None,
                    admission_no=admission_no or None, dob=dob_iso, email=email,
                )
                # A SAVEPOINT per row. A plain db.session.rollback() here would
                # also discard every row already flushed by this import (while
                # `created` kept counting them), and on Postgres a failed
                # statement aborts the whole transaction. The savepoint undoes
                # only this row.
                with db.session.begin_nested():
                    db.session.add(member)
                    db.session.flush()
                    generate_qr_for_member(member)
                created += 1
            except Exception as row_err:
                skipped_error += 1
                app.logger.warning("Member import: row %s could not be saved: %s", row_num + 2, row_err)
                if first_error is None:
                    # Never echo the exception: on Postgres it carries the SQL
                    # statement and its parameters (other people's data).
                    first_error = f"row {row_num + 2}: could not be saved"

        db.session.commit()
        response = {
            "ok": True,
            "sheet_used": best_sheet,
            "total_rows": total_rows,
            "created": created,
            "skipped_duplicate": skipped_duplicate,
            "skipped_duplicate_email": skipped_duplicate_email,
            "skipped_blank": skipped_blank,
            "skipped_too_long": skipped_too_long,
            "skipped_error": skipped_error,
        }
        if first_error:
            response["first_error"] = first_error
        return jsonify(response)
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Member import failed")
        return jsonify({"error": "Import failed. Please check the file and try again."}), 500


# Recognised column-name variants per Book field, used to auto-detect the
# right sheet/columns whatever the register's original header wording was.
IMPORT_COLUMN_ALIASES = {
    "accession_no": ["accession_no", "accession no", "accession num", "accession number", "acc no", "acc. no"],
    "isbn": ["isbn"],
    "title": ["title", "book title"],
    "author": ["author", "author name"],
    "publisher": ["publisher", "publishers name", "publishers name.", "publisher name"],
    "edition": ["edition", "book edition"],
    "subject": ["subject", "category"],
    "class_name": ["class_name", "class", "class name"],
    "rack_no": ["rack_no", "rack", "rack no", "rack number"],
    "copies": ["copies", "quantity", "qty", "no of copies", "no. of copies"],
}

# Recognised column-name variants for a student/member register import —
# the school's real admission registers use headers like "AdmissionNo",
# "rollno", "DOB", "class", "section", "Mobile".
MEMBER_IMPORT_COLUMN_ALIASES = {
    "member_id": ["member_id", "member id"],
    "admission_no": ["admissionno", "admission_no", "admission no", "admission number", "adm no", "adm. no"],
    "roll_no": ["rollno", "roll_no", "roll no", "roll number"],
    "name": ["name", "student name", "full name", "studentname"],
    "dob": ["dob", "date of birth", "d.o.b", "d.o.b."],
    "class_name": ["class", "class_name", "class name", "std", "grade"],
    "section": ["section", "sec"],
    "phone": ["mobile", "phone", "mobile no", "mobile number", "contact", "contact no", "contact number"],
    "parent_phone": ["parent phone", "parent mobile", "father mobile", "guardian phone", "parent contact"],
    "email": ["email", "email id", "email address", "school email", "mail id"],
}


def _parse_dob_to_iso(text):
    """
    Accepts whatever date format a school register happens to use
    ("20 Apr 2012", "20-04-2012", "20/04/2012", or already-ISO
    "2012-04-20") and returns ISO yyyy-mm-dd, or None if unparseable.
    Never raises — a stray bad date shouldn't abort the whole import.
    """
    if not text:
        return None
    text = str(text).strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _normalize_header(text):
    return str(text).strip().lower().rstrip(".")


def _map_import_columns(columns, aliases=None):
    """Match a sheet's actual column headers against a column-alias dict (defaults to the book one)."""
    aliases = aliases if aliases is not None else IMPORT_COLUMN_ALIASES
    normalized = {_normalize_header(c): c for c in columns}
    mapping = {}
    for field, alias_list in aliases.items():
        for alias in alias_list:
            if alias in normalized:
                mapping[field] = normalized[alias]
                break
    return mapping


def _import_cell(row, colmap, field, default=""):
    import pandas as pd
    col = colmap.get(field)
    if col is None:
        return default
    val = row.get(col)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    if isinstance(val, float) and val.is_integer():
        val = int(val)
    text = str(val).strip()
    return text if text.lower() != "nan" else default


def _clean_accession(value):
    text = str(value).strip()
    if text.endswith(".0") and text.replace(".0", "").isdigit():
        text = text[:-2]
    return text


AUTO_ACCESSION_PATTERN = re.compile(r"^BK\d+$", re.IGNORECASE)


def _is_blank_value(value):
    return value is None or str(value).strip() in ("", "None", "nan")


def _book_looks_incomplete(book):
    """
    Flags a catalog entry as a leftover from a failed/interrupted import:
    either it has no title at all, or its accession number matches the
    auto-generated BK#### pattern *and* it has no author either (a real
    book that happens to have a BK-style accession number but a real
    author is left alone).
    """
    if _is_blank_value(book.title):
        return True
    if AUTO_ACCESSION_PATTERN.match(book.accession_no or "") and _is_blank_value(book.author):
        return True
    return False


@app.route("/api/books/incomplete-count")
@login_required
@role_required("admin", "librarian")
def incomplete_books_count():
    all_books = Book.query.all()
    incomplete = [b for b in all_books if _book_looks_incomplete(b)]
    return jsonify({"total_books": len(all_books), "incomplete": len(incomplete)})


@app.route("/api/books/purge-incomplete", methods=["DELETE"])
@login_required
@role_required("admin")
def purge_incomplete_books():
    all_books = Book.query.all()
    incomplete = [b for b in all_books if _book_looks_incomplete(b)]

    blocked, removable = [], []
    for b in incomplete:
        has_history = Issue.query.filter_by(accession_no=b.accession_no).first() is not None
        (blocked if has_history else removable).append(b)

    for b in removable:
        # delete() ignores missing files and bad keys, so cleanup never fails the request.
        storage.delete(b.barcode_path or _barcode_key(b.accession_no))
        db.session.delete(b)
    db.session.commit()

    return jsonify({
        "ok": True,
        "removed": len(removable),
        "skipped_has_history": len(blocked),
        "skipped_accession_numbers": [b.accession_no for b in blocked],
    })


# --------------------------------------------------------------------------
# Backup / Restore
# --------------------------------------------------------------------------
BACKUP_PREFIX = "uploads/backups/"

# Backup names look like backup_2026_10_28_141500.json, or the same with an
# auto_ prefix for scheduler-made ones, and .db for SQLite copies. The stamp
# is the only thing pruning and sorting trust (never the storage backend's
# modified time, which R2 resets if an object is ever copied), so a file
# someone dropped in by hand with any other name is never deleted.
_BACKUP_NAME_RE = re.compile(r"^(?:auto_)?backup_(\d{4}_\d{2}_\d{2}_\d{6})\.(?:json|db)$")


def _backup_stamp(name):
    m = _BACKUP_NAME_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y_%m_%d_%H%M%S")
    except ValueError:
        return None


def _export_all_tables_json():
    """
    Every table in the database as one UTF-8 JSON document, for backups when
    the database is Postgres (there is no single file to copy). Reads the
    live table list instead of the models so the security tables that
    security.py creates by hand (app_security, login_rate_limits, ...) are
    included too. Password and PIN hashes are included, same as the SQLite
    file copy; the bucket is private and downloads are admin-only. Needs an
    app context. Values JSON cannot represent (dates, Decimals) become
    strings via default=str.
    """
    quote = db.engine.dialect.identifier_preparer.quote
    tables = {}
    for name in sorted(sa_inspect(db.engine).get_table_names()):
        rows = db.session.execute(sa_text(f"SELECT * FROM {quote(name)}")).mappings().all()
        tables[name] = [dict(r) for r in rows]
    payload = {"exported_at": datetime.now().isoformat(), "tables": tables}
    return json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")


def _save_json_backup(prefix):
    """Writes a JSON export to storage and returns the file's basename."""
    name = f"{prefix}{datetime.now():%Y_%m_%d_%H%M%S}.json"
    payload = _export_all_tables_json()
    # The export leaves a Postgres transaction open on this session. Release
    # the connection before the (possibly slow) upload: Neon's pooler pins a
    # server connection for as long as a transaction stays open.
    db.session.remove()
    storage.save_bytes(payload, BACKUP_PREFIX + name, "application/json")
    return name


def _prune_backups(retention_days):
    """Deletes backups whose name-stamp is older than retention_days; returns how many."""
    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = 0
    for key in storage.list_keys(BACKUP_PREFIX):
        name = key[len(BACKUP_PREFIX):]
        stamp = _backup_stamp(name)
        if stamp is not None and stamp < cutoff:
            storage.delete(key)
            removed += 1
            app.logger.info("Pruned old backup: %s", name)
    return removed


def _scheduled_cloud_backup():
    # Called from the scheduler thread, which has no request or app context.
    with app.app_context():
        return _save_json_backup("auto_backup_")


@app.route("/api/backup", methods=["POST"])
@login_required
@role_required("admin")
def backup_now():
    if _database_url:
        # Postgres: nothing to copy, and database.db does not exist here.
        return jsonify({"ok": True, "file": _save_json_backup("backup_")})
    # SQLite: copy the database file as before, but hand the copy to storage
    # so the file lands in uploads/backups/ locally and in the bucket on R2.
    name = f"backup_{datetime.now():%Y_%m_%d_%H%M%S}.db"
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # SQLite's online backup API, not a file copy: the database runs in
        # WAL mode, so recent commits can still be sitting in database.db-wal
        # and a plain copy of database.db would silently leave them out.
        src = sqlite3.connect(DB_PATH)
        try:
            dst = sqlite3.connect(tmp_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        storage.save_file(tmp_path, BACKUP_PREFIX + name, "application/octet-stream")
    finally:
        os.remove(tmp_path)
    return jsonify({"ok": True, "file": name})


@app.route("/api/backup/download/<filename>")
@login_required
@role_required("admin")
def download_backup(filename):
    safe = secure_filename(filename)
    if not safe:
        abort(404)
    return _send_stored(BACKUP_PREFIX + safe, download_name=safe)


@app.route("/api/backup/list")
@login_required
@role_required("admin")
def list_backups():
    names = [key[len(BACKUP_PREFIX):] for key in storage.list_keys(BACKUP_PREFIX)]
    # Newest first by the stamp in the name: sorting by name alone would put
    # every auto_backup_ file after every backup_ file whatever its date.
    names.sort(key=lambda n: (_backup_stamp(n) or datetime.min, n), reverse=True)
    return jsonify(names)


# --------------------------------------------------------------------------
# CLI bootstrap: create tables + default admin
# --------------------------------------------------------------------------
def _enable_multi_user_mode():
    """
    Switches SQLite into WAL (Write-Ahead Logging) mode. By default,
    SQLite lets only one connection write at a time and can make other
    connections wait, which becomes noticeable once more than one
    computer on the network is using the app at the same time (e.g. two
    staff members issuing books at the same moment). WAL mode lets reads
    and writes happen concurrently in almost all cases, which is exactly
    the pattern a multi-computer library desk needs. This is a one-time
    setting stored in the database file itself — safe to run every startup.
    """
    import sqlite3
    db_path = os.path.join(BASE_DIR, "database.db")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=15000;")  # wait up to 15s instead of failing instantly if briefly locked — gives headroom for a whole classroom logging in within the same few seconds
        conn.commit()
        conn.close()
    except Exception as e:
        app.logger.warning(f"Could not enable WAL mode (non-fatal): {e}")


def _auto_migrate_schema():
    """
    Runs on every app startup, right after db.create_all(), inside an app
    context. Compares the live database against what models.py currently
    declares, and adds any missing columns automatically. This is what
    makes the app self-healing if the database was created by an older
    version of models.py — no manual migration script to remember to run.
    Never removes a column or touches existing data; only ever adds.

    Works on whichever database db.engine points at (the local SQLite file
    or Postgres on Neon): it asks SQLAlchemy's inspector what exists
    rather than opening the SQLite file directly, and takes each column's
    type from the model compiled for the engine's own dialect.

    Returns the "table.column" names it added (empty when the schema was
    already current). Failures are logged and never stop startup.
    """
    added = []
    try:
        dialect = db.engine.dialect
        quote = dialect.identifier_preparer.quote
        inspector = sa_inspect(db.engine)
        existing_tables = set(inspector.get_table_names())
        for mapper in db.Model.registry.mappers:
            table_name = mapper.class_.__tablename__
            if table_name not in existing_tables:
                continue  # db.create_all() builds missing tables, columns and all
            existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
            for col in mapper.columns:
                if col.name in existing_cols:
                    continue
                col_type = col.type.compile(dialect=dialect)
                default_clause = ""
                if col.default is not None and getattr(col.default, "arg", None) is not None:
                    dv = col.default.arg
                    if isinstance(dv, str):
                        escaped = dv.replace("'", "''")
                        default_clause = f" DEFAULT '{escaped}'"
                    elif isinstance(dv, bool):
                        # SQLite stores booleans as 0/1; Postgres has a real
                        # boolean type and rejects DEFAULT 1 for it.
                        if dialect.name == "sqlite":
                            default_clause = f" DEFAULT {1 if dv else 0}"
                        else:
                            default_clause = f" DEFAULT {'TRUE' if dv else 'FALSE'}"
                    elif isinstance(dv, (int, float)):
                        default_clause = f" DEFAULT {dv}"
                # One transaction per column, so one failure can't block the rest.
                try:
                    with db.engine.begin() as conn:
                        conn.execute(sa_text(
                            f"ALTER TABLE {quote(table_name)} "
                            f"ADD COLUMN {quote(col.name)} {col_type}{default_clause}"
                        ))
                    added.append(f"{table_name}.{col.name}")
                except Exception as e:
                    app.logger.warning(
                        f"Auto-migration could not add {table_name}.{col.name} (non-fatal): {e}"
                    )
    except Exception as e:
        app.logger.warning(f"Auto-migration check failed (non-fatal): {e}")
    if added:
        app.logger.info(f"Auto-migration added missing column(s): {', '.join(added)}")
    return added


def init_db():
    with app.app_context():
        db.create_all()
        _auto_migrate_schema()
        _enable_multi_user_mode()
        security.ensure_security_tables(DB_PATH)
        # NOTE: this used to auto-create admin/admin123, librarian/lib123,
        # and viewer/view123 here on every fresh install — shipping the
        # same three publicly-documented default passwords to every school
        # that ever runs this software. That's now replaced by the
        # first-run Setup Wizard (see /setup and _require_first_run_setup):
        # on a database with zero users, every request is redirected there
        # so the school picks its own admin username and password before
        # the system is usable at all.
        #
        # Upgrade safety: a school already running an older version of
        # this system will have existing User rows (including possibly the old
        # admin/lib123/view123 defaults) but has never set the new
        # "setup_complete" flag, since that flag didn't exist before. If we
        # didn't check for that here, every existing install would get
        # redirected into the setup wizard on first launch after updating,
        # which would be broken (it already has an admin account) rather
        # than helpful. So: only ever force the wizard for a database that
        # truly has zero accounts.
        if not security.setup_is_complete(DB_PATH) and User.query.count() > 0:
            security.mark_setup_complete(DB_PATH)


# Runs once, unconditionally, whether this module is started directly
# (python app.py / RUN_SmartLib360.bat) or imported by a WSGI server like
# gunicorn (Render). This has to happen outside the __main__ guard below:
# gunicorn imports this file as a module rather than running it directly,
# so __name__ is never "__main__" in that case, and code that only lived
# inside that block (as init_db() used to) would simply never run —
# meaning no tables would ever get created on a fresh Postgres database,
# and the very first request would crash.
init_db()

# On Postgres there is no database file to copy, so the scheduler exports
# every table to JSON in storage instead. Like init_db() this must sit
# outside the __main__ guard so gunicorn starts it. Tests never set
# DATABASE_URL, so they never start this thread.
if _database_url:
    backup_scheduler.start_cloud_backups(
        backup_fn=_scheduled_cloud_backup,
        prune_fn=_prune_backups,
        logger=app.logger,
    )

if __name__ == "__main__":
    # The SQLite file copier is pointless (and would back up an empty,
    # unused database.db) when running against Postgres.
    if not _database_url:
        backup_scheduler.start_background_backups(
            db_path=DB_PATH,
            backups_dir=app.config["UPLOAD_BACKUPS"],
            logger=app.logger,
        )

    lan_ip = get_lan_ip()
    hostname = get_hostname()
    print("=" * 64)
    print(f"  {PRODUCT_NAME} is starting...")
    print(f"  On THIS computer, open:      http://127.0.0.1:5000")
    if hostname:
        print(f"  On OTHER library computers:  http://{hostname}:5000   (or http://{lan_ip}:5000)")
    else:
        print(f"  On OTHER library computers:  http://{lan_ip}:5000")
    print("  (Other computers must be on the same school Wi-Fi/network.")
    print("   If they can't connect, check Windows Firewall — see the")
    print("   'Multiple Computers / Client-Server Setup' notes.)")
    if not security.setup_is_complete(DB_PATH):
        print("  FIRST RUN: no admin account exists yet — open the link above")
        print("  and you will be taken to the Setup Wizard to create one.")
    print("  Automatic daily backups are enabled (uploads/backups/).")
    print(f"  Full activity/error log: logs/app.log (share this file if something breaks)")
    print("=" * 64)

    try:
        try:
            from waitress import serve
            # threads=24 — with 30 students potentially reading/watching at
            # once, each large PDF/video transfer can occupy a thread for a
            # while; 8 was becoming a bottleneck (everyone else queues behind
            # whichever transfers are already in progress). 24 gives much more
            # headroom for real concurrent classroom use.
            #
            # Bind host is configurable via LIBRARY_BIND_HOST so a school
            # running this on their own LAN (the default/normal case) keeps
            # 0.0.0.0 — other computers on the same Wi-Fi need that. An
            # internet-facing deployment behind a reverse proxy (e.g. Nginx
            # on the same server) should set LIBRARY_BIND_HOST=127.0.0.1
            # instead, so Waitress is only reachable through the proxy and
            # never directly from the internet.
            bind_host = os.environ.get("LIBRARY_BIND_HOST", "0.0.0.0")
            serve_kwargs = dict(host=bind_host, port=5000, threads=24,
                                 channel_timeout=1800, connection_limit=300)
            if _FORCE_HTTPS:
                # Waitress does NOT trust X-Forwarded-Proto (or any other
                # forwarded header) by default — it silently strips them,
                # as a deliberate anti-spoofing measure, unless the proxy
                # sending them is explicitly marked trusted. Without this,
                # the app-level HTTPS redirect/HSTS logic above would never
                # see "https" from Nginx and would redirect every request
                # in an infinite loop. 127.0.0.1 is safe to trust here
                # specifically because LIBRARY_BIND_HOST=127.0.0.1 is what
                # every internet-facing deployment guide (DEPLOY_TO_VPS.md)
                # tells the school to set alongside this flag — Waitress is
                # then only reachable through Nginx on the same machine, so
                # nothing else could forge this header and have Waitress
                # believe it.
                serve_kwargs.update(
                    trusted_proxy="127.0.0.1",
                    trusted_proxy_headers={"x-forwarded-proto", "x-forwarded-for", "x-forwarded-host"},
                    clear_untrusted_proxy_headers=True,
                )
            serve(app, **serve_kwargs)
        except ImportError:
            app.logger.warning(
                "The 'waitress' package isn't installed (run: pip install -r requirements.txt) "
                "— falling back to Flask's built-in server, which is fine for one computer at a "
                "time but not recommended once multiple computers use the app together."
            )
            app.run(debug=False, host="0.0.0.0", port=5000)
    except Exception:
        # Whatever this was, it's fatal enough to end the process — but log
        # it to the file first (not just the console window, which vanishes
        # the instant the process exits) so a crash is diagnosable instead
        # of just "it stopped working" with no evidence of why.
        app.logger.exception("FATAL: server process is exiting due to an unhandled error")
        raise
