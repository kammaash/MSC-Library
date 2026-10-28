"""Tests for backup_scheduler._do_backup."""
import logging
import os
import sqlite3

import backup_scheduler


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT id, name FROM members ORDER BY id").fetchall()
    finally:
        conn.close()


def test_backup_includes_rows_still_in_the_wal(tmp_path):
    # The app runs SQLite in WAL mode: recent writes live in database.db-wal
    # until a checkpoint, so copying only database.db would lose them.
    db_path = tmp_path / "database.db"
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE members (id INTEGER PRIMARY KEY, name TEXT)")
    writer.executemany("INSERT INTO members (name) VALUES (?)", [("A",), ("B",), ("C",)])
    writer.commit()
    try:
        assert os.path.getsize(str(db_path) + "-wal") > 0  # the rows really are only in the WAL
        assert _rows(db_path) != []  # (a normal reader sees them through the WAL)

        backups = tmp_path / "backups"
        backup_scheduler._do_backup(str(db_path), str(backups))
    finally:
        writer.close()

    [copy] = list(backups.iterdir())
    assert copy.name.startswith("auto_backup_") and copy.suffix == ".db"
    assert _rows(copy) == [(1, "A"), (2, "B"), (3, "C")]


def test_backup_logs_success_and_skips_missing_database(tmp_path, caplog):
    logger = logging.getLogger("test_backup_scheduler")
    db_path = tmp_path / "database.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE members (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()
    with caplog.at_level(logging.INFO, logger=logger.name):
        backup_scheduler._do_backup(str(db_path), str(tmp_path / "b"), logger)
        backup_scheduler._do_backup(str(tmp_path / "missing.db"), str(tmp_path / "b2"), logger)
    assert [r.getMessage().split(":")[0] for r in caplog.records] == ["Automatic backup created"]
    assert len(os.listdir(tmp_path / "b")) == 1


def test_backup_failure_is_logged_not_raised(tmp_path, caplog):
    logger = logging.getLogger("test_backup_scheduler_fail")
    bad = tmp_path / "database.db"
    bad.write_bytes(b"this is not a sqlite database" * 100)
    with caplog.at_level(logging.WARNING, logger=logger.name):
        backup_scheduler._do_backup(str(bad), str(tmp_path / "b"), logger)
    assert any("Automatic backup failed (non-fatal)" in r.getMessage() for r in caplog.records)
