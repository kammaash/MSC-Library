# SmartLib 360 — Cloud Deployment Design

**Date:** 2026-10-28
**Status:** Agreed in conversation, ready for implementation planning

## Goal

Put SmartLib 360 online at `library.msceducation.net` with **every existing
feature kept** — the physical library manager (catalogue, issue/return,
fines, lost books, stock checks, periodicals, visitors, donations,
certificates, reports) and the digital library — while the school PC /
Windows `.exe` setup keeps working offline exactly as it does today.

## Hosting

| Piece | Service | Region |
|---|---|---|
| App (Flask, gunicorn) | Render web service | Singapore |
| Database | Neon Postgres (free tier) | `aws-ap-southeast-1` (Singapore) |
| Files (PDFs, videos, QR codes, barcodes, backups) | Cloudflare R2 | Location hint: Asia-Pacific |

`library.msceducation.net` CNAMEs to the Render service. GitHub Pages is
switched off for this repo.

## Decisions

1. **Keep the existing schema.** No rewrite to the generic
   users/assets/watch_history schema. All 20 tables stay. New columns only:
   - `members.email`, `members.entra_oid` — Microsoft sign-in
   - `online_books.grade_target` — show students books for their class
   - `read_history.progress_seconds` — resume video/audio where left off
2. **Files go through one storage layer.** When the four R2 environment
   variables are set, files live in R2; when none are set, files live in
   `uploads/` on local disk as today. A partial R2 configuration is a
   startup error. File references in the database stay as relative keys
   such as `uploads/pdfs/abc_book.pdf`.
3. **Temporary links.** In R2 mode, students and staff never get a
   permanent file URL. Opening an item asks the server for a link, which
   checks the login first and returns an R2 presigned URL valid for
   **30 minutes**.
4. **"Need more time".** For video and audio, a bar appears **5 minutes
   before** the link expires: "Your access ends in 5 minutes. [Need more
   time]". Clicking it re-checks the login and swaps in a fresh 30-minute
   link without losing the playback position. No cap on extensions. If
   the login has expired, the student is asked to sign in again. PDFs and
   EPUBs download fully on open, so they show no timer.
5. **Two student sign-in methods.**
   - **Microsoft (Entra ID):** "Sign in with Microsoft" button. Only
     accounts from the school's own tenant are accepted. The Microsoft
     account is matched to a library card by `members.email`; after the
     first sign-in the card is bound to the account's permanent ID
     (`entra_oid`). Microsoft checks the password — the app never sees it.
   - **School login ID + PIN:** the existing admission number + date of
     birth + PIN flow, unchanged.
   - Staff keep username + password. (Microsoft sign-in for staff is a
     possible follow-up, not in scope.)
6. **Backups.** On Postgres, the admin Backup button and the daily
   automatic backup write a JSON export of every table to
   `uploads/backups/` in storage (R2). Neon's own point-in-time restore is
   the first line of recovery; the JSON export is the school's own copy.
7. **Moving existing data.** A one-off script copies a school PC's
   `database.db` and `uploads/` folder into Neon and R2, for when the
   library PC holds real data.

## Out of scope

- Direct browser-to-R2 uploads for large videos (uploads go through the
  server; revisit only if large uploads time out).
- Microsoft sign-in for staff.
- OCR and Word/PowerPoint previews on Render (Tesseract and LibreOffice
  aren't installed there; the app already skips both quietly).
