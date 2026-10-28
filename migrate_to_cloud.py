"""
One-off: move a school PC's SmartLib 360 data into the cloud deployment.

    python migrate_to_cloud.py --sqlite PATH/database.db --uploads PATH/uploads

Copies every row of the PC's SQLite database into the Postgres database named
by DATABASE_URL (Neon), and every file under the PC's uploads folder into the
R2 bucket named by the R2_* variables. Run it once per school PC.

PRECONDITION: run the app once against the Neon DATABASE_URL first, so that
init_db() creates the (empty) tables — and do it BEFORE anyone completes
/setup, because /setup writes rows and this script refuses to copy into a
database that already has data. If any target table has rows the script stops
before copying anything. The copy runs in one transaction, so a failure
part-way leaves the target empty and the script can simply be run again.

This file deliberately does not import app.py: importing it would run
init_db() against DATABASE_URL and start the cloud backup scheduler. Both
databases are read through plain SQLAlchemy reflection instead.

Not copied: uploads/backups/. Those are old SQLite backup files made on the
PC; on the cloud deployment backups are JSON files that the app writes itself.
"""
import argparse
import mimetypes
import os
import sys

import sqlalchemy as sa

from storage import R2Storage, get_storage

# Columns holding a storage key. Windows PCs wrote them with backslashes
# (uploads\pdfs\x.pdf); the cloud only ever sees forward slashes.
PATH_COLUMNS = {"barcode_path", "qr_path", "pdf_path", "preview_pdf_path"}

# Rows are inserted this many at a time, so a big table is never held in
# memory all at once.
BATCH_SIZE = 500

# Subfolder of uploads/ that is not copied (see the module docstring).
SKIPPED_UPLOAD_DIR = "backups"


def _to_bool(value):
    """SQLite stores booleans as 0/1 (sometimes as text); Postgres BOOLEAN
    columns reject integers, so hand them a real bool. None stays None."""
    if value is None:
        return None
    if isinstance(value, (int, str)):
        try:
            return bool(int(value))
        except ValueError:
            pass
    return bool(value)


def _fix_path(value):
    return value.replace("\\", "/") if isinstance(value, str) else value


def _sequence_reset_sql(dialect, table_name):
    """After copying rows with explicit ids, Postgres' id sequence still
    starts at 1 and the next insert would collide; move it past MAX(id)."""
    quoted = dialect.identifier_preparer.quote(table_name)
    literal = quoted.replace("'", "''")
    return (f"SELECT setval(pg_get_serial_sequence('{literal}', 'id'), "
            f"COALESCE(MAX(id), 1)) FROM {quoted}")


def _has_integer_id(table):
    return "id" in table.c and isinstance(table.c.id.type, sa.Integer)


def copy_database(source_url: str, target_url: str) -> dict:
    """Copy every table the target has (and the source also has) across.

    Returns {table name: rows copied}. Raises RuntimeError, copying nothing,
    if any target table already contains rows.
    """
    source_engine = sa.create_engine(source_url)
    target_engine = sa.create_engine(target_url)
    try:
        source_meta = sa.MetaData()
        source_meta.reflect(bind=source_engine)
        target_meta = sa.MetaData()
        target_meta.reflect(bind=target_engine)

        # Check everything before writing anything.
        with target_engine.connect() as conn:
            occupied = [
                t.name for t in target_meta.sorted_tables
                if conn.execute(sa.select(sa.func.count()).select_from(t)).scalar()
            ]
        if occupied:
            raise RuntimeError(
                "Target database is not empty (tables with rows: " + ", ".join(occupied)
                + "). Run this before anyone completes /setup, against a freshly created database."
            )

        counts = {}
        # One transaction: all tables are copied or none are.
        with source_engine.connect() as src, target_engine.begin() as dst:
            # sorted_tables puts parents before the tables that reference them.
            for table in target_meta.sorted_tables:
                source_table = source_meta.tables.get(table.name)
                if source_table is None:
                    print(f"  {table.name}: not in the source database, skipped")
                    continue
                counts[table.name] = _copy_table(src, dst, source_table, table)
            if target_engine.dialect.name == "postgresql":
                for table in target_meta.sorted_tables:
                    if table.name in counts and _has_integer_id(table):
                        dst.execute(sa.text(_sequence_reset_sql(target_engine.dialect, table.name)))
        return counts
    finally:
        source_engine.dispose()
        target_engine.dispose()


