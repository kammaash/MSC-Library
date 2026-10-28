"""
Tests for the final-review hardening wave: database URL handling, error
messages, backup access, deletes under enforced foreign keys, and so on.
"""
import pytest

import migrate_to_cloud


@pytest.mark.parametrize("raw, expected", [
    ("postgres://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
    ("postgresql://u:p@h/db?sslmode=require", "postgresql+psycopg2://u:p@h/db?sslmode=require"),
    ("postgresql+psycopg2://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
    ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ("sqlite:///x.db", "sqlite:///x.db"),
])
def test_sqlalchemy_database_uri(app_module, raw, expected):
    assert app_module._sqlalchemy_database_uri(raw) == expected
    assert migrate_to_cloud._sqlalchemy_database_uri(raw) == expected


def test_rate_limit_timestamps_are_double_precision(app_module):
    # On Postgres REAL is float4, which cannot hold a unix timestamp to better
    # than ~64 seconds. SQLite maps DOUBLE PRECISION to REAL affinity, so
    # existing SQLite databases behave exactly as before.
    import sqlite3
    conn = sqlite3.connect(app_module.DB_PATH)
    try:
        cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(login_rate_limits)")}
    finally:
        conn.close()
    assert cols["window_start"] == "DOUBLE PRECISION"
    assert cols["locked_until"] == "DOUBLE PRECISION"
