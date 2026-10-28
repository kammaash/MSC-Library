# Deploying SmartLib 360 to Render (Neon + Cloudflare R2)

This guide puts SmartLib 360 on the internet at
`https://library.msceducation.net`. It is written for the school's IT person.
Follow the steps in order.

| Part | Service | What it holds |
| --- | --- | --- |
| App | Render (Singapore) | Runs the website |
| Database | Neon (`aws-ap-southeast-1`) | Books, members, history, accounts |
| Files | Cloudflare R2 (Asia-Pacific) | PDFs, videos, barcodes, QR codes, backups |
| Student sign-in | Microsoft Entra ID | "Sign in with Microsoft" for students |

Production DNS (step 7) is changed only after the staging check (step 6)
passes. Do not skip ahead to the domain.

The offline school-PC install and the VPS install
([DEPLOY_TO_VPS.md](DEPLOY_TO_VPS.md)) are unaffected. Nothing below applies
to them.

## Environment variables

You will collect these values in steps 1 to 3 and enter them in Render in
step 4. Never put any of these in the repository or in an email; the
repository is public.

| Name | Example / where it comes from | Required? |
| --- | --- | --- |
| `PYTHON_VERSION` | `3.12.7` | Yes (Render) |
| `DATABASE_URL` | Neon pooled connection string, starts `postgresql://` or `postgres://` (both work) | Yes |
| `LIBRARY_SECRET_KEY` | Run `python -c "import secrets; print(secrets.token_hex(32))"` and paste the output. Keep the same value forever; changing it signs everyone out | Yes |
| `LIBRARY_FORCE_HTTPS_COOKIES` | `1`. Secure cookies, redirect to HTTPS, HSTS | Yes |
| `LIBRARY_TRUST_PROXY` | `1`. Makes login lockouts use the student's real IP address instead of Render's proxy. Do not set this on a school-PC install | Yes |
| `PUBLIC_BASE_URL` | `https://library.msceducation.net` (no trailing slash). Used in QR codes and as the Microsoft redirect address | Yes |
| `R2_ACCOUNT_ID` | Cloudflare account ID (R2 overview page) | Yes |
| `R2_ACCESS_KEY_ID` | From the R2 API token (step 2) | Yes |
| `R2_SECRET_ACCESS_KEY` | From the R2 API token (step 2); shown only once | Yes |
| `R2_BUCKET_NAME` | `smartlib360-files` | Yes |
| `ENTRA_TENANT_ID` | Directory (tenant) ID **GUID** from the app's Overview page in Entra (step 3) | Yes, for Microsoft sign-in |
| `ENTRA_CLIENT_ID` | Application (client) ID from the same page | Yes, for Microsoft sign-in |
| `ENTRA_CLIENT_SECRET` | The secret **Value** created in step 3 | Yes, for Microsoft sign-in |
| `LIBRARY_LINK_TTL_SECONDS` | `120`. For testing "Need more time" only; remove afterwards. Default is 1800 | No |

All-or-nothing rules: the four `R2_*` variables must all be set or all be
absent; the three `ENTRA_*` variables must all be set or all be absent, and
they also need `PUBLIC_BASE_URL`. A partial set stops the app at startup with
an error naming the problem. Check the Render logs if the service will not
start.

## 1. Neon (database)

1. Sign in to Neon and create a project. Choose the region
   `aws-ap-southeast-1` (Singapore).
2. Open the project's connection details and copy the **pooled** connection
   string (the one with connection pooling turned on).
3. Keep it for step 4 as `DATABASE_URL`.

## 2. Cloudflare R2 (files)

1. In the Cloudflare dashboard, open R2 and create a bucket named
   `smartlib360-files`. Set the location hint to Asia-Pacific.
2. Create an R2 API token with **Object Read & Write** permission, limited to
   this bucket only. Copy the Access Key ID and Secret Access Key now; the
   secret is not shown again. Note your Account ID from the R2 overview page.
