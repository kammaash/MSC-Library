"""
Montessori Senior Secondary School - Library Management System
Database models (Flask-SQLAlchemy)
"""
from datetime import date

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()

# --------------------------------------------------------------------------
# Config constants
# --------------------------------------------------------------------------
LOAN_DAYS = 14        # default loan period, in days
FINE_PER_DAY = 2       # ₹ fine charged per day overdue


# --------------------------------------------------------------------------
# Users (login accounts: admin / librarian / viewer / student)
# --------------------------------------------------------------------------
class User(db.Model, UserMixin):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    name = db.Column(db.String(150), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="librarian")  # admin | librarian | viewer | student
    password_hash = db.Column(db.String(255), nullable=False, default="")
    phone = db.Column(db.String(20))
    # Only set for auto-provisioned student logins — links back to their Member record.
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=True)


# --------------------------------------------------------------------------
# Books (Catalog)
# --------------------------------------------------------------------------
class Book(db.Model):
    __tablename__ = "books"
    id = db.Column(db.Integer, primary_key=True)
    accession_no = db.Column(db.String(50), unique=True, nullable=False)
    isbn = db.Column(db.String(50))
    title = db.Column(db.String(300), nullable=False)
    author = db.Column(db.String(200))
    publisher = db.Column(db.String(200))
    edition = db.Column(db.String(50))
    subject = db.Column(db.String(100))
    class_name = db.Column(db.String(50))
    rack_no = db.Column(db.String(50))
    copies = db.Column(db.Integer, default=1)
    status = db.Column(db.String(20), default="Available")  # Available | Lost (Issued is computed)
    barcode_path = db.Column(db.String(255))

    def computed_status(self):
        if self.status == "Lost":
            return "Lost"
        active_count = Issue.query.filter_by(accession_no=self.accession_no, status="Active").count()
        return "Issued" if active_count >= (self.copies or 1) else "Available"

    def to_dict(self):
        return {
            "id": self.id,
            "accession_no": self.accession_no,
            "isbn": self.isbn,
            "title": self.title,
            "author": self.author,
            "publisher": self.publisher,
            "edition": self.edition,
            "subject": self.subject,
            "class_name": self.class_name,
            "rack_no": self.rack_no,
            "copies": self.copies,
            "status": self.computed_status(),
            "barcode_path": self.barcode_path,
        }


# --------------------------------------------------------------------------
# Members (students / staff)
# --------------------------------------------------------------------------
class Member(db.Model):
    __tablename__ = "members"
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(150), nullable=False)
    type = db.Column(db.String(20), default="Student")  # Student | Staff
    class_dept = db.Column(db.String(80))
    phone = db.Column(db.String(20))
    parent_phone = db.Column(db.String(20))
    admission_no = db.Column(db.String(50))
    dob = db.Column(db.String(10))  # ISO yyyy-mm-dd; also doubles as the student login password (ddmmyyyy)
    qr_path = db.Column(db.String(255))
    # A student-chosen PIN, required in addition to DOB once set. NULL
    # until the student's first login under this system, at which point
    # they're prompted to create one (see /login). This exists because
    # DOB alone identifies a child but isn't a real secret — it's often
    # semi-public — so it shouldn't be the only thing standing between
    # an internet-facing login page and a student's account.
    pin_hash = db.Column(db.String(255))
    # School Microsoft account address, stored lowercased. Used to match a
    # Microsoft sign-in to a Member. Uniqueness is enforced in application
    # code, not by the database, so adding the column to an existing
    # database stays a plain ALTER TABLE ... ADD COLUMN.
    email = db.Column(db.String(254))
    # Entra ID object id ("oid" claim), recorded on a student's first
    # Microsoft sign-in so later sign-ins match on a stable identifier
    # rather than an email address that can be renamed.
    entra_oid = db.Column(db.String(64))

    def to_dict(self):
        active_loans = Issue.query.filter_by(member_id=self.member_id, status="Active").count()
        return {
            "id": self.id,
            "member_id": self.member_id,
            "name": self.name,
            "type": self.type,
            "class_dept": self.class_dept,
            "pin_set": bool(self.pin_hash),
            "phone": self.phone,
            "parent_phone": self.parent_phone,
            "admission_no": self.admission_no,
            "dob": self.dob,
            "email": self.email,
            "ms_linked": bool(self.entra_oid),
            "qr_path": self.qr_path,
            "active_loans": active_loans,
        }


