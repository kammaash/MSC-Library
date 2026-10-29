# SmartLib 360

Smart Library & Learning Resource Management System for the Montessori School
Committee (MSC) library, Kurnool. Intended to be served at
`library.msceducation.net`.

Python Flask app with SQLAlchemy. Uses SQLite (`database.db`) locally, or
Postgres when `DATABASE_URL` is set.

## Run locally

    pip install -r requirements.txt
    python app.py

Then open http://localhost:5000. On Windows, `START_LIBRARY.bat` /
`RUN_SmartLib360.bat` do the same.

On first run with a fresh database the app redirects to the Setup Wizard
(`/setup`) to create the admin account — there is no default login.

## Deployment

- **Render + Neon + Cloudflare R2 (internet, `library.msceducation.net`):**
  follow [deploy/DEPLOY_TO_RENDER.md](deploy/DEPLOY_TO_RENDER.md). The app
  runs on Render (the Start Command, the same text as `Procfile`, starts
  gunicorn with one worker), the database
  is Neon Postgres (`DATABASE_URL`), uploaded files and backups live in an R2
  bucket (`R2_*`), and students can sign in with their school Microsoft
  account (`ENTRA_*`). The guide has the full step-by-step instructions, the
  table of every environment variable, a staging checklist that must pass
  before the domain is switched over, and the recovery notes.
- **VPS:** see [deploy/DEPLOY_TO_VPS.md](deploy/DEPLOY_TO_VPS.md) for the
  Nginx + systemd setup.
- **Windows .exe:** `BUILD_EXE.bat` builds a standalone executable with
  PyInstaller (`MontessoriDigitalLibrary.spec`).

With none of these variables set the app runs exactly as a school-PC install:
SQLite database, files under `uploads/` and backups under `backups/`.

## Environment variables

Every variable the code reads. On an offline school PC you normally set none
of them. For the cloud values and how to get them, use the table in
[deploy/DEPLOY_TO_RENDER.md](deploy/DEPLOY_TO_RENDER.md#environment-variables).

| Name | What it does | When it is needed |
| --- | --- | --- |
| `DATABASE_URL` | Use Postgres (Neon) instead of the SQLite `database.db`. Also read by `migrate_to_cloud.py` and `fix_duplicate_read_history.py` | Cloud. Unset on an offline PC |
| `LIBRARY_SECRET_KEY` | Session signing key. If unset, a random key is created and saved in `instance_secret.key` next to the database | Cloud (a disk file is lost on Render). Optional on a PC |
| `LIBRARY_FORCE_HTTPS_COOKIES` | `1` = Secure cookies, redirect to HTTPS and HSTS. Also switches on the `LIBRARY_SETUP_TOKEN` rule | Cloud or any internet-facing install. Leave unset on a LAN |
| `LIBRARY_SETUP_TOKEN` | With `LIBRARY_FORCE_HTTPS_COOKIES=1`, `/setup` only works as `/setup?token=<value>`; setup is refused if this is unset. Not needed when the flag is off | Cloud. Not needed on a PC |
| `LIBRARY_TRUST_PROXY` | Number of proxies in front of the app (`3` on Render). The client address is read that many entries from the right of `X-Forwarded-For`, so sign-in lockouts use the real client address | Cloud (Render). Do not set on a PC |
| `PUBLIC_BASE_URL` | Public https address of the site, no trailing slash. Used in QR codes and as the Microsoft redirect address | Cloud; required with `ENTRA_*` |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` | Store uploads and backups in a Cloudflare R2 bucket instead of the `uploads/` and `backups/` folders. All four or none | Cloud |
| `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET` | "Sign in with Microsoft" for students. All three or none (and `PUBLIC_BASE_URL`) | Optional (cloud) |
| `LIBRARY_LINK_TTL_SECONDS` | How long a link to a PDF, video or audio file stays valid; default 1800. Lower it only to test "Need more time" | Optional |
| `LIBRARY_BIND_HOST` | Address the built-in server listens on; default `0.0.0.0`. Set `127.0.0.1` behind Nginx on a VPS | Optional (VPS) |
| `LIBRARY_DATA_DIR` | Folder for the database, uploads and key instead of the app folder. Used by the automated tests | Optional |

Two more appear in the Render guide but are not read by the app itself:
`PYTHON_VERSION` is read by Render, and `TZ` (`Asia/Kolkata`) is read by the
operating system so that dates and fines use Indian time. A half-filled
`R2_*` or `ENTRA_*` group stops the app at startup with an error.

## Never commit

This repo is public. `database.db` (student and library data),
`instance_secret.key` (session signing key), `uploads/`, `backups/`, `reports/` and
`logs/` are gitignored — keep them that way.
