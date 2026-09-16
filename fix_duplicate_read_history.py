"""
fix_duplicate_read_history.py — one-time cleanup for the points-inflation bug
------------------------------------------------------------------
Run this ONCE, after updating to the fixed app.py/models.py, and before
relying on point totals again. It does two things, in order:

  1. Finds any existing duplicate rows in read_history — the same
     student having more than one row for the same digital item, which
     is exactly what the refresh/race-condition bug could have created
     before this fix — and merges each group into a single row (keeping
     the earliest first_opened, the latest last_opened, and the summed
     open_count as a "how many times opened" statistic), deleting the
     extras. Since points are computed from the row COUNT, this step is
     what actually corrects any already-inflated point totals.

  2. Adds the unique index that stops this from ever happening again —
     the same protection models.py now declares, applied directly to
     your EXISTING database, since SQLAlchemy's db.create_all() only
     creates brand-new tables and won't retrofit a constraint onto a
     table that already exists.

Safe to run more than once — both steps check for their own work being
already done and skip it if so.

Works against whichever database the app is actually configured for:
local SQLite (the usual case) or Postgres if DATABASE_URL is set in the
environment when you run this, same as app.py itself.
"""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")
DATABASE_URL = os.environ.get("DATABASE_URL")


def _get_connection():
    if DATABASE_URL:
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(DB_PATH)


def _placeholder():
    return "%s" if DATABASE_URL else "?"


def main():
    if not DATABASE_URL and not os.path.exists(DB_PATH):
        print(f"No database.db found at {DB_PATH} — nothing to fix yet.")
        return

    conn = _get_connection()
    cur = conn.cursor()
    ph = _placeholder()

    print("Step 1: looking for duplicate read_history rows...")
    cur.execute("""
        SELECT member_id, online_book_id, COUNT(*) as n
        FROM read_history
        GROUP BY member_id, online_book_id
        HAVING COUNT(*) > 1
    """)
    duplicate_groups = cur.fetchall()

    if not duplicate_groups:
        print("  No duplicates found — points were not previously inflated by this bug.")
    else:
        print(f"  Found {len(duplicate_groups)} student+item pair(s) with duplicate rows. Merging...")
        for member_id, online_book_id, count in duplicate_groups:
            cur.execute(
                f"SELECT id, first_opened, last_opened, open_count FROM read_history "
                f"WHERE member_id = {ph} AND online_book_id = {ph} ORDER BY id",
                (member_id, online_book_id),
            )
            rows = cur.fetchall()
            keep_id = rows[0][0]
            merged_first = min(r[1] for r in rows if r[1])
            merged_last = max(r[2] for r in rows if r[2])
            merged_opens = sum((r[3] or 1) for r in rows)
            cur.execute(
                f"UPDATE read_history SET first_opened={ph}, last_opened={ph}, open_count={ph} WHERE id={ph}",
                (merged_first, merged_last, merged_opens, keep_id),
            )
            extra_ids = [r[0] for r in rows[1:]]
            for eid in extra_ids:
                cur.execute(f"DELETE FROM read_history WHERE id={ph}", (eid,))
            print(f"  Merged {count} rows into 1 for member {member_id}, item {online_book_id} "
                  f"(this student's points for this item now count once, not {count} times).")
        conn.commit()
        print(f"  Done — merged {len(duplicate_groups)} duplicate group(s).")

    print("\nStep 2: adding the unique index (if not already present)...")
    try:
        if DATABASE_URL:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_read_history_member_book
                ON read_history (member_id, online_book_id)
            """)
        else:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_read_history_member_book
                ON read_history (member_id, online_book_id)
            """)
        conn.commit()
        print("  Unique index is in place — duplicate rows can no longer be created.")
    except Exception as e:
        print(f"  Could not create the index: {e}")
        print("  If this says something already exists, that's fine — it means a previous")
        print("  run of this script already added it.")

    conn.close()
    print("\nDone. Point totals now reflect each digital item read exactly once per student.")


if __name__ == "__main__":
    main()
