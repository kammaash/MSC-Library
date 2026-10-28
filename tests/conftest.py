"""
Test harness for SmartLib 360.

Importing app.py has side effects: it computes BASE_DIR/DB_PATH, opens that
database, creates tables, and (via security.py) reads DATABASE_URL — all at
import time. So the environment has to be prepared *before* `import app`
ever runs, which is why the env handling below sits at module level rather
than inside a fixture.

Helpers (make_admin, make_student, login_as) are plain functions; tests
import them with `from conftest import make_admin, make_student, login_as`.
Fixtures are for things that need setup/teardown (app_module, db_reset,
client).
"""
import atexit
import os
import shutil
import sys
import tempfile

import pytest

# Make the project root importable regardless of where pytest is launched.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A throwaway data directory for the whole test session: database.db,
# instance_secret.key, uploads/, logs/ all land here instead of the repo.
_DATA_DIR = tempfile.mkdtemp(prefix="smartlib-tests-")
atexit.register(shutil.rmtree, _DATA_DIR, ignore_errors=True)
os.environ["LIBRARY_DATA_DIR"] = _DATA_DIR

# Tests must always run in offline/local mode, whatever the developer's shell
# happens to have exported (a stray DATABASE_URL would point the suite at a
# real Postgres database).
_SCRUBBED_EXACT = {"DATABASE_URL", "PUBLIC_BASE_URL"}
_SCRUBBED_PREFIXES = ("R2_", "ENTRA_")
for _name in list(os.environ):
    if _name in _SCRUBBED_EXACT or _name.startswith(_SCRUBBED_PREFIXES):
        del os.environ[_name]

CSRF_TOKEN = "test-csrf"


@pytest.fixture(scope="session")
def app_module():
    import app
    # Last line of defence: never let a fixture below drop tables in a
    # database that is not the throwaway one.
    assert os.environ["LIBRARY_DATA_DIR"] in app.DB_PATH, (
        f"tests would run against a real database: {app.DB_PATH}"
    )
    return app


@pytest.fixture(autouse=True)
def db_reset(app_module):
    """Give every test an empty schema, with first-run setup marked done."""
    from models import db
    from sqlalchemy import text
    import security

    with app_module.app.app_context():
        db.drop_all()
        db.create_all()
        security.ensure_security_tables(app_module.DB_PATH)
        security.mark_setup_complete(app_module.DB_PATH)
        # These two tables are managed by security.py, not by the models, so
        # drop_all() does not clear them. Stale rows would leak lockouts and
        # must-change-password flags (keyed by reused user ids) between tests.
        db.session.execute(text("DELETE FROM login_rate_limits"))
        db.session.execute(text("DELETE FROM user_security_flags"))
        db.session.commit()
    yield


@pytest.fixture
def client(app_module):
    """Test client that passes the CSRF check on every request."""
    c = app_module.app.test_client()
    with c.session_transaction() as sess:
        sess["_csrf_token"] = CSRF_TOKEN
    c.environ_base["HTTP_X_CSRF_TOKEN"] = CSRF_TOKEN
    return c


def _persist(obj):
    """Commit obj and return it detached but fully loaded, so it stays
    usable after the app context (and its session) is gone."""
    import app
    from models import db

    with app.app.app_context():
        db.session.add(obj)
        db.session.commit()
        db.session.refresh(obj)
        db.session.expunge(obj)
    return obj


def make_admin(username="admin", password="admin-pass-123"):
    from models import User
    from werkzeug.security import generate_password_hash

    return _persist(User(username=username, name="Test Admin", role="admin",
                         password_hash=generate_password_hash(password)))


def make_student(admission_no="A100", dob="2014-05-01", class_dept="7-A", **fields):
    """Creates only the Member row (no User). Extra fields (email, entra_oid,
    ...) are set on the row as-is."""
    from models import Member

    member = Member(member_id=f"MEM-{admission_no}", name="Test Student",
                    type="Student", class_dept=class_dept,
                    admission_no=admission_no, dob=dob)
    for key, value in fields.items():
        setattr(member, key, value)
    return _persist(member)


def login_as(client, user):
    """Put an authenticated session on the client without going through /login."""
    import app

    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        sess["srv"] = app.SERVER_INSTANCE_ID


@pytest.fixture
def r2(app_module, monkeypatch):
    """An R2Storage backed by moto's in-memory S3, installed as app.storage.

    raising=False because app.storage only exists once app.py wires it up;
    this keeps the fixture usable either way.
    """
    import boto3
    from botocore.config import Config
    from moto import mock_aws
    from storage import R2Storage

    # Dummy credentials so botocore never reaches for real ones.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        # SigV4 like production (get_storage); the default would presign with
        # the legacy scheme, which has no X-Amz-Expires parameter.
        client = boto3.client("s3", region_name="us-east-1",
                              config=Config(signature_version="s3v4"))
        client.create_bucket(Bucket="test-bucket")
        storage = R2Storage(client, "test-bucket")
        monkeypatch.setattr(app_module, "storage", storage, raising=False)
        yield storage
