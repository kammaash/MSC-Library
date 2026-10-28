from sqlalchemy import inspect, text

from models import db


def test_auto_migrate_adds_missing_columns(app_module):
    with app_module.app.app_context():
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE members DROP COLUMN email"))
        added = app_module._auto_migrate_schema()
        assert "members.email" in added
        cols = {c["name"] for c in inspect(db.engine).get_columns("members")}
        assert "email" in cols


def test_auto_migrate_is_noop_when_schema_current(app_module):
    with app_module.app.app_context():
        assert app_module._auto_migrate_schema() == []
