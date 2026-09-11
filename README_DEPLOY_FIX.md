# Fixing "Add Member" and "Import Excel" not working

I tested both directly in this code and they work correctly — which
means your running server is on an older version that doesn't have them
yet. This zip is the complete, current set of files. Two ways to fix it,
pick whichever matches how you're running the app:

## If you run it with START_LIBRARY.bat (Python installed directly)

1. Replace `app.py`, `models.py`, and everything inside `templates/`
   and `static/` with the files in this zip.
2. **Do not touch `database.db`** — your real students/books stay exactly
   as they are.
3. Restart `START_LIBRARY.bat`.
4. Reload the page in your browser (a full refresh, not just clicking
   around) so it picks up the new `index.html`/`app.js`.

## If you run it as the built .exe

Replacing files in the source folder does **nothing** by itself — the
running `.exe` reads its own bundled copy. You must rebuild:

1. Put these files into your **source** project folder (the one with
   `BUILD_EXE.bat` — not the `dist\MontessoriDigitalLibrary` folder).
2. Run `BUILD_EXE.bat` again.
3. Copy `database.db` and the `uploads` folder from your **currently
   running** server into the **newly built** folder.
4. Run the new `.exe`.

## Either way — get your 2,655 students in immediately, right now

You don't need to wait for the redeploy to work perfectly before your
data is in. **`import_students.py`** is a standalone script that adds
students straight into `database.db`, independent of whether the "Import
Excel" button in the web app is working yet:

1. Close the running app first (`START_LIBRARY.bat` window, or the `.exe`).
2. Put `import_students.py` in the same folder as `app.py`/`database.db`.
3. Put your Excel file in that same folder.
4. Command prompt in that folder:
   ```
   venv\Scripts\activate
   python import_students.py "Nandakishore.xlsx"
   ```
   (use whatever your file is actually named)
5. It prints a summary — how many were added, how many already existed.
6. Start the app again — your students are already there.

Safe to run more than once on the same file — students already present
(matched by Admission Number) are skipped, never duplicated.

## How to confirm it actually worked this time

After redeploying, open **Members** and check for the **⬆ Import Excel**
button next to Export — if you see it, you're on the current version and
both features will work from the UI going forward.
