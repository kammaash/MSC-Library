"""
Montessori Digital Library — Import Students directly into database.db

WHAT THIS DOES
Adds every student from an Excel register straight into your live
database.db, without needing the "Import Excel" button in the web app
to be working. Safe to run even if your app.py is an older version —
this script only touches the database, not the running web server.

Existing students are matched by Admission Number and skipped (never
duplicated or overwritten) — safe to run more than once, including on
the same file.

HOW TO RUN
1. Put this file (import_students.py) in the SAME folder as app.py,
   models.py, and database.db.
2. Put your Excel file in that same folder too.
3. Open a command prompt in that folder and run:
     venv\\Scripts\\activate
     python import_students.py "Nandakishore.xlsx"
   (If you didn't rename it, use whatever the file is actually called.)
4. It will print a summary when done — how many were added, how many
   already existed, how many rows had a problem.

WHAT COLUMNS IT UNDERSTANDS
Same flexible matching as the Import Excel button: AdmissionNo/Admission
No/Admission Number, rollno/Roll No, Name/Student Name, DOB/Date of
Birth, class/Class, section/Section, Mobile/Phone/Contact. It doesn't
matter which exact wording your spreadsheet uses, or which sheet the
data is on if there's more than one — it finds the right one automatically.

STOP THE APP FIRST
Close START_LIBRARY.bat (or the .exe) before running this, so nothing
else is writing to database.db at the same time. Start it again
afterward as normal.
"""
import sys
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


def main():
    if len(sys.argv) < 2:
        print("Usage: python import_students.py <path-to-excel-file.xlsx>")
        sys.exit(1)
    excel_path = sys.argv[1]
    if not os.path.exists(excel_path):
        print(f"[ERROR] File not found: {excel_path}")
        sys.exit(1)

    try:
        from app import app
        from models import db, Member
    except Exception as e:
        print(f"[ERROR] Could not load app.py/models.py: {e}")
        print("Make sure this script is in the same folder as app.py and models.py.")
        sys.exit(1)

    import pandas as pd
    from datetime import datetime

    def normalize_header(h):
        return str(h).strip().lower().replace(".", "").replace("_", " ")

    ALIASES = {
        "member_id": ["member_id", "member id"],
        "admission_no": ["admissionno", "admission_no", "admission no", "admission number", "adm no", "adm no"],
        "roll_no": ["rollno", "roll_no", "roll no", "roll number"],
        "name": ["name", "student name", "full name", "studentname"],
        "dob": ["dob", "date of birth", "d o b"],
        "class_name": ["class", "class_name", "class name", "std", "grade"],
        "section": ["section", "sec"],
        "phone": ["mobile", "phone", "mobile no", "mobile number", "contact", "contact no", "contact number"],
        "parent_phone": ["parent phone", "parent mobile", "father mobile", "guardian phone", "parent contact"],
    }

    def map_columns(columns):
        normalized = {normalize_header(c): c for c in columns}
        mapping = {}
        for field, aliases in ALIASES.items():
            for alias in aliases:
                if alias in normalized:
                    mapping[field] = normalized[alias]
                    break
        return mapping

    def cell(row, colmap, field):
        col = colmap.get(field)
        if not col or col not in row:
            return None
        val = row[col]
        if pd.isna(val):
            return None
        text = str(val).strip()
        return text or None

    def parse_dob(text):
        if not text:
            return None
        text = str(text).strip()
        for fmt in ("%Y-%m-%d", "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
        return None

    print(f"Reading {excel_path} ...")
    excel = pd.ExcelFile(excel_path)

    best_sheet, best_map, best_score = None, {}, -1
    for sheet_name in excel.sheet_names:
        preview = excel.parse(sheet_name, nrows=0)
        colmap = map_columns(preview.columns)
        score = len(colmap) + (5 if "name" in colmap else 0) + (3 if "admission_no" in colmap else 0)
        if score > best_score:
            best_sheet, best_map, best_score = sheet_name, colmap, score

    if best_sheet is None or "name" not in best_map:
        print("[ERROR] Couldn't find a sheet with a recognisable Name column.")
        sys.exit(1)

    print(f"Using sheet: {best_sheet}")
    print(f"Matched columns: {best_map}")
    df = excel.parse(best_sheet)

    with app.app_context():
        created, skipped_duplicate, skipped_blank, skipped_error = 0, 0, 0, 0
        for row_num, row in df.iterrows():
            try:
                name = cell(row, best_map, "name")
                if not name:
                    skipped_blank += 1
                    continue

                admission_no = cell(row, best_map, "admission_no")
                if admission_no and Member.query.filter_by(admission_no=admission_no).first():
                    skipped_duplicate += 1
                    continue

                class_val = cell(row, best_map, "class_name")
                section_val = cell(row, best_map, "section")
                class_dept = f"{class_val}-{section_val}" if class_val and section_val else (class_val or section_val)

                dob_iso = parse_dob(cell(row, best_map, "dob"))
                phone = cell(row, best_map, "phone")
                parent_phone = cell(row, best_map, "parent_phone")

                # Auto-generate a member_id the same way the app does: MSS-M-0001, MSS-M-0002, ...
                count = Member.query.count() + 1
                member_id = f"MSS-M-{count:04d}"
                while Member.query.filter_by(member_id=member_id).first():
                    count += 1
                    member_id = f"MSS-M-{count:04d}"

                member = Member(
                    member_id=member_id, name=name, type="Student",
                    class_dept=class_dept, phone=phone, parent_phone=parent_phone,
                    admission_no=admission_no, dob=dob_iso,
                )
                db.session.add(member)
                db.session.flush()

                try:
                    import qrcode
                    qr_dir = os.path.join(BASE_DIR, "uploads", "qrcodes")
                    os.makedirs(qr_dir, exist_ok=True)
                    payload = f"MemberID:{member.member_id}|Name:{member.name}|Class:{member.class_dept or ''}"
                    img = qrcode.make(payload)
                    path = os.path.join(qr_dir, f"{member.member_id}.png")
                    img.save(path)
                    member.qr_path = os.path.relpath(path, BASE_DIR)
                except Exception:
                    pass  # QR code is a nice-to-have; never let it block the import

                created += 1
            except Exception as row_err:
                db.session.rollback()
                skipped_error += 1
                print(f"  Row {row_num + 2}: {row_err}")

        db.session.commit()

    print()
    print("=" * 60)
    print(f"  Added:               {created}")
    print(f"  Already existed:     {skipped_duplicate}  (matched by Admission No.)")
    print(f"  Skipped (no name):   {skipped_blank}")
    print(f"  Skipped (error):     {skipped_error}")
    print("=" * 60)
    print("Done. Start START_LIBRARY.bat (or the .exe) to see them in Members.")


if __name__ == "__main__":
    main()
