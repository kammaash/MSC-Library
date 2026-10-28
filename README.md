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
SQLite database and files under `uploads/`.

## Never commit

This repo is public. `database.db` (student and library data),
`instance_secret.key` (session signing key), `uploads/`, `reports/` and
`logs/` are gitignored — keep them that way.
