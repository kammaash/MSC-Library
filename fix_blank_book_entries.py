"""
One-time cleanup: removes catalog entries left over from a failed/old
import (blank title, or an auto-generated BK#### accession number with no
author). This is a command-line fallback — the same cleanup is now also
available inside the app itself: log in as Admin, open the Catalog tab,
and a red "Clean Up N Incomplete" button appears automatically whenever
there are entries like this. Use whichever is more convenient.

This script prints diagnostic information (which database file it's using,
how many books it found in total, and a sample of the first few rows) so
that if nothing gets removed, you can see exactly why.

Usage:
    cd library_app
    venv\\Scripts\\activate          (Windows)   or   source venv/bin/activate (Mac/Linux)
    python fix_blank_book_entries.py
"""
import os
import re
import sys

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

from app import app                    # noqa: E402
from models import db, Book, Issue     # noqa: E402

AUTO_ACC_PATTERN = re.compile(r"^BK\d+$", re.IGNORECASE)


def is_blank(value):
    return value is None or str(value).strip() in ("", "None", "nan")


def looks_broken(book):
    if is_blank(book.title):
        return True
    if AUTO_ACC_PATTERN.match(book.accession_no or "") and is_blank(book.author):
        return True
    return False


def main():
    with app.app_context():
        db_path = app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")
        print(f"Using database file: {db_path}")
        print(f"  (exists: {os.path.exists(db_path)})")

        all_books = Book.query.all()
        print(f"Total books currently in the catalog: {len(all_books)}")

        if not all_books:
            print("The catalog is empty — nothing to clean up.")
            return

        print("Sample of the first 3 books currently stored:")
        for b in all_books[:3]:
            print(f"   accession_no={b.accession_no!r}  title={b.title!r}  author={b.author!r}")

        broken = [b for b in all_books if looks_broken(b)]

        if not broken:
            print("\nNo blank/broken book entries matched. If your Catalog still shows")
            print("blank titles on screen, please share the 'Sample of the first 3 books'")
            print("lines above so the exact stored values can be checked.")
            return

        print(f"\nFound {len(broken)} blank/broken book entr(y/ies).")

        blocked, removable = [], []
        for b in broken:
            has_history = Issue.query.filter_by(accession_no=b.accession_no).first() is not None
            (blocked if has_history else removable).append(b)

        if blocked:
            print(f"Skipping {len(blocked)} entr(y/ies) that already have loan history "
                  f"— please review these manually in the Catalog tab:")
            for b in blocked:
                print(f"   - {b.accession_no}")

        removed = 0
        for b in removable:
            barcode_path = os.path.join(BASE_DIR, "uploads", "barcodes", f"{b.accession_no}.png")
            if os.path.exists(barcode_path):
                try:
                    os.remove(barcode_path)
                except OSError:
                    pass
            db.session.delete(b)
            removed += 1

        db.session.commit()
        print(f"Removed {removed} blank/broken book entr(y/ies). Database cleaned up.")
        print("You can now go to Catalog -> Import Excel and upload "
              "books_import_ready.xlsx again.")


if __name__ == "__main__":
    main()