3. Add a CORS rule to the bucket's settings:
   - Allowed origin: `https://library.msceducation.net`
   - Allowed methods: `GET`, `HEAD`
   - Allowed headers: `Range`
   - Exposed headers: `Content-Length`, `Content-Range`, `Accept-Ranges`

   The book reader and video seeking need this. Without it, videos play but
   cannot be skipped forward and EPUBs may not open.

## 3. Microsoft Entra (school IT)

This is done by whoever administers the school's Microsoft 365 tenant.

1. In the Microsoft Entra admin center, go to App registrations and choose
   New registration.
2. Name it (for example "SmartLib 360"). For supported account types choose
   **single tenant** (this directory only).
3. Under Redirect URI choose the **Web** platform and enter
   `https://library.msceducation.net/auth/microsoft/callback`. Register.
4. On the app's Overview page copy the **Application (client) ID** and the
   **Directory (tenant) ID**.
   - `ENTRA_TENANT_ID` must be the tenant ID **GUID** (looks like
     `0b1c2d3e-...`), **not** a domain name such as `school.edu`. With a
     domain name every student will see "Please sign in with your school
     Microsoft account."
5. Under Certificates & secrets, create a new client secret. Copy its
   **Value** immediately (it is hidden afterwards). **Write down the expiry
   date and set a calendar reminder** for a few weeks before it. When the
   secret expires, Microsoft sign-in stops working until a new secret is
   created here and pasted into Render as `ENTRA_CLIENT_SECRET`. Staff and PIN
   logins are not affected.
6. The default `User.Read` permission is enough. No other permissions or
   admin consent are needed.

### How students are matched (read before enabling)

A student's first Microsoft sign-in is matched to their library card by the
**School Email** on the card. The librarian enters it on the member, or
imports it through the Excel import (column named `email` or `school email`).
After the first match the card is bound to that Microsoft account.

This is only safe if the school's IT controls what the email address is:

- Student email addresses/UPNs must be **assigned by school IT**, not chosen
  by the student.
- **Never invite a guest account using a student's school address.** A guest
  with that address could claim the student's library card.

The account picker is always shown, so students on a shared computer can
choose their own account.

## 4. Render (the app)

1. In Render, create a **Web Service** from the GitHub repository
   `kammaash/MSC-Library`, branch `main`. Region: **Singapore**.
2. Plan: **Starter** is recommended for school hours. The **Free** plan works
   for trying it out, but it sleeps after a period of no visits; each wake-up
   restarts the app, which signs everyone out (login sessions are tied to the
   running process), and the first page load is slow.
3. Build command: `pip install -r requirements.txt`. Start command: take it
   from the repository's `Procfile`
   (`gunicorn app:app --workers 1 --threads 8 --timeout 120`). Keep
   `--workers 1`; the app must run as a single process.
4. Add the environment variables from the table above:
   `PYTHON_VERSION`, `DATABASE_URL`, `LIBRARY_SECRET_KEY`,
   `LIBRARY_FORCE_HTTPS_COOKIES`, `LIBRARY_TRUST_PROXY`, `PUBLIC_BASE_URL`,
   the four `R2_*`, and the three `ENTRA_*`.
5. Deploy. The first start creates the empty database tables in Neon.

Do not open `/setup` yet if you need step 5 (migration).

## 5. Move the library PC's existing data (only if it has real data)

Skip this step for a fresh start.

Run this on a computer that has the library PC's `database.db` and its
`uploads/` folder (copy them off the PC first; do not change the originals).
It must happen after the first deploy in step 4 (so the tables exist) and
**before** anyone completes `/setup` (the script refuses to copy into a
database that already holds data).

1. Install the dependencies (`pip install -r requirements.txt`).
2. Set the same `DATABASE_URL` and four `R2_*` variables in your shell.
3. Run:

       python migrate_to_cloud.py --sqlite PATH/database.db --uploads PATH/uploads

It uploads the files first, then copies the rows in one transaction. If it
fails part-way the database is still empty and you can run it again. Old
`uploads/backups/` files are not copied. Afterwards, sign in with the PC's
existing admin account instead of using `/setup`.

## 6. Before go-live: staging check