# --------------------------------------------------------------------------
# Issues (loans)
# --------------------------------------------------------------------------
class Issue(db.Model):
    __tablename__ = "issues"
    id = db.Column(db.Integer, primary_key=True)
    accession_no = db.Column(db.String(50), nullable=False)
    member_id = db.Column(db.String(30), nullable=False)  # matches Member.member_id
    issue_date = db.Column(db.String(10), nullable=False)
    due_date = db.Column(db.String(10), nullable=False)
    return_date = db.Column(db.String(10))
    fine = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default="Active")  # Active | Returned
    # How many times this loan has been renewed — capped in the /renew
    # route (see RENEWAL_LIMIT in app.py) so a single loan can't be
    # extended indefinitely and effectively vanish from circulation.
    renewal_count = db.Column(db.Integer, default=0)

    def computed_status(self):
        if self.status == "Returned":
            return "Returned"
        try:
            if date.fromisoformat(self.due_date) < date.today():
                return "Overdue"
        except (TypeError, ValueError):
            pass
        return "Active"

    def current_fine(self):
        if self.status == "Returned":
            return self.fine or 0
        try:
            late_days = (date.today() - date.fromisoformat(self.due_date)).days
        except (TypeError, ValueError):
            return 0
        return late_days * FINE_PER_DAY if late_days > 0 else 0

    def to_dict(self):
        return {
            "id": self.id,
            "accession_no": self.accession_no,
            "member_id": self.member_id,
            "issue_date": self.issue_date,
            "due_date": self.due_date,
            "return_date": self.return_date,
            "fine": self.current_fine() if self.status == "Active" else (self.fine or 0),
            "status": self.computed_status(),
            "renewal_count": self.renewal_count or 0,
        }


# --------------------------------------------------------------------------
# Fine payments
# --------------------------------------------------------------------------
class FinePayment(db.Model):
    __tablename__ = "fine_payments"
    id = db.Column(db.Integer, primary_key=True)
    issue_id = db.Column(db.Integer, db.ForeignKey("issues.id"))
    member_id = db.Column(db.String(30))
    amount = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default="Pending")  # Pending | Paid
    paid_date = db.Column(db.String(10))


# --------------------------------------------------------------------------
# Lost books
# --------------------------------------------------------------------------
class LostBook(db.Model):
    __tablename__ = "lost_books"
    id = db.Column(db.Integer, primary_key=True)
    accession_no = db.Column(db.String(50), nullable=False)
    member_id = db.Column(db.String(30))
    lost_date = db.Column(db.String(10))
    cost = db.Column(db.Integer, default=0)
    remarks = db.Column(db.Text)


# --------------------------------------------------------------------------
# Stock verification
# --------------------------------------------------------------------------
class StockCheck(db.Model):
    __tablename__ = "stock_checks"
    id = db.Column(db.Integer, primary_key=True)
    accession_no = db.Column(db.String(50), nullable=False)
    check_date = db.Column(db.String(10))
    found_status = db.Column(db.String(30))  # Present | Missing | Damaged
    remarks = db.Column(db.Text)


# --------------------------------------------------------------------------
# Newspaper / magazine register
# --------------------------------------------------------------------------
class Periodical(db.Model):
    __tablename__ = "periodicals"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    type = db.Column(db.String(30), default="Newspaper")  # Newspaper | Magazine
    entry_date = db.Column(db.String(10))
    remarks = db.Column(db.Text)


# --------------------------------------------------------------------------
# Visitor register
# --------------------------------------------------------------------------
class Visitor(db.Model):
    __tablename__ = "visitors"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    purpose = db.Column(db.String(200))
    visit_time = db.Column(db.String(30))
    check_out_time = db.Column(db.String(30))


# --------------------------------------------------------------------------
# Book donations
# --------------------------------------------------------------------------
class Donation(db.Model):
    __tablename__ = "donations"
    id = db.Column(db.Integer, primary_key=True)
    donor_name = db.Column(db.String(150), nullable=False)
    book_title = db.Column(db.String(300), nullable=False)
    quantity = db.Column(db.Integer, default=1)
    donation_date = db.Column(db.String(10))


