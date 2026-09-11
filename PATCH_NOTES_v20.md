# Complete update: new logo, librarian permissions, user editing

Since my working files reset between sessions, this is the **full
project** — replace everything except `database.db`. The self-healing
schema check adds the new `phone` column to Users automatically on
next startup — no manual migration step.

## New official logo

Replaced the Golden Jubilee crest with your new MSLRMS emblem — same
filename (`logo_jubilee.png`), so it updates automatically everywhere
it was used: the masthead, the login page, and Digital Certificates.
Tested and confirmed rendering correctly on all three.

## Title & Digital Library button (from last round, now on login.html too)

The two-tier title (descriptive name + big "MSLRMS" badge) is now on
**both** the main app and the login page — I only had the main app
file last time. Same for the Digital Library button's enhanced glow.

## Librarian book-deletion permission

Librarians can now delete Catalog books, not just Admin — the Delete
button now shows for both roles, and the server enforces it the same
way (not just a hidden button).

## Edit Users (Manage Users, Admin only)

Every account now has an **Edit** button alongside Remove:
- Change display name
- Change mobile number (new field — Users didn't have one before)
- Change role (Librarian / Admin / Viewer)
- Reset password (optional — leave blank to keep the current one)

Username itself isn't editable (it's how they sign in, and other
records may reference it) — delete and recreate if it genuinely needs
to change. The "at least one Admin must remain" safety check still
applies when changing someone's role away from Admin.

## Beautiful Welcome Screen (new)

The login page now opens with a full-screen welcome/splash screen
before the sign-in form: both logos with a gentle floating animation,
the school name, the big MSLRMS badge, the tagline, and an "Enter
MSLRMS →" button. Clicking it fades smoothly into the actual sign-in
form (with a "← Back" option to return). Purely a frontend addition —
no change to how signing in itself works.

## Import Students from Excel (new)

Members now has an **⬆ Import Excel** button next to Export, matching
how Catalog already works. It auto-detects columns the same way book
import does — tested directly against a real 2,655-row admission
register with headers like `AdmissionNo`, `rollno`, `DOB`, `class`,
`section`, `Mobile` — all 2,655 imported cleanly with zero errors, DOB
parsed correctly from "20 Apr 2012"-style dates, class+section combined
automatically, and a QR code generated for every student. Re-uploading
the same file is safe — students already in the system (matched by
Admission Number) are skipped, never duplicated.

## Tested before shipping

Real student data import (2,655 rows, verified twice — once for
creation, once to confirm re-import safely skips duplicates), plus
everything from this session end-to-end: librarian deleting a book,
creating/editing a user with a phone number and password reset,
certificate generation with the new logo, and the full existing
regression suite (1,115-book import, catalog, members, issue/return,
registers, reports) — all still passing.
