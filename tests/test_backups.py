import json
import os
import re
import threading
from types import SimpleNamespace
from datetime import datetime, timedelta

import pytest

import backup_scheduler
from conftest import login_as, make_admin, make_student


@pytest.fixture(autouse=True)
def clean_backups(app_module):
    """Local storage is shared across the whole test session; start and end
    each test with no backup files."""
    def wipe():
        for key in app_module.storage.list_keys("uploads/backups/"):
            app_module.storage.delete(key)
    wipe()
    yield
    wipe()


def test_json_export_contains_every_table(app_module):
    make_student()
    with app_module.app.app_context():
        data = json.loads(app_module._export_all_tables_json())
    assert {"members", "books", "app_security", "login_rate_limits"} <= set(data["tables"])
    assert data["tables"]["members"][0]["admission_no"] == "A100"
    assert "exported_at" in data


def test_json_export_keeps_non_ascii_text(app_module):
    make_student(admission_no="A200", class_dept="Grade 7 සිං")
    with app_module.app.app_context():
        raw = app_module._export_all_tables_json()
    assert "Grade 7 සිං".encode("utf-8") in raw


def test_backup_now_postgres_mode_writes_json_to_storage(client, r2, monkeypatch, app_module):
    monkeypatch.setattr(app_module, "_database_url", "postgresql://fake")
    login_as(client, make_admin())
    name = client.post("/api/backup").get_json()["file"]
    assert name.endswith(".json") and r2.list_keys("uploads/backups/") == ["uploads/backups/" + name]
    body = r2.client.get_object(Bucket=r2.bucket, Key="uploads/backups/" + name)["Body"].read()
    assert "members" in json.loads(body)["tables"]


def test_backup_now_sqlite_mode_copies_db_through_storage(client, app_module):
    login_as(client, make_admin())
    name = client.post("/api/backup").get_json()["file"]
    assert re.fullmatch(r"backup_\d{4}_\d\d_\d\d_\d{6}\.db", name)
    assert app_module.storage.list_keys("uploads/backups/") == ["uploads/backups/" + name]
    with open(app_module.storage.local_path("uploads/backups/" + name), "rb") as f:
        assert f.read(15) == b"SQLite format 3"


def test_list_backups_newest_first_across_prefixes(client, app_module):
    s = app_module.storage
    s.save_bytes(b"{}", "uploads/backups/backup_2026_01_01_000000.json")
    s.save_bytes(b"{}", "uploads/backups/auto_backup_2026_03_01_000000.json")
    s.save_bytes(b"{}", "uploads/backups/backup_2026_02_01_000000.db")
    login_as(client, make_admin())
    assert client.get("/api/backup/list").get_json() == [
        "auto_backup_2026_03_01_000000.json",
        "backup_2026_02_01_000000.db",
        "backup_2026_01_01_000000.json",
    ]


def test_download_backup_serves_file_and_404s_when_missing(client, app_module):
    app_module.storage.save_bytes(b"hello", "uploads/backups/backup_2026_01_01_000000.json")
    login_as(client, make_admin())
    r = client.get("/api/backup/download/backup_2026_01_01_000000.json")
    assert r.status_code == 200 and r.data == b"hello"
    assert "attachment" in r.headers["Content-Disposition"]
    assert client.get("/api/backup/download/nope.json").status_code == 404


def test_download_backup_redirects_to_presigned_link_on_r2(client, r2):
    r2.save_bytes(b"x", "uploads/backups/backup_2026_01_01_000000.json")
    login_as(client, make_admin())
    r = client.get("/api/backup/download/backup_2026_01_01_000000.json")
    assert r.status_code == 302 and "X-Amz-Expires=300" in r.headers["Location"]


def test_backup_endpoints_require_admin(client):
    assert client.post("/api/backup").status_code in (401, 302, 403)


def test_prune_deletes_only_old_backups(app_module):
    app_module.storage.save_bytes(b"{}", "uploads/backups/backup_2020_01_01_000000.json")
    app_module.storage.save_bytes(b"{}", f"uploads/backups/backup_{datetime.now():%Y_%m_%d_%H%M%S}.json")
    assert app_module._prune_backups(60) == 1


def test_prune_handles_both_prefixes_and_extensions_and_ignores_odd_names(app_module):
    s = app_module.storage
    for name in ("backup_2020_01_01_000000.db", "auto_backup_2020_01_01_000000.json",
                 "keep_me.txt", "backup_notadate.json"):
        s.save_bytes(b"x", "uploads/backups/" + name)
    assert app_module._prune_backups(60) == 2
    assert sorted(k.rsplit("/", 1)[1] for k in s.list_keys("uploads/backups/")) == [
        "backup_notadate.json", "keep_me.txt"]


def test_cloud_scheduler_survives_errors_and_runs_backup_then_prune(monkeypatch):
    calls = []
    sleeps = []
    reached_third_cycle = threading.Event()
    release = threading.Event()

    def backup_fn():
        calls.append("backup")
        raise RuntimeError("boom")

    def prune_fn(days):
        calls.append(("prune", days))
        raise RuntimeError("boom too")

    def fake_sleep(seconds):
        # 1st: startup delay, 2nd: after cycle one, 3rd: after cycle two.
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            reached_third_cycle.set()
            release.wait(5)

    # Patch the module's own reference so the real time.sleep is untouched.
    monkeypatch.setattr(backup_scheduler, "time", SimpleNamespace(sleep=fake_sleep))
    t = backup_scheduler.start_cloud_backups(backup_fn, prune_fn, None)
    try:
        assert reached_third_cycle.wait(5)
    finally:
        release.set()
    # Two full cycles ran despite both functions raising every time.
    assert calls == ["backup", ("prune", backup_scheduler.BACKUP_RETENTION_DAYS)] * 2
    assert sleeps[1:3] == [backup_scheduler.BACKUP_INTERVAL_HOURS * 3600] * 2
    assert t.daemon
