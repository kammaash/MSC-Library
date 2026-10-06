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

- **Render:** `Procfile` starts `gunicorn app:app`. Set `DATABASE_URL`
  (Postgres) and `LIBRARY_SECRET_KEY` in the service environment.
- **VPS:** see [deploy/DEPLOY_TO_VPS.md](deploy/DEPLOY_TO_VPS.md) for the
  Nginx + systemd setup.
- **Windows .exe:** `BUILD_EXE.bat` builds a standalone executable with
  PyInstaller (`MontessoriDigitalLibrary.spec`).

## Never commit

This repo is public. `database.db` (student and library data),
`instance_secret.key` (session signing key), `uploads/`, `reports/` and
`logs/` are gitignored — keep them that way.
