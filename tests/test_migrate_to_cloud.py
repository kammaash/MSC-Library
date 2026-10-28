"""
Tests for migrate_to_cloud.py.

SQLite -> SQLite stands in for SQLite -> Postgres: both sides are built from
the Flask-SQLAlchemy model metadata on a plain engine (no Flask app needed),
which is also all the script itself does — it never imports app.py.
"""
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
                                           (True, True), (False, False), (None, None)])
def test_boolean_conversion(raw, expected):
    # SQLite keeps 0/1; a real Postgres BOOLEAN column rejects integers, so
    # the value handed to the target must already be a bool (None stays None).
    assert migrate_to_cloud._to_bool(raw) is expected


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


def test_postgres_sequence_reset_sql_quotes_the_table():
    # The dialect alone is enough; no driver or connection needed.
    dialect = postgresql.dialect()
    sql = migrate_to_cloud._sequence_reset_sql(dialect, "online_books")
    assert sql == ("SELECT setval(pg_get_serial_sequence('online_books', 'id'), "
                   "COALESCE(MAX(id), 1)) FROM online_books")
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
        seeded_source_db, empty_target_db, tmp_path, r2, monkeypatch):
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
    with pytest.raises(OSError):
        migrate_to_cloud.main(argv, env=env)
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
    with pytest.raises(RuntimeError, match="not empty"):
        migrate_to_cloud.main(["--sqlite", seeded_source_db.removeprefix("sqlite:///"),
                               "--uploads", str(uploads)], env={"DATABASE_URL": seeded_source_db})
    assert r2.list_keys("uploads/") == []


def test_target_without_tables_is_refused_before_uploading(tmp_path, r2, seeded_source_db, monkeypatch):
    # A brand-new Neon database has no tables until the app has started once
    # against it. Copying "into" it would silently do nothing and still report
    # success, so it must fail up front, before any file is uploaded.
    bare_target = f"sqlite:///{tmp_path / 'bare.db'}"
    sa.create_engine(bare_target).dispose()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "a.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(migrate_to_cloud, "get_storage", lambda base_dir, env: r2)
    with pytest.raises(RuntimeError, match="Start the app once") as exc:
        migrate_to_cloud.main(["--sqlite", seeded_source_db.removeprefix("sqlite:///"),
                               "--uploads", str(uploads)], env={"DATABASE_URL": bare_target})
    assert "DATABASE_URL" in str(exc.value)
    assert r2.list_keys("uploads/") == []
    with pytest.raises(RuntimeError, match="Start the app once"):
        migrate_to_cloud.ensure_target_empty(bare_target)
