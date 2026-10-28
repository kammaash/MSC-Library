"""
Tests for migrate_to_cloud.py.

SQLite -> SQLite stands in for SQLite -> Postgres: both sides are built from
the Flask-SQLAlchemy model metadata on a plain engine (no Flask app needed),
which is also all the script itself does — it never imports app.py.
"""
import shutil
import tempfile

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

import migrate_to_cloud
from migrate_to_cloud import copy_database, copy_uploads
from models import db


def _make_db(tmp_path, name):
    url = f"sqlite:///{tmp_path / name}"
    engine = sa.create_engine(url)
    db.metadata.create_all(engine)
    engine.dispose()
    return url


def _query(url, sql):
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(sa.text(sql))]
    finally:
        engine.dispose()


@pytest.fixture
def seeded_source_db(tmp_path):
    url = _make_db(tmp_path, "source.db")
    engine = sa.create_engine(url)
    t = db.metadata.tables
    with engine.begin() as conn:
        conn.execute(t["online_books"].insert().values(
            id=7, title="Old Book", pdf_path="uploads\\pdfs\\old.pdf",
            qr_path="uploads\\qr\\old.png", preview_pdf_path=None,
            ocr_applied=1, file_type="pdf"))
        # FK child of online_books: forces parent-before-child ordering.
        conn.execute(t["read_history"].insert().values(
            id=3, member_id="MEM-1", online_book_id=7, open_count=2))
    engine.dispose()
    return url


@pytest.fixture
def empty_target_db(tmp_path):
    return _make_db(tmp_path, "target.db")


def test_copies_rows_and_fixes_windows_paths(seeded_source_db, empty_target_db):
    counts = copy_database(seeded_source_db, empty_target_db)
    assert counts["online_books"] == 1
    assert counts["read_history"] == 1
    row = _query(empty_target_db, "select pdf_path, qr_path, preview_pdf_path, ocr_applied from online_books")
    assert row == [("uploads/pdfs/old.pdf", "uploads/qr/old.png", None, 1)]
    # Ids are preserved so foreign keys keep pointing at the right rows.
    assert _query(empty_target_db, "select id, online_book_id, open_count from read_history") == [(3, 7, 2)]


@pytest.mark.parametrize("raw, expected", [(1, True), (0, False), ("1", True), ("0", False),
                                           (True, True), (False, False), (None, None),
                                           ("True", True), ("False", False), ("false", False),
                                           (" FALSE ", False), ("t", True), ("F", False),
                                           ("yes", True), ("No", False), ("y", True), ("n", False),
                                           ("", False), ("2", True), ("1.0", True), ("0.0", False)])
def test_boolean_conversion(raw, expected):
    # SQLite keeps 0/1 (sometimes as text, sometimes as the words "True" /
    # "False"); a real Postgres BOOLEAN column rejects integers, so the value
    # handed to the target must already be a bool (None stays None). The
    # string "False" in particular must not become True.
    assert migrate_to_cloud._to_bool(raw) is expected


def test_boolean_conversion_rejects_unreadable_text():
    with pytest.raises(ValueError, match="maybe"):
        migrate_to_cloud._to_bool("maybe")


def test_refuses_non_empty_target(seeded_source_db):
    with pytest.raises(RuntimeError, match="not empty") as exc:
        copy_database(seeded_source_db, seeded_source_db)
    assert "online_books" in str(exc.value)


def test_non_empty_target_is_left_untouched(seeded_source_db, tmp_path):
    target = _make_db(tmp_path, "partial.db")
    engine = sa.create_engine(target)
    with engine.begin() as conn:
        conn.execute(db.metadata.tables["certificates"].insert().values(
            id=1, member_id="MEM-1", title="Top Reader"))
    engine.dispose()
    with pytest.raises(RuntimeError, match="certificates"):
        copy_database(seeded_source_db, target)
    assert _query(target, "select count(*) from online_books") == [(0,)]