Do this on a separate Render service, ideally on a branch deploy, using a
Neon **branch** (a copy of the project) and a **separate R2 bucket** with the
same CORS rule, so real data is not touched. For the staging service use its
own `*.onrender.com` address as `PUBLIC_BASE_URL`, and add that address as a
second Redirect URI (`.../auth/microsoft/callback`) in the Entra app. Tick
each item in order.

1. **New columns exist.** After the first deploy against Neon, open Neon's SQL
   editor and run:

       SELECT column_name FROM information_schema.columns
       WHERE table_name='members' AND column_name IN ('email','entra_oid');

   It must return **2 rows**. The automatic column migration has not been run
   against real Postgres before, so this check matters. If you get fewer, stop
   and report it.
2. Open `/setup` and create the admin account.
3. Add a book. Its barcode image must display.
4. Add a member with a School Email. The member's QR code must display.
5. Upload a PDF and an MP4 as digital books. This confirms real uploads to R2
   work (the app configures boto3 not to send the extra checksums that R2
   rejects). If the upload fails or the file does not appear in the bucket,
   check the Render logs and report the error.
6. Sign in as a student with the PIN and open both files.
   - **Video seeking:** drag the video to a later point. It must jump there
     and keep playing. If it restarts or cannot skip, the R2 CORS rule
     (`Range` header, exposed headers) is wrong.
7. **Microsoft sign-in:** with a real school test student account whose email
   matches a library card, choose Sign in with Microsoft. It must land on the
   student's page. Sign out and in again; it must still work (now matched to
   the bound account). Try an account from another organisation; it must be
   refused.
8. **Need more time:** temporarily add `LIBRARY_LINK_TTL_SECONDS=120` in
   Render and redeploy. Open the video as a student. A "Need more time"
   bar must appear after about 60 seconds. Click it and confirm the video
   carries on from the same place. **Then remove the
   variable and redeploy.**
9. **Backup:** as admin open Backup & Restore and press Backup Now, then
   View Backups. A `.json` file must be listed for download, and it must
   open.
10. **Redeploy** the service (Manual Deploy) and confirm the uploaded PDF and
    MP4 still open. This proves files and data survive restarts.
11. Delete the staging service, Neon branch, bucket and the extra Redirect
    URI when finished.

## 7. Domain

1. In the msceducation.net DNS, change the `library` CNAME from
   `kammaash.github.io` to the Render `*.onrender.com` host shown for your
   service.
   - If the domain's DNS is on Cloudflare, set this record to **DNS only**
     (grey cloud), not proxied. A proxied record interferes with Render's
     certificate.
2. In Render, add the custom domain `library.msceducation.net` to the service
   and wait until its certificate shows as issued.
3. Turn off GitHub Pages for the repository: Settings, Pages, Unpublish (or
   `gh api -X DELETE repos/kammaash/MSC-Library/pages`).

## 8. First run

Open `https://library.msceducation.net/setup` and create the admin account.
Skip this if you migrated data in step 5. Confirm the address shows the login
page with a valid certificate (padlock).

## 9. Recovery

- First line of defence: Neon's point-in-time restore window. Restore from the
  Neon dashboard if data is damaged or deleted.
- Admin, Backup Now creates a JSON export of the data in R2 (under
  `uploads/backups/`) and the app also makes one automatically each day. The
  files can be downloaded from View Backups.
- Download one backup **monthly** and keep it somewhere outside these cloud
  accounts (for example a school drive). A cloud account problem would
  otherwise take the data and the backups together.

## Troubleshooting

- **Every student gets "Please sign in with your school Microsoft account":**
  `ENTRA_TENANT_ID` is probably a domain name, or the account is from a
  different organisation. Use the tenant ID GUID.
- **Microsoft sign-in suddenly stopped working for everyone:** the client
  secret has probably expired (see step 3).
- **Everyone is signed out at random times:** the Render Free plan has
  restarted after sleeping. Use Starter.
- **Service will not start and logs mention R2 or Entra settings:** one of the
  all-or-nothing groups is only partly filled in.
