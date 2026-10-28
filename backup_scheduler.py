"""
backup_scheduler.py — automatic daily database backups
--------------------------------------------------------
Previously, backups only happened when a staff member remembered to open
Manage Users -> Backup and click the button — meaning a school could go
months without one and lose everything in a drive failure. This adds a
background thread (started once, from app.py's __main__ block) that:

  1. Takes a backup shortly after every startup (covers schools that only
     ever run the app during school hours and might not be running at
     whatever fixed hour a cron-style schedule would pick).
  2. Then takes one every BACKUP_INTERVAL_HOURS after that, for as long as
     the process keeps running.
  3. Prunes backup files older than BACKUP_RETENTION_DAYS so the backups
     folder doesn't grow forever on a small school PC's disk.

start_background_backups() above copies the SQLite file and is for the
offline school-PC install. start_cloud_backups() is its counterpart for
the Render + Neon deployment: the database is not a local file there, so
the loop is handed two callables from app.py (one that writes a JSON
export to storage, one that prunes old exports) and has no file logic
of its own.

Deliberately dependency-free (no APScheduler) — a single daemon thread
with a sleep loop is simple, easy to reason about, and matches the rest
of the codebase's preference for small, self-contained solutions.
"""
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

BACKUP_INTERVAL_HOURS = 24
BACKUP_RETENTION_DAYS = 60


def _do_backup(db_path, backups_dir, logger=None):
    try:
        os.makedirs(backups_dir, exist_ok=True)
        if not os.path.exists(db_path):
            return
        stamp = datetime.now().strftime("%Y_%m_%d_%H%M%S")
        dest = os.path.join(backups_dir, f"auto_backup_{stamp}.db")
        # The app runs SQLite in WAL mode, so recent rows may still be in
        # database.db-wal; a plain file copy would miss them. SQLite's online
        # backup API reads through the WAL and gives a consistent snapshot.
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        if logger:
            logger.info(f"Automatic backup created: {os.path.basename(dest)}")
    except Exception as e:
        if logger:
            logger.warning(f"Automatic backup failed (non-fatal): {e}")


def _prune_old_backups(backups_dir, logger=None):
    try:
        if not os.path.isdir(backups_dir):
            return
        cutoff = datetime.now() - timedelta(days=BACKUP_RETENTION_DAYS)
        for fname in os.listdir(backups_dir):
            fpath = os.path.join(backups_dir, fname)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
                if mtime < cutoff:
                    os.remove(fpath)
                    if logger:
                        logger.info(f"Pruned old backup: {fname}")
            except OSError:
                continue
    except Exception as e:
        if logger:
            logger.warning(f"Backup pruning failed (non-fatal): {e}")


def _loop(db_path, backups_dir, logger):
    # First backup happens ~2 minutes after startup — gives the app time
    # to finish initializing rather than competing with startup I/O.
    time.sleep(120)
    while True:
        _do_backup(db_path, backups_dir, logger)
        _prune_old_backups(backups_dir, logger)
        time.sleep(BACKUP_INTERVAL_HOURS * 3600)


def start_background_backups(db_path, backups_dir, logger=None):
    """
    Call once at startup. Runs as a daemon thread so it never blocks
    process shutdown (Ctrl+C / service stop works normally).
    """
    t = threading.Thread(
        target=_loop, args=(db_path, backups_dir, logger),
        name="mslrms-backup-scheduler", daemon=True,
    )
    t.start()
    return t


def _cloud_loop(backup_fn, prune_fn, logger):
    # Same shape as _loop. Each step is wrapped separately so a failed
    # backup (say Neon is briefly unreachable) still lets the prune run,
    # and neither can kill the thread — a dead thread would silently mean
    # no more backups until the next deploy.
    time.sleep(120)
    while True:
        try:
            name = backup_fn()
            if logger:
                logger.info(f"Automatic backup created: {name}")
        except Exception as e:
            if logger:
                logger.warning(f"Automatic backup failed (non-fatal): {e}")
        try:
            prune_fn(BACKUP_RETENTION_DAYS)
        except Exception as e:
            if logger:
                logger.warning(f"Backup pruning failed (non-fatal): {e}")
        time.sleep(BACKUP_INTERVAL_HOURS * 3600)


def start_cloud_backups(backup_fn, prune_fn, logger=None):
    """
    Cloud counterpart of start_background_backups. backup_fn() takes one
    backup and returns its name; prune_fn(retention_days) deletes old ones.
    Daemon thread, so it never blocks process shutdown.
    """
    t = threading.Thread(
        target=_cloud_loop, args=(backup_fn, prune_fn, logger),
        name="mslrms-cloud-backup-scheduler", daemon=True,
    )
    t.start()
    return t