def test_failure_midway_leaves_target_empty(seeded_source_db, empty_target_db):
    # A NOT NULL violation in the source data must roll everything back.
    engine = sa.create_engine(seeded_source_db)
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
        conn.execute(sa.text("INSERT INTO certificates (id, member_id, title) VALUES (1, 'M', 'ok')"))
    engine.dispose()
    # Make the target stricter than the source for one table.
    t_engine = sa.create_engine(empty_target_db)
    with t_engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE certificates"))
        conn.execute(sa.text("CREATE TABLE certificates (id INTEGER PRIMARY KEY, member_id VARCHAR(30) NOT NULL, "
                             "title VARCHAR(150) NOT NULL CHECK (title <> 'ok'), awarded_date VARCHAR(10), issued_by VARCHAR(150))"))
    t_engine.dispose()
    with pytest.raises(sa.exc.IntegrityError):
        copy_database(seeded_source_db, empty_target_db)
    assert _query(empty_target_db, "select count(*) from online_books") == [(0,)]
    assert _query(empty_target_db, "select count(*) from read_history") == [(0,)]


def test_table_missing_from_source_is_skipped(tmp_path, empty_target_db, capsys):
    url = f"sqlite:///{tmp_path / 'partial_source.db'}"
    engine = sa.create_engine(url)
    db.metadata.tables["certificates"].create(engine)
    engine.dispose()
    counts = copy_database(url, empty_target_db)
    assert counts == {"certificates": 0}
    assert "online_books" in capsys.readouterr().out


def test_only_common_columns_are_copied(tmp_path, empty_target_db):
    # Source has an extra column the target doesn't know about, and lacks one
    # the target has: only the columns present in both are copied.
    url = f"sqlite:///{tmp_path / 'old_schema.db'}"
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE certificates (id INTEGER PRIMARY KEY, member_id VARCHAR(30), "
                             "title VARCHAR(150), legacy_note TEXT)"))
        conn.execute(sa.text("INSERT INTO certificates VALUES (1, 'M1', 'Star', 'x')"))
    engine.dispose()
    assert copy_database(url, empty_target_db)["certificates"] == 1
    assert _query(empty_target_db, "select member_id, title, awarded_date from certificates") == [("M1", "Star", None)]


def test_column_missing_from_old_source_gets_the_model_default(seeded_source_db, empty_target_db):
    # A PC database from before read_history.progress_seconds existed: the
    # copied rows must get the model's default (0), not NULL.
    engine = sa.create_engine(seeded_source_db)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE read_history DROP COLUMN progress_seconds"))
    engine.dispose()
    assert copy_database(seeded_source_db, empty_target_db)["read_history"] == 1
    assert _query(empty_target_db, "select id, open_count, progress_seconds from read_history") == [(3, 2, 0)]


def test_column_present_in_source_keeps_its_own_value_and_null(seeded_source_db, empty_target_db):
    # The default only fills columns the source lacks; a value (or NULL) the
    # source does have is copied as it is.
    engine = sa.create_engine(seeded_source_db)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE read_history SET progress_seconds = NULL"))
        conn.execute(sa.text("INSERT INTO read_history (id, member_id, online_book_id, open_count, "
                             "progress_seconds) VALUES (4, 'MEM-2', 7, 1, 95)"))
    engine.dispose()
    copy_database(seeded_source_db, empty_target_db)
    assert _query(empty_target_db, "select id, progress_seconds from read_history order by id") == [(3, None), (4, 95)]


def test_postgres_sequence_reset_sql_quotes_the_table():
    # The dialect alone is enough; no driver or connection needed.
    dialect = postgresql.dialect()
    sql = migrate_to_cloud._sequence_reset_sql(dialect, "online_books")
    # Next id is MAX+1 (1 for an empty table), not MAX or 2.
    assert sql == ("SELECT setval(pg_get_serial_sequence('online_books', 'id'), "
                   "COALESCE(MAX(id), 0) + 1, false) FROM online_books")
    # Reserved words get quoted, inside the string literal and outside it.
    sql = migrate_to_cloud._sequence_reset_sql(dialect, "user")
    assert "pg_get_serial_sequence('\"user\"', 'id')" in sql and sql.endswith('FROM "user"')