def _copy_table(src, dst, source_table, table) -> int:
    columns = [c for c in table.columns if c.name in source_table.c]
    if not columns:
        return 0
    bool_columns = {c.name for c in columns if isinstance(c.type, sa.Boolean)}
    # Read with plain SQL so the source's raw stored values come back as-is,
    # without SQLite's reflected types second-guessing them.
    quote = src.dialect.identifier_preparer.quote
    select = sa.text(f"SELECT {', '.join(quote(c.name) for c in columns)} FROM {quote(table.name)}")
    result = src.execute(select)

    copied = 0
    while True:
        rows = result.fetchmany(BATCH_SIZE)
        if not rows:
            break
        batch = []
        for row in rows:
            record = {}
            for column, value in zip(columns, row):
                if column.name in PATH_COLUMNS:
                    value = _fix_path(value)
                elif column.name in bool_columns:
                    value = _to_bool(value)
                record[column.name] = value
            batch.append(record)
        dst.execute(table.insert(), batch)
        copied += len(batch)
    return copied


def copy_uploads(uploads_dir: str, target) -> int:
    """Upload every file under uploads_dir to target as uploads/<relative path>.

    Dotfiles (.gitkeep, .DS_Store, ...) and the uploads/backups/ folder (old
    SQLite backups, not wanted in the bucket) are skipped. Returns the number
    of files uploaded.
    """
    count = 0
    for root, dirs, files in os.walk(uploads_dir):
        if os.path.abspath(root) == os.path.abspath(uploads_dir):
            dirs[:] = [d for d in dirs if d != SKIPPED_UPLOAD_DIR]
        dirs.sort()
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, uploads_dir).replace(os.sep, "/")
            content_type = mimetypes.guess_type(name)[0]
            target.save_file(path, f"uploads/{rel}", content_type)
            count += 1
    return count


def main(argv=None, env=os.environ) -> int:
    parser = argparse.ArgumentParser(
        description="Copy a school PC's SQLite database and uploads folder into Neon and R2.")
    parser.add_argument("--sqlite", required=True, help="path to the PC's database.db")
    parser.add_argument("--uploads", required=True, help="path to the PC's uploads folder")
    args = parser.parse_args(argv)

    database_url = (env.get("DATABASE_URL") or "").strip()
    if not database_url:
        print("DATABASE_URL is not set. Point it at the Neon database.", file=sys.stderr)
        return 2
    # Same rewrite app.py does: SQLAlchemy only knows "postgresql://".
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    try:
        storage = get_storage(os.path.dirname(os.path.abspath(__file__)), env)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not isinstance(storage, R2Storage):
        print("The R2_* variables (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, "
              "R2_BUCKET_NAME) are not set. Point them at the R2 bucket.", file=sys.stderr)
        return 2

    if not os.path.isfile(args.sqlite):
        print(f"SQLite database not found: {args.sqlite}", file=sys.stderr)
        return 2
    if not os.path.isdir(args.uploads):
        print(f"Uploads folder not found: {args.uploads}", file=sys.stderr)
        return 2

    print("Copying database rows...")
    counts = copy_database("sqlite:///" + os.path.abspath(args.sqlite), database_url)
    for name, n in counts.items():
        print(f"  {name}: {n} rows")
    print("Copying uploaded files (uploads/backups/ is skipped)...")
    files = copy_uploads(args.uploads, storage)
    print(f"  {files} files")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
