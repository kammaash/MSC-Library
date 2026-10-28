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