def test_copy_uploads_to_r2(tmp_path, r2):
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "pdfs" / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "pdfs" / ".gitkeep").write_bytes(b"")
    (tmp_path / ".DS_Store").write_bytes(b"junk")
    assert copy_uploads(str(tmp_path), r2) == 1
    assert r2.list_keys("uploads/") == ["uploads/pdfs/a.pdf"]
    head = r2.client.head_object(Bucket=r2.bucket, Key="uploads/pdfs/a.pdf")
    assert head["ContentType"] == "application/pdf"


def test_copy_uploads_skips_old_sqlite_backups(tmp_path, r2):
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "library_backup.db").write_bytes(b"sqlite")
    (tmp_path / "qrcodes").mkdir()
    (tmp_path / "qrcodes" / "q.png").write_bytes(b"png")
    assert copy_uploads(str(tmp_path), r2) == 1
    assert r2.list_keys("uploads/") == ["uploads/qrcodes/q.png"]


def test_main_requires_database_url(tmp_path, capsys):
    (tmp_path / "database.db").write_bytes(b"")
    (tmp_path / "uploads").mkdir()
    code = migrate_to_cloud.main(["--sqlite", str(tmp_path / "database.db"),
                                  "--uploads", str(tmp_path / "uploads")], env={})
    assert code == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_main_requires_r2(tmp_path, capsys):
    (tmp_path / "database.db").write_bytes(b"")
    (tmp_path / "uploads").mkdir()
    code = migrate_to_cloud.main(["--sqlite", str(tmp_path / "database.db"),
                                  "--uploads", str(tmp_path / "uploads")],
                                 env={"DATABASE_URL": "postgres://u:p@h/db"})
    assert code == 2
    assert "R2" in capsys.readouterr().err


