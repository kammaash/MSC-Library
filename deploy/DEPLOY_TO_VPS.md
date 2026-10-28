# Deploying SmartLib360 to library.msceducation.net (VPS)

This walks through putting SmartLib360 on its own small cloud server,
reachable at `library.msceducation.net`, completely separate from your
existing static website. Two files in this folder do most of the work
for you: `smartlib360.service` and `nginx_smartlib360.conf`.

**Read the "Before you go live" section at the end before switching this
on for real students — it's short, and it matters.**

---

## 0. What you're building

```
Student's browser  --HTTPS-->  Nginx (port 443, your cert)  --HTTP-->  Waitress (127.0.0.1:5000)  -->  SmartLib360
```

Nginx is the only thing exposed to the internet. It handles HTTPS and
hands requests to the app, which only listens on the server's own
loopback address — never directly reachable from outside.

---

## 1. Get a VPS

Any of these work fine for a school library's traffic (a few dozen
concurrent users at most): DigitalOcean, Hetzner, Linode, or an Indian
provider if you'd rather bill in INR. Pick the cheapest plan with:

- **Ubuntu 22.04 or 24.04**
- At least 1 GB RAM
- A public IPv4 address (you'll be given one automatically)

Once it's created, you'll have a server IP address, e.g. `203.0.113.10`.

## 2. Point the subdomain at it

In whatever control panel manages the `msceducation.net` DNS (likely
the same place the domain was registered, or wherever the current
website's DNS is managed):

- Add an **A record**: `library` → `203.0.113.10` (your VPS's IP)

This can take anywhere from a few minutes to a few hours to work
everywhere. You can check it's live with:
```
ping library.msceducation.net
```
(It should resolve to your VPS's IP — actual ping replies aren't
necessary, just that it resolves.)

## 3. Basic server setup

SSH into the VPS (your provider gives you the command/password or an
SSH key when it's created), then:

```bash
# Update the system
sudo apt update && sudo apt upgrade -y

# Install what SmartLib360 needs
sudo apt install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx tesseract-ocr poppler-utils

# A dedicated non-root user to run the app — never run it as root
sudo adduser --disabled-password --gecos "" smartlib
```

## 4. Copy SmartLib360 onto the server

From your own computer (not the VPS), upload the SmartLib360 folder —
easiest with `scp`:

```bash
scp -r SmartLib360 smartlib@203.0.113.10:/home/smartlib/
```

(If `scp` isn't available on Windows, WinSCP or FileZilla using SFTP
does the same thing with a drag-and-drop interface.)

Then, back on the VPS:

```bash
sudo su - smartlib
cd ~/SmartLib360
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
exit    # back to your own sudo-capable user
```

## 5. Install the systemd service (keeps it running)

This makes SmartLib360 start automatically on boot and restart itself
if it ever crashes — the VPS equivalent of the `START_LIBRARY.bat`
launcher.

```bash
sudo cp smartlib360.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smartlib360

# Confirm it's running:
sudo systemctl status smartlib360
```

If it's not running, check the logs:
```bash
sudo journalctl -u smartlib360 -n 50
```

The included `smartlib360.service` already sets two environment
variables that matter for this deployment specifically:
- `LIBRARY_BIND_HOST=127.0.0.1` — the app only listens on the server's
  own loopback address, never directly reachable from the internet.
- `LIBRARY_FORCE_HTTPS_COOKIES=1` — login sessions only work over
  HTTPS, once step 7 below is done. (Don't set this on a school's
  plain-HTTP LAN install — only on this internet-facing one.)

## 6. Nginx + HTTPS

```bash
sudo cp nginx_smartlib360.conf /etc/nginx/sites-available/smartlib360
sudo ln -s /etc/nginx/sites-available/smartlib360 /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# Get a free certificate and auto-configure HTTPS:
sudo certbot --nginx -d library.msceducation.net
```

Certbot will ask for an email (for renewal reminders) and rewrite the
Nginx config to add the HTTPS block automatically. Certificates renew
themselves — nothing more to do here.

## 7. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

This closes every port except SSH (22) and web (80/443) — the app's
port 5000 is not opened to the internet at all, since Nginx reaches it
over localhost.

## 8. Test it

Visit `https://library.msceducation.net` — you should land on the
Setup Wizard, exactly like a brand-new install. Complete setup, then
confirm both staff and student login work, and that a certificate/
report downloads correctly (this exercises the logo-loading code, a
good end-to-end check).

---

## Before you go live for real students — read this

1. **Hand out PIN set-up codes before telling students the address.**
   A student can only create a PIN with a one-time set-up code from the
   librarian (Members → **Issue PIN set-up codes** for a whole class, or
   **Issue code** on one card). The codes show in the member list's PIN
   column; print or copy them per class and hand them out in person.
   Students who already set a PIN are unaffected.
2. **Get the HTTPS certificate (step 6, `certbot`) before you ever set
   `LIBRARY_FORCE_HTTPS_COOKIES=1` and restart the service.** Once that
   flag is on, the app itself redirects every plain-HTTP request to
   HTTPS — correct once certbot's real certificate is in place, but if
   the flag is switched on first, students would be redirected to an
   HTTPS address that doesn't exist yet and can't reach the app at all.
   Order: certbot first, confirm `https://library.msceducation.net`
   loads in a browser, *then* set the flag and restart.
3. **Take a backup before and after this migration** — `Backup Now` in
   Admin settings, downloaded and kept somewhere off this server too.
4. **Tell students/parents this replaces the old LAN-only address** —
   if the school PC was also still running its own copy, you now have
   two separate databases that will drift apart. Pick one as the real
   system (this VPS, if students need home access) and retire the
   other, rather than running both.
