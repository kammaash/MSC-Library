# MSLRMS Hardening Update — What Changed

## Sign-in limits and PIN set-up codes (October 2026)

- **Creating a PIN now needs a set-up code from the librarian.** Before,
  a correct admission number and date of birth on a card with no PIN let
  anyone create the PIN and take the account. Now the student also types
  a one-time 6-character code. Staff issue codes in Members: **Issue PIN
  set-up codes** (every student without a PIN or code, optionally one
  class), **Issue code** / **New code** on one card, and **Reset PIN** or
  approving a "Forgot PIN" request now also issues one and shows it. The
  code is shown in the member list's PIN column (admin/librarian only) and
  stops working once used. This applies to offline (LAN) installs too.
- **Three sign-in limits instead of one** (`security.py`): 5 failures from
  one address for one username lock that pair for 15 minutes (as before);
  10 failures on one account from any addresses lock it — a student card
  stays locked until staff click **Unlock** (or issue a code / reset the
  PIN), a staff account unlocks after an hour; 40 failures from one
  address across all usernames block that address for 15 minutes. The
  "Forgot PIN" request counts towards the same account and address limits.

---

## Deployment hardening (October 2026)

- **`LIBRARY_SETUP_TOKEN`** (new env var). With
  `LIBRARY_FORCE_HTTPS_COOKIES=1`, `/setup` only works as
  `/setup?token=<value>` and is refused while the variable is unset, so a
  stranger who reaches a fresh database first cannot make themselves admin.
  Wrong tokens count towards the sign-in limits. LAN installs are unchanged.
- **`/healthz`** answers `ok` without touching the database, before setup
  too. An uptime service pings it during school hours to keep a Render
  Free instance from sleeping (and signing everyone out on waking) without
  keeping Neon's Free-plan compute awake. See section 10 of
  `deploy/DEPLOY_TO_RENDER.md`.
- **`LIBRARY_TRUST_PROXY` is now the number of proxies** in front of the
  app, and Render needs **`3`**, not `1`. Render passes each request
  through three proxies; with `1` the app read the address of a Render edge
  server that many students share, so the per-address sign-in limits would
  have counted the whole school together (measured on HungerHunt, which
  runs on the same platform). `1` still means one proxy. A value that is not
  a whole number from 0 to 10 stops the app at startup.