def test_main_upload_failure_leaves_database_empty_and_rerun_works(
        seeded_source_db, empty_target_db, tmp_path, r2, monkeypatch, capsys):
    uploads = tmp_path / "uploads"
    (uploads / "pdfs").mkdir(parents=True)
    (uploads / "pdfs" / "old.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(migrate_to_cloud, "get_storage", lambda base_dir, env: r2)
    argv = ["--sqlite", seeded_source_db.removeprefix("sqlite:///"), "--uploads", str(uploads)]
    env = {"DATABASE_URL": empty_target_db}

    real_copy_uploads = migrate_to_cloud.copy_uploads

    def failing(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(migrate_to_cloud, "copy_uploads", failing)
    assert migrate_to_cloud.main(argv, env=env) == 1
    assert "Migration failed: network down" in capsys.readouterr().err
    assert _query(empty_target_db, "select count(*) from online_books") == [(0,)]

    monkeypatch.setattr(migrate_to_cloud, "copy_uploads", real_copy_uploads)
    assert migrate_to_cloud.main(argv, env=env) == 0
    assert _query(empty_target_db, "select count(*) from online_books") == [(1,)]
    assert r2.list_keys("uploads/") == ["uploads/pdfs/old.pdf"]


def test_main_refuses_non_empty_target_before_uploading(seeded_source_db, tmp_path, r2, monkeypatch, capsys):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "a.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(migrate_to_cloud, "get_storage", lambda base_dir, env: r2)
    code = migrate_to_cloud.main(["--sqlite", seeded_source_db.removeprefix("sqlite:///"),
                                  "--uploads", str(uploads)], env={"DATABASE_URL": seeded_source_db})
    assert code == 1
    assert "Migration failed: Target database is not empty" in capsys.readouterr().err
    assert r2.list_keys("uploads/") == []


def test_target_without_tables_is_refused_before_uploading(tmp_path, r2, seeded_source_db, monkeypatch, capsys):
    # A brand-new Neon database has no tables until the app has started once
    # against it. Copying "into" it would silently do nothing and still report
    # success, so it must fail up front, before any file is uploaded.
    bare_target = f"sqlite:///{tmp_path / 'bare.db'}"
    sa.create_engine(bare_target).dispose()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "a.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(migrate_to_cloud, "get_storage", lambda base_dir, env: r2)
    code = migrate_to_cloud.main(["--sqlite", seeded_source_db.removeprefix("sqlite:///"),
                                  "--uploads", str(uploads)], env={"DATABASE_URL": bare_target})
    assert code == 1
    err = capsys.readouterr().err
    assert "Start the app once" in err and "DATABASE_URL" in err
    assert r2.list_keys("uploads/") == []
    with pytest.raises(RuntimeError, match="Start the app once"):
        migrate_to_cloud.ensure_target_empty(bare_target)


# --- rows left dangling on the PC (SQLite never enforced foreign keys) -------

@pytest.fixture
def orphan_source_db(tmp_path):
    """A PC database where a member and a digital item were deleted but rows
    pointing at them remain: users.member_id -> members.id (nullable) and
    bookmarks/favorites/read_history.online_book_id -> online_books.id."""
    url = _make_db(tmp_path, "orphans.db")
    engine = sa.create_engine(url)
    # What the PC's app did: foreign keys not enforced (some SQLite builds
    # default them to ON, so say OFF explicitly).
    sa.event.listen(engine, "connect", lambda conn, _rec: conn.execute("PRAGMA foreign_keys=OFF"))
    t = db.metadata.tables
    with engine.begin() as conn:
        conn.execute(t["members"].insert().values(id=1, member_id="MEM-1", name="Asha"))
        conn.execute(t["online_books"].insert().values(id=7, title="Kept Book", pdf_path="uploads/pdfs/k.pdf", file_type="pdf"))
        conn.execute(t["users"].insert(), [
            {"id": 1, "username": "asha", "name": "Asha", "role": "student", "member_id": 1},
            {"id": 2, "username": "gone", "name": "Gone", "role": "student", "member_id": 99},  # member deleted
            {"id": 3, "username": "staff", "name": "Staff", "role": "librarian", "member_id": None},
        ])
        conn.execute(t["bookmarks"].insert(), [
            {"id": 1, "member_id": "MEM-1", "online_book_id": 7},
            {"id": 2, "member_id": "MEM-1", "online_book_id": 8},  # item deleted
            {"id": 3, "member_id": "MEM-1", "online_book_id": 8},
        ])
        conn.execute(t["favorites"].insert().values(id=1, member_id="MEM-1", online_book_id=8))
        conn.execute(t["read_history"].insert().values(id=1, member_id="MEM-1", online_book_id=7, open_count=1))
    engine.dispose()
    return url


@pytest.fixture
def sqlite_foreign_keys_on():
    # SQLite ignores foreign keys unless every connection asks for them; make
    # the target behave like Postgres.
    def enable(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
    sa.event.listen(sa.engine.Engine, "connect", enable)
    yield
    sa.event.remove(sa.engine.Engine, "connect", enable)


def _check_orphans_repaired(target_url, counts, out):
    assert counts["users"] == 3
    assert counts["bookmarks"] == 1
    assert counts["favorites"] == 0
    assert counts["read_history"] == 1
    assert _query(target_url, "select id, member_id from users order by id") == [(1, 1), (2, None), (3, None)]
    assert _query(target_url, "select id, online_book_id from bookmarks") == [(1, 7)]
    assert "  bookmarks: 2 rows skipped (they pointed at a deleted digital item)" in out
    assert "  favorites: 1 row skipped (they pointed at a deleted digital item)" in out
    assert "  users: member_id cleared on 1 row (they pointed at a deleted member" in out
    assert "read_history" not in out.split("Some rows pointed")[1]


def test_rows_pointing_at_deleted_parents_are_repaired_not_fatal(
        orphan_source_db, empty_target_db, sqlite_foreign_keys_on, capsys):
    # Sanity check that the target really enforces foreign keys here.
    engine = sa.create_engine(empty_target_db)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(db.metadata.tables["bookmarks"].insert().values(id=1, member_id="M", online_book_id=8))
    engine.dispose()

    counts = copy_database(orphan_source_db, empty_target_db)
    _check_orphans_repaired(empty_target_db, counts, capsys.readouterr().out)


def test_main_reports_repairs_and_succeeds(orphan_source_db, empty_target_db, sqlite_foreign_keys_on,
                                           tmp_path, r2, monkeypatch, capsys):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(migrate_to_cloud, "get_storage", lambda base_dir, env: r2)
    code = migrate_to_cloud.main(["--sqlite", orphan_source_db.removeprefix("sqlite:///"),
                                  "--uploads", str(uploads)], env={"DATABASE_URL": empty_target_db})
    assert code == 0
    assert "bookmarks: 2 rows skipped" in capsys.readouterr().out


def test_main_failure_is_one_readable_line(seeded_source_db, empty_target_db, tmp_path, r2, monkeypatch, capsys):
    # A genuine database error (here a CHECK constraint) must come out as one
    # line naming the table, with no SQL statement or row values in it.
    t_engine = sa.create_engine(empty_target_db)
    with t_engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE online_books"))
        conn.execute(sa.text("DROP TABLE read_history"))
        conn.execute(sa.text("CREATE TABLE online_books (id INTEGER PRIMARY KEY, title VARCHAR(200) "
                             "CHECK (title <> 'Old Book'))"))
    t_engine.dispose()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(migrate_to_cloud, "get_storage", lambda base_dir, env: r2)
    argv = ["--sqlite", seeded_source_db.removeprefix("sqlite:///"), "--uploads", str(uploads)]
    assert migrate_to_cloud.main(argv, env={"DATABASE_URL": empty_target_db}) == 1
    err = capsys.readouterr().err
    assert err.strip().splitlines() == ["Migration failed: online_books: CHECK constraint failed: title <> 'Old Book'"]
    assert "Traceback" not in err and "INSERT" not in err

    assert migrate_to_cloud.main(argv + ["--verbose"], env={"DATABASE_URL": empty_target_db}) == 1
    err = capsys.readouterr().err
    assert "Migration failed: online_books:" in err and "Traceback" in err


@pytest.fixture(scope="module")
def postgres_url():
    pgserver = pytest.importorskip("pgserver")
    data_dir = tempfile.mkdtemp(prefix="smartlib-pg-")
    server = pgserver.get_server(data_dir, cleanup_mode="stop")
    try:
        yield migrate_to_cloud._sqlalchemy_database_uri(server.get_uri())
    finally:
        server.cleanup()
        shutil.rmtree(data_dir, ignore_errors=True)


def test_orphans_repaired_on_real_postgres(orphan_source_db, postgres_url, capsys):
    engine = sa.create_engine(postgres_url)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            conn.execute(sa.text("CREATE SCHEMA public"))
        db.metadata.create_all(engine)

        counts = copy_database(orphan_source_db, postgres_url)
        _check_orphans_repaired(postgres_url, counts, capsys.readouterr().out)

        # Sequences continue after the highest copied id; an empty table starts at 1.
        with engine.begin() as conn:
            t = db.metadata.tables
            new_user = conn.execute(t["users"].insert().values(username="new", name="N", role="viewer")
                                    .returning(t["users"].c.id)).scalar()
            new_cert = conn.execute(t["certificates"].insert().values(member_id="M", title="T")
                                    .returning(t["certificates"].c.id)).scalar()
        assert new_user == 4
        assert new_cert == 1
    finally:
        engine.dispose()