# --------------------------------------------------------------------------
# Digital Library (multi-format repository: PDF / EPUB / DOC / PPT / audio / video)
# --------------------------------------------------------------------------
class OnlineBook(db.Model):
    __tablename__ = "online_books"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    author = db.Column(db.String(200))
    subject = db.Column(db.String(100))  # academic subject — Maths, Physics, Chemistry, Computer Science...
    category = db.Column(db.String(100))  # genre — Fiction, Comic, Biography, Reference, Non-Fiction...
    pdf_path = db.Column(db.String(255), nullable=False)  # kept name for backward-compat; holds ANY file type now
    file_type = db.Column(db.String(20), default="pdf")   # pdf | epub | doc | docx | ppt | pptx | mp4 | mp3 | m4a | wav
    file_size_kb = db.Column(db.Integer)
    # Extracted text (native PDF/DOCX/PPTX text, or OCR text for scanned pages) — powers Content/OCR/Smart Search.
    content_text = db.Column(db.Text)
    ocr_applied = db.Column(db.Boolean, default=False)
    qr_path = db.Column(db.String(255))
    added_date = db.Column(db.String(10))
    # If set, a Word/PowerPoint file was auto-converted to PDF so it can be
    # read in-browser like a normal PDF, instead of forcing a download.
    # Only populated when LibreOffice is installed on the host PC — see docs.
    preview_pdf_path = db.Column(db.String(255))
    # School grade (1-12) this item is meant for; NULL means every class.
    grade_target = db.Column(db.Integer)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "author": self.author,
            "subject": self.subject,
            "category": self.category,
            "has_preview": bool(self.preview_pdf_path),
            "file_type": self.file_type,
            "file_size_kb": self.file_size_kb,
            "ocr_applied": self.ocr_applied,
            "qr_path": self.qr_path,
            "has_content_text": bool(self.content_text),
            "grade_target": self.grade_target,
        }


# --------------------------------------------------------------------------
# Student engagement — bookmarks, favorites, reading history, certificates
# --------------------------------------------------------------------------
class Bookmark(db.Model):
    """A digital item a student saved to read later."""
    __tablename__ = "bookmarks"
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String(30), nullable=False)  # matches Member.member_id
    online_book_id = db.Column(db.Integer, db.ForeignKey("online_books.id"), nullable=False)
    added_date = db.Column(db.String(10))


class Favorite(db.Model):
    """A digital item a student starred/liked."""
    __tablename__ = "favorites"
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String(30), nullable=False)
    online_book_id = db.Column(db.Integer, db.ForeignKey("online_books.id"), nullable=False)
    added_date = db.Column(db.String(10))


class ReadHistory(db.Model):
    """
    One row per (student, digital item) ever opened — updated, not
    re-inserted, on repeat reads, so this doubles as both "Recently Read"
    (sort by last_opened) and a read-count for points/leaderboards.

    The unique constraint matters beyond just preventing accidental
    duplicates: it's what makes _log_read_history's "first open" check in
    app.py safe under concurrent requests (e.g. a video's HTTP Range
    requests, or a fast double-refresh) — without it, two near-
    simultaneous requests could each see "no row yet" and both insert
    one, permanently inflating that student's points by an extra +5.
    """
    __tablename__ = "read_history"
    __table_args__ = (
        db.UniqueConstraint("member_id", "online_book_id", name="uq_read_history_member_book"),
    )
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String(30), nullable=False)
    online_book_id = db.Column(db.Integer, db.ForeignKey("online_books.id"), nullable=False)
    first_opened = db.Column(db.String(10))
    last_opened = db.Column(db.String(10))
    open_count = db.Column(db.Integer, default=1)
    # Where the student got to in a video/audio item, so it can resume.
    progress_seconds = db.Column(db.Integer, default=0)


class Certificate(db.Model):
    """A digital certificate awarded to a student — logged so it can be re-downloaded and so Admin has a history of what's been given out."""
    __tablename__ = "certificates"
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String(30), nullable=False)
    title = db.Column(db.String(150), nullable=False)  # e.g. "Top Reader of the Month — July 2026"
    awarded_date = db.Column(db.String(10))
    issued_by = db.Column(db.String(150))  # name of the staff member who awarded it


class DigitalLibraryVisit(db.Model):
    """One row per (student, day) that they opened the Digital Library — a small, real reason to award points just for showing up and browsing, not only for finishing a book."""
    __tablename__ = "digital_library_visits"
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String(30), nullable=False)
    visit_date = db.Column(db.String(10))


# --------------------------------------------------------------------------
# Self-service "Forgot my PIN" requests
# --------------------------------------------------------------------------
class PinResetRequest(db.Model):
    """
    A student-initiated request to clear their PIN, submitted from the
    login page after they re-prove admission no. + DOB (the same check
    used at login). Deliberately NOT auto-approved: DOB + admission
    number are exactly the two pieces of info this reset is meant to
    protect against (see the /login and /reset-pin comments in app.py)
    — someone who already knows both could otherwise "recover" a PIN
    that was never theirs. Instead this just raises a flag for a
    librarian/admin, who can approve it in a couple of clicks once
    they've actually looked at who's asking (in person, by calling the
    parent number on file, etc.) — same real-world check they'd do if
    the student walked up to the desk, just without the walk.
    """
    __tablename__ = "pin_reset_requests"
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String(30), nullable=False)  # matches Member.member_id
    status = db.Column(db.String(20), default="Pending")  # Pending | Approved | Denied
    requested_at = db.Column(db.String(20))
    reviewed_by = db.Column(db.String(150))
    reviewed_at = db.Column(db.String(20))
