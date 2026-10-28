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
before copying anything.

Order of work: check the target database is empty, upload the files, and only
then copy the database rows (in one transaction). A failure at any step
therefore leaves the target database empty, and the script can simply be run
again; re-uploading the same file keys just overwrites them.

This file deliberately does not import app.py: importing it would run
init_db() against DATABASE_URL and start the cloud backup scheduler. Both
databases are read through plain SQLAlchemy reflection instead.

Rows left dangling on the PC are repaired, not fatal: SQLite never enforced
foreign keys, so deleting a member or a digital item there could leave behind
a student login (users.member_id) or bookmarks/favorites/read history pointing
at it, and Postgres refuses those. A nullable reference is cleared and the row
kept; a required one means the row is skipped. Each case is printed.

Any failure prints one "Migration failed: ..." line and exits 1; add --verbose
for the full traceback.

Not copied: uploads/backups/. Those are old SQLite backup files made on the
PC; on the cloud deployment backups are JSON files that the app writes itself.
"""
import argparse
import mimetypes
import os
import sys
import traceback

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

# How the progress line names the parent a dropped row pointed at, keyed by
# (table, parent table). Anything not listed falls back to "a deleted row in
# <parent>".
PARENT_NAMES = {
    ("users", "members"): "a deleted member",
    ("bookmarks", "online_books"): "a deleted digital item",
    ("favorites", "online_books"): "a deleted digital item",
    ("read_history", "online_books"): "a deleted digital item",
}

_TRUE_WORDS = {"1", "true", "t", "yes", "y"}
_FALSE_WORDS = {"0", "false", "f", "no", "n", ""}


def _to_bool(value):
    """SQLite stores booleans as 0/1 (sometimes as text); Postgres BOOLEAN
    columns reject integers, so hand them a real bool. None stays None."""
    if value is None:
        return None
    if isinstance(value, str):
        # Text must be read as words first: int("False") raises, and falling
        # back to bool("False") would turn every non-empty string into True.
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        try:
            return float(word) != 0
        except ValueError:
            raise ValueError(f"cannot read {value!r} as a true/false value") from None
    return bool(value)


def _fix_path(value):
    return value.replace("\\", "/") if isinstance(value, str) else value


def _sequence_reset_sql(dialect, table_name):
    """After copying rows with explicit ids, Postgres' id sequence still
    starts at 1 and the next insert would collide; make the next id MAX(id)+1
    (or 1 for an empty table: setval(..., false) means "not yet handed out")."""
    quoted = dialect.identifier_preparer.quote(table_name)
    literal = quoted.replace("'", "''")
    return (f"SELECT setval(pg_get_serial_sequence('{literal}', 'id'), "
            f"COALESCE(MAX(id), 0) + 1, false) FROM {quoted}")


def _has_integer_id(table):
    return "id" in table.c and isinstance(table.c.id.type, sa.Integer)


def _ensure_empty(engine, meta):
    # A brand-new database has no tables until the app has started once
    # against it. Without this check the copy would find nothing to copy into,
    # skip every table, and still report success.
    if "members" not in meta.tables:
        raise RuntimeError(
            "The target database has no SmartLib tables yet. Start the app once "
            "against this DATABASE_URL (so it creates them), then run this script again."
        )
    with engine.connect() as conn:
        occupied = [
            t.name for t in meta.sorted_tables
            if conn.execute(sa.select(sa.func.count()).select_from(t)).scalar()
        ]
    if occupied:
        raise RuntimeError(
            "Target database is not empty (tables with rows: " + ", ".join(occupied)
            + "). Run this before anyone completes /setup, against a freshly created database."
        )


def _sqlalchemy_database_uri(raw: str) -> str:
    """Same rewrite as app.py (duplicated: this script must not import app).
    "postgres://" / "postgresql://" -> "postgresql+psycopg2://", so SQLAlchemy
    2.1's default of psycopg v3 (not installed) never applies. Other URLs,
    including ones that already name a driver, are returned unchanged."""
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg2://" + raw[len(prefix):]
    return raw


def ensure_target_empty(target_url: str) -> None:
    """Raise RuntimeError ("not empty", naming the tables) if any table in the
    target database has rows. main() calls this before uploading files, so a
    non-empty database is reported before anything has been changed."""
    engine = sa.create_engine(_sqlalchemy_database_uri(target_url))
    try:
        meta = sa.MetaData()
        meta.reflect(bind=engine)
        _ensure_empty(engine, meta)
    finally:
        engine.dispose()


def copy_database(source_url: str, target_url: str) -> dict:
    """Copy every table the target has (and the source also has) across.

    Returns {table name: rows copied}. Raises RuntimeError, copying nothing,
    if any target table already contains rows.

    Rows whose foreign key points at a parent row that no longer exists are
    repaired rather than failing the copy (see _copy_table): SQLite never
    enforced foreign keys, so deleting a member or a digital item on the PC
    could leave such rows behind, but Postgres refuses them. What was changed
    is printed, one line per table.
    """
    source_engine = sa.create_engine(_sqlalchemy_database_uri(source_url))
    target_engine = sa.create_engine(_sqlalchemy_database_uri(target_url))
    try:
        source_meta = sa.MetaData()
        source_meta.reflect(bind=source_engine)
        target_meta = sa.MetaData()
        target_meta.reflect(bind=target_engine)

        # Check everything before writing anything.
        _ensure_empty(target_engine, target_meta)

        counts = {}
        repairs = []
        # One transaction: all tables are copied or none are.
        with source_engine.connect() as src, target_engine.begin() as dst:
            # sorted_tables puts parents before the tables that reference them.
            for table in target_meta.sorted_tables:
                source_table = source_meta.tables.get(table.name)
                if source_table is None:
                    print(f"  {table.name}: not in the source database, skipped")
                    continue
                try:
                    counts[table.name] = _copy_table(src, dst, source_meta, source_table, table, repairs)
                except Exception as exc:
                    exc.migration_table = table.name  # for main()'s one-line message
                    raise
            if target_engine.dialect.name == "postgresql":
                for table in target_meta.sorted_tables:
                    if table.name in counts and _has_integer_id(table):
                        dst.execute(sa.text(_sequence_reset_sql(target_engine.dialect, table.name)))
        if repairs:
            print("Some rows pointed at things that were deleted on the PC:")
            for line in repairs:
                print(line)
        return counts
    finally:
        source_engine.dispose()
        target_engine.dispose()


def _foreign_key_checks(src, source_meta, table, column_names):
    """One check per foreign key of the target table: which source columns
    hold the reference, the set of parent keys that exist in the source, and
    whether the reference may be cleared (all its columns nullable) or the row
    must be skipped. A key whose columns, or whose parent table, are missing
    from the source is not checked (the target would reject it anyway; that
    is a schema problem, not leftover rows)."""
    quote = src.dialect.identifier_preparer.quote
    checks = []
    for constraint in table.foreign_key_constraints:
        local = [e.parent.name for e in constraint.elements]
        remote = [e.column.name for e in constraint.elements]
        parent = constraint.referred_table
        parent_source = source_meta.tables.get(parent.name)
        if not all(name in column_names for name in local):
            continue
        if parent_source is None or not all(name in parent_source.c for name in remote):
            continue
        select = sa.text(f"SELECT {', '.join(quote(n) for n in remote)} FROM {quote(parent.name)}")
        keys = {tuple(row) for row in src.execute(select)}
        checks.append({
            "local": local,
            "keys": keys,
            "parent": parent.name,
            "clearable": all(table.c[name].nullable for name in local),
        })
    return checks


def _missing_column_defaults(source_table, table):
    """{column name: value} for columns the target has and the source lacks,
    where the model gives the column a plain Python-side default (for example
    read_history.progress_seconds, default=0, which a PC database from before
    that column existed does not have). Without this the new column would be
    NULL on every migrated row, while the app only ever writes 0 there.

    The target is read by reflection, and reflection does not carry
    Python-side defaults, so they are looked up on the model of the same name.
    Only a scalar default is used; a callable default (a date, a token) cannot
    be known here and the column stays NULL, as before. models.py needs no
    Flask app, so importing it does not break the "never import app.py" rule.
    """
    from models import db
    model_table = db.metadata.tables.get(table.name)
    if model_table is None:
        return {}
    defaults = {}
    for column in table.columns:
        if column.name in source_table.c or column.name not in model_table.c:
            continue
        default = model_table.c[column.name].default
        if default is not None and getattr(default, "is_scalar", False):
            defaults[column.name] = default.arg
    return defaults


def _copy_table(src, dst, source_meta, source_table, table, repairs) -> int:
    """Copy one table's rows. Rows with a dangling foreign key are repaired in
    memory as they pass through: a nullable reference (users.member_id) is set
    to NULL and the row kept; a required one (bookmarks.online_book_id) means
    the row is skipped. A line per table describing that goes onto repairs."""
    columns = [c for c in table.columns if c.name in source_table.c]
    if not columns:
        return 0
    names = {c.name for c in columns}
    # Columns only the target has, filled with the model's default (see above).
    filled = _missing_column_defaults(source_table, table)
    bool_columns = {c.name for c in columns if isinstance(c.type, sa.Boolean)}
    checks = _foreign_key_checks(src, source_meta, table, names)
    skipped = {}   # parent table -> rows skipped
    cleared = {}   # (parent table, column names) -> rows kept with the reference cleared
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
            record.update(filled)
            dangling = [
                check for check in checks
                if all(record[n] is not None for n in check["local"])
                and tuple(record[n] for n in check["local"]) not in check["keys"]
            ]
            required = next((check for check in dangling if not check["clearable"]), None)
            if required is not None:
                skipped[required["parent"]] = skipped.get(required["parent"], 0) + 1
                continue
            for check in dangling:
                for n in check["local"]:
                    record[n] = None
                key = (check["parent"], ", ".join(check["local"]))
                cleared[key] = cleared.get(key, 0) + 1
            batch.append(record)
        if batch:
            dst.execute(table.insert(), batch)
        copied += len(batch)

    for parent, n in skipped.items():
        what = PARENT_NAMES.get((table.name, parent), f"a deleted row in {parent}")
        repairs.append(f"  {table.name}: {n} {'row' if n == 1 else 'rows'} skipped (they pointed at {what})")
    for (parent, column_list), n in cleared.items():
        what = PARENT_NAMES.get((table.name, parent), f"a deleted row in {parent}")
        repairs.append(f"  {table.name}: {column_list} cleared on {n} {'row' if n == 1 else 'rows'} "
                       f"(they pointed at {what}; the rows themselves were kept)")
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


def _short_reason(exc) -> str:
    """First line of the underlying database error (str(exc.orig)) for
    SQLAlchemy errors, whose own text carries the whole SQL statement and
    the row's values; otherwise first line of the exception's message."""
    if isinstance(exc, sa.exc.DBAPIError) and exc.orig is not None:
        text = str(exc.orig)
    else:
        text = str(exc)
    lines = text.strip().splitlines()
    return lines[0].strip() if lines else type(exc).__name__


def main(argv=None, env=os.environ) -> int:
    parser = argparse.ArgumentParser(
        description="Copy a school PC's SQLite database and uploads folder into Neon and R2.")
    parser.add_argument("--sqlite", required=True, help="path to the PC's database.db")
    parser.add_argument("--uploads", required=True, help="path to the PC's uploads folder")
    parser.add_argument("--verbose", action="store_true",
                        help="print the full Python traceback if the migration fails")
    args = parser.parse_args(argv)

    try:
        return _run(args, env)
    except Exception as exc:
        # One readable line instead of a SQLAlchemy traceback full of SQL and
        # student data; --verbose is for whoever has to debug it.
        table = getattr(exc, "migration_table", None)
        prefix = f"Migration failed: {table}: " if table else "Migration failed: "
        print(prefix + _short_reason(exc), file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        return 1


def _run(args, env) -> int:
    database_url = (env.get("DATABASE_URL") or "").strip()
    if not database_url:
        print("DATABASE_URL is not set. Point it at the Neon database.", file=sys.stderr)
        return 2

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

    # Files first, database last: the database copy is the step that makes the
    # target non-empty, so if the upload fails the script can just be re-run.
    ensure_target_empty(database_url)
    print("Copying uploaded files (uploads/backups/ is skipped)...")
    files = copy_uploads(args.uploads, storage)
    print(f"  {files} files")
    print("Copying database rows...")
    counts = copy_database("sqlite:///" + os.path.abspath(args.sqlite), database_url)
    for name, n in counts.items():
        print(f"  {name}: {n} rows")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