- **The app refuses to start** when imported by gunicorn with
  `LIBRARY_FORCE_HTTPS_COOKIES=1` but without `LIBRARY_TRUST_PROXY`:
  behind Render's proxy every visitor would share one address and the
  sign-in limits would lock out the whole school. `python app.py` (the VPS
  guide's waitress setup) is not affected.
- **Content-Security-Policy** on every page when
  `LIBRARY_FORCE_HTTPS_COOKIES=1`: scripts only from the app itself; files
  also from the R2 bucket's host. The login, setup and change-password
  scripts moved to `static/js/`, and no page uses inline `onclick=` handlers
  any more.
- **Backups moved to `backups/`**, next to `uploads/` instead of inside it.
  An existing PC install's `uploads/backups/` files are moved there
  automatically on first start. `/uploads/` now serves only `qrcodes/`,
  `barcodes/` and `pdfs/`; backups are admin downloads only.
- Over-long text (a name, rack number, title… longer than its database
  column) is now refused with a message naming the field instead of a
  server error on Postgres; Excel imports skip such rows and count them.
- Students can open only their own member QR code (and digital items'
  codes); staff see all of them as before.
- Large digital-library uploads are written to disk once (via
  `upload_tmp/` next to `uploads/`), not copied from the system temp folder.

---

## Behaviour changes in the cloud-deployment branch

These differ from the `main` branch and can surprise staff or students.

- **Deleting a member or a digital item now also deletes the students'
  records for it.** Deleting a digital item (`delete_online_book`, admin
  only) removes every student's bookmarks, favourites and read history for
  that item. Deleting a member (`delete_member`) removes that member's
  bookmarks, favourites, read history, digital-library visits and PIN-reset
  requests, plus their student sign-in account (loans, fines, lost-book and
  certificate records are kept). Postgres refuses to delete a row that is
  still referenced, so this is needed on the cloud; it now applies to
  offline installs too. Achievement points count read history (5 points per
  item read) and library visits, so after a digital item is deleted every
  student who had read it loses those points and their reading count falls.
- **Backup list order.** `/api/backup/list` (View Backups) now sorts
  newest first by the date and time in the file name. Before, it sorted by
  name alone, which put every `auto_backup_` file ahead of every `backup_`
  file whatever its date. Files in the backups folder that do not match the
  `backup_` / `auto_backup_` naming are listed last.
- **Name of the downloaded "Office" original.** Staff downloading the
  original Word or PowerPoint file of a digital item get `<title>.<type>`
  with control characters (such as line breaks), double quotes and
  backslashes removed from the title (`_download_name`). Before, the title
  was used as typed, so a title containing a quote or backslash gave a
  different file name on an offline PC. Other characters, including
  non-English text, are kept. An empty title becomes `download`.
- **Registers, reports and exports are staff-only.** The member, issue and
  fine lists (`/api/members`, `/api/issues`, `/api/fines`), all
  `/api/reports/...` endpoints and `/api/export/<entity>` now allow only
  admin, librarian and viewer. The lost-books, periodicals, visitors and
  donations registers apply the same rule to reading; adding to them still
  needs admin or librarian. A student who calls any of these gets a 403
  "Forbidden". Before, any signed-in user, students included, could read
  them. Student pages use their own `/api/student/...` endpoints and are
  not affected.

---

## Internet-Facing Deployment Support (August 2026)

Added to support running SmartLib360 on a public subdomain (e.g.
`library.msceducation.net`) instead of only the school LAN, so students
can reach it from home.

- **`LIBRARY_BIND_HOST`** (env var, default `0.0.0.0` — unchanged for
  every existing LAN install): lets an internet-facing deployment bind
  Waitress to `127.0.0.1` only, so it's reachable exclusively through a
  reverse proxy (Nginx) and never directly from the internet.
- **`LIBRARY_FORCE_HTTPS_COOKIES`** (env var, default off): when set to
  `1`, this now switches on HTTPS enforcement everywhere it matters, not
  just the cookie:
  - Session cookies are marked `Secure` (HTTPS-only), as before.
  - Any request that reaches the app over plain HTTP is redirected
    (301) to the HTTPS version of the same URL — previously this relied
    entirely on Nginx getting the redirect right; now the app enforces
    it itself too, so a misconfigured or later-reverted Nginx config
    doesn't silently start serving login pages over plain HTTP.
  - An HSTS header (`Strict-Transport-Security`) is sent on every
    response, telling browsers to remember to always use HTTPS for this
    site for a year — closes the gap a redirect alone leaves open on
    someone's very first visit to a bookmarked/typed `http://` link.
  - `X-Content-Type-Options`, `X-Frame-Options`, and `Referrer-Policy`
    headers are added as well — cheap, standard hardening that only
    matters once real students are browsing from arbitrary home
    networks and devices, not a school LAN.
  - Waitress is also told to trust `X-Forwarded-Proto`/`-For`/`-Host`
    from `127.0.0.1` only (see `app.py`, bottom) — required for any of
    the above to actually see "https" from Nginx at all; Waitress
    silently ignores forwarded headers by default as an anti-spoofing
    measure.
  Left off by default because a plain-HTTP LAN install would silently
  break login if any of this were on unconditionally.
- Session cookies now always set `SameSite=Lax` regardless of
  deployment (safe everywhere, no opt-in needed).
- Added `deploy/` — a systemd service file, an Nginx reverse-proxy
  config, and `DEPLOY_TO_VPS.md`, a full walkthrough for standing this
  up on a small cloud server behind HTTPS.

**Read `deploy/DEPLOY_TO_VPS.md` in full before pointing a public
address at a real school's data** — in particular the section on doing
the student PIN rollout before the address is shared, not after.

---

## Student PIN — Second Factor for Internet-Facing Login (August 2026)

Added ahead of exposing student login to the open internet (previously
LAN-only). Date of birth alone identifies a child but isn't a real
secret — it's often semi-public — so it's no longer sufficient by
itself once the login page is reachable from anywhere.

- Every student now sets a 4-6 digit PIN the first time they log in
  under this system. DOB is still checked first (unchanged); once
  correct, the app checks whether a PIN exists yet and prompts
  accordingly — "create a PIN" on first login, "enter your PIN" on
  every login after that.
- PIN attempts count toward the same lockout as DOB attempts (5 wrong
  tries → 15-minute lock, persisted — see the rate-limiter fix from
  the previous round).
- Admin/Librarian can reset a student's PIN from Manage Members
  (`/api/members/<id>/reset-pin`) for a genuine "forgot my PIN" case —
  clears it so they're walked through creating a new one on next login.

**Known rollout gap, by design — read before going internet-facing:**
Right after this update, every existing student has no PIN set yet
(same as a brand-new install). Until a student sets their own PIN,
anyone who already knows their admission number and DOB can set the
PIN first and lock the real student out. Do the PIN rollout — every
current student logging in once to set their PIN — **while still
LAN-only**, before switching on any internet-facing access. The same
gap re-opens for one student any time their PIN is reset, so pair a
reset with the student setting a new PIN immediately, not left open
over a break.

---

## Certificates & Reports — Letterhead Fix and Redesign (August 2026)

**Real bug found and fixed:** the Certificate of Achievement PDF was
silently generating with *no logos at all* on real installs. The code
to draw both logos was already there, but it built the file path from
`BASE_DIR` (the `.exe`'s own folder) — and `static/images/` only
exists on disk at PyInstaller's temporary `sys._MEIPASS` folder when
running as a packaged `.exe`, which Flask's own static route already
accounted for correctly, but this one PDF generator didn't. The check
was wrapped in `if os.path.exists(...)`, so it just quietly skipped
both logos every time, with no error anywhere. Fixed by routing both
the certificate and the report generator through one shared,
bundle-aware `_logo_paths()` helper, so this can't silently regress
again in either place.

**Also fixed while in this code:** the Overdue and Fine Register
report headers showed a black replacement-box glyph instead of the ₹
symbol — the base PDF font doesn't include that character. Changed to
"Rs." in both headers.

**Certificate redesign:** school name is now bigger and bold, with a
font size that automatically shrinks just enough to never run under
either logo on a long school name. The library/product name
("Smart-Lib 360°") is now its own highlighted gold line, separated
from the school name by a thin rule.

**Report redesign (Issue Register, Overdue, Fine Register, Member
List, Stock Report):** all five now render with a full letterhead —
both logos, bigger bold school name (same long-name-safe auto-sizing
as the certificate), and the product name highlighted in gold —
repeated on every page of a multi-page report, not just the first.
Table headers are now bold, larger, and clearly distinguished from
the data rows with a gold accent rule, wider cell padding throughout,
and each report now shows a generation timestamp, a record count, and
a footer with who generated it and a page number.

---

## Multi-School Distribution Fixes (August 2026)

This round fixes issues found during review before packaging MSLRMS for
other schools to deploy independently, and prepares the codebase itself
(not just this one school's data) to be handed out as a clean release.

**Security fixes:**
- **Session secret key.** Every install was previously running on the
  literal fallback string `"change-this-secret-key"` because nothing
  ever set the `LIBRARY_SECRET_KEY` environment variable — meaning
  every school running this software shared the same publicly-visible
  key, which could be used to forge a valid admin session. Fixed: the
  app now generates a random key on first run and saves it to
  `instance_secret.key` next to `database.db`, unique per install.
  `LIBRARY_SECRET_KEY` still overrides it if you set one explicitly.
- **CSRF protection.** No form submission or API call was previously
  verified as having actually come from this app's own pages. Added a
  standard synchronizer-token check (`X-CSRF-Token` header, verified
  against the session) on every POST/PUT/DELETE/PATCH.
- **Rate limiter now persists.** Login lockouts (5 failed attempts →
  15-minute lock) were stored only in memory and reset on every server
  restart. They're now stored in a `login_rate_limits` table in
  `database.db`, so a restart no longer hands an attacker a clean slate.

**Reliability fixes:**
- **`requirements.txt` is now pinned** to specific, tested versions
  instead of "whatever's newest today" — a fresh install six months
  from now won't silently pull an untested major version and break.
- **Renewal cap added.** A single loan could previously be renewed an
  unlimited number of times. Capped at 2 renewals (`RENEWAL_LIMIT` in
  `app.py`) via a new `renewal_count` column — added automatically by
  the existing self-healing schema migration, no manual step needed.
- Removed a stale comment in `app.py` still referencing the old
  `admin/admin123` default login, which hasn't existed since the Setup
  Wizard was introduced.

**Distribution cleanup:**
- `database.db` and everything in `uploads/` were reset to empty —
  this release ships with no real student, staff, or book data. Every
  new install starts at the Setup Wizard, exactly like a brand-new
  school.
- Removed working notes and one-off patch instructions from a previous
  live-upgrade session (`note updated.txt`, `README_DEPLOY_FIX.md`,
  `MSLRMS_Update_Hardened.zip`, `MSLRMS_Update_Feature_Guide.docx`) —
  these were specific to merging this update into one already-running
  install, not relevant to a fresh deployment.

**Not done in this round** (flagged for a future pass, not silently
skipped): splitting `app.py` into smaller modules, an automated test
suite, an audit log of admin actions, and SMS/email due-date reminders
using the `parent_phone` field that's already collected but unused.

---

This update implements the first four items from the productization
roadmap: **forced password change on first use, automated daily
backups, login rate limiting, and a first-run setup wizard** (which
replaces the old hardcoded default accounts, the main packaging risk
of item 4).

## IMPORTANT — one thing I could not do

The uploaded ZIP contained `app.py`, `templates/`, and `static/`, but
**not `models.py`**. Every new feature here was deliberately built to
need zero changes to `models.py` — the new state (setup-complete flag,
school name, "must change password" per user) lives in two small
tables (`app_security`, `user_security_flags`) that `security.py`
creates for itself inside the same `database.db`, using raw SQLite
rather than the SQLAlchemy models. That means you can drop these files
straight into your real project next to your real `models.py` and
nothing about your existing tables needs to change.

I could not run the *whole* real application end-to-end (no
`models.py` = no real Book/Issue/Member tables to query), so I built a
minimal stand-in model file, ran the full setup wizard → login →
forced password change → rate-limit → auto-backup flow against it with
Flask's test client, and confirmed every step behaves correctly (see
the transcript in this conversation). The only failure I hit was an
unrelated dashboard query needing real columns my stand-in model
didn't have — expected, and not part of this change. **Please still
smoke-test this against your real database.db before rolling it out
to a live school**, ideally on a copy first.

## Files changed / added

| File | Change |
|---|---|
| `app.py` | Modified — see below |
| `security.py` | **New** — setup-wizard state, forced-password-change flag, password strength check, login rate limiter |
| `backup_scheduler.py` | **New** — background daily backup + pruning thread |
| `templates/setup.html` | **New** — first-run setup wizard page |
| `templates/change_password.html` | **New** — forced password-change page |
| `templates/login.html` | Modified — redirects to `/change-password` when the server says it's required |

## What changed in `app.py`, and why

1. **No more hardcoded default accounts.** `init_db()` no longer
   creates `admin/admin123`, `librarian/lib123`, `viewer/view123`. On a
   database with zero users, every request is redirected to `/setup`,
   a one-time wizard where the school picks their own admin username
   and password (min. 8 characters, letters + numbers, common
   passwords rejected). **Upgrade-safe**: if a database already has
   users when this version first runs (i.e. an existing school
   upgrading), setup is automatically marked complete so they are
   *not* forced back through the wizard.

2. **Forced password change.** When an admin creates a new staff
   account or resets someone's password via Manage Users, that account
   is flagged `must_change_password`. On next login, every route
   except the change-password page/API and logout is blocked (403 for
   API calls, redirect for pages) until they set their own password.

3. **Login rate limiting.** In-memory limiter keyed by IP + username:
   5 failed attempts within 15 minutes locks that combination out for
   15 minutes — including against the *correct* password, which is
   what actually stops brute-forcing. No new dependency; resets are
   in-memory, so a server restart clears any active lockouts (fine for
   this deployment model — there's no way to distribute the limiter
   across restarts without adding a real dependency, and a restart on
   a school PC is already a meaningful barrier for an attacker).

4. **Automated daily backups.** A daemon thread (`backup_scheduler.py`)
   starts when the app starts: first backup ~2 minutes after startup,
   then every 24 hours for as long as the process runs, writing to the
   same `backups/` folder the existing manual "Backup Now"
   button already uses. Backups older than 60 days are pruned
   automatically so the folder doesn't grow forever. Both numbers are
   constants at the top of the file if you want to tune them.

## Still worth doing before wide rollout (not built here)

- The rate limiter is in-memory/per-process — fine for this
  single-server deployment model, but worth knowing if you ever move
  to multiple app instances behind a load balancer.
- No password-reset-via-email flow exists (there's no email
  configured anywhere in the app) — a locked-out admin still needs
  physical/desk access to reset another account.
- The installer/packaging piece (turning this into a polished
  double-click `.exe` installer rather than a `python app.py` folder)
  is a packaging task (e.g. PyInstaller + Inno Setup) that needs your
  actual Windows build environment — I can help write that spec file
  if useful, but couldn't test it here.
