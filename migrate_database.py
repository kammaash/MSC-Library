"""
Montessori Digital Library — safe database migration.

WHAT THIS DOES:
Compares your existing database.db against the columns models.py expects
for every table, and adds any MISSING columns with a safe default value.
It never deletes a column, never deletes a row, and never touches data
in columns that already exist. Existing books, members, and loan records
are completely untouched.

WHEN TO RUN THIS:
Any time you update app.py/models.py to a newer version and see an error
like "'xyz' is an invalid keyword argument for SomeModel" or
"no such column: xyz" — that means the code expects a database column
that your database.db doesn't have yet. Run this once and it's fixed.

HOW TO RUN:
    venv\\Scripts\\activate
    python migrate_database.py

Safe to run multiple times — if there's nothing to add, it says so and
does nothing.
"""
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")

# Map SQLAlchemy column types to SQLite column types + a safe default.
SQLITE_TYPE_MAP = {
    "INTEGER": "INTEGER",
    "VARCHAR": "TEXT",
    "TEXT": "TEXT",
    "BOOLEAN": "BOOLEAN",
    "FLOAT": "FLOAT",
    "DATE": "TEXT",
    "DATETIME": "TEXT",
}


def main():
    if not os.path.exists(DB_PATH):
        print(f"No database.db found at {DB_PATH} — nothing to migrate.")
        print("(A fresh one will be created automatically the next time you start the app.)")
        return

    sys.path.insert(0, BASE_DIR)
    try:
        from app import app, db
        import models
    except Exception as e:
        print(f"Could not load app.py/models.py to check the expected schema: {e}")
        print("Make sure this script is in the same folder as app.py and models.py.")
        sys.exit(1)

    added_any = False
    with app.app_context():
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        for mapper in db.Model.registry.mappers:
            model_cls = mapper.class_
            table_name = model_cls.__tablename__

            cur.execute(f"PRAGMA table_info({table_name})")
            existing_cols = {row[1] for row in cur.fetchall()}
            if not existing_cols:
                # Table doesn't exist yet at all — db.create_all() (run automatically
                # on app startup) will create it fresh, nothing to migrate here.
                continue

            for col in mapper.columns:
                col_name = col.name
                if col_name in existing_cols:
                    continue

                sqlite_type = SQLITE_TYPE_MAP.get(str(col.type).split("(")[0].upper(), "TEXT")
                default_clause = ""
                if col.default is not None and getattr(col.default, "arg", None) is not None:
                    default_val = col.default.arg
                    if isinstance(default_val, str):
                        default_clause = f" DEFAULT '{default_val}'"
                    elif isinstance(default_val, bool):
                        default_clause = f" DEFAULT {1 if default_val else 0}"
                    elif isinstance(default_val, (int, float)):
                        default_clause = f" DEFAULT {default_val}"

                sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {sqlite_type}{default_clause}"
                print(f"Adding column: {table_name}.{col_name} ({sqlite_type}{default_clause})")
                cur.execute(sql)
                added_any = True

        conn.commit()
        conn.close()

    if added_any:
        print("\nDone — your database now matches what the app expects.")
        print("Your existing books, members, and loan records were not touched.")
    else:
        print("Nothing to do — your database already matches the app's expected schema.")


if __name__ == "__main__":
    main()
