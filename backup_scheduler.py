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

Deliberately dependency-free (no APScheduler) — a single daemon thread
with a sleep loop is simple, easy to reason about, and matches the rest
of the codebase's preference for small, self-contained solutions.
"""
import os
import shutil
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
        shutil.copy(db_path, dest)
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
