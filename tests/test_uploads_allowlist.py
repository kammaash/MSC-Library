"""/uploads/ serves only the folders the app writes for the browser.

The route used to deny "backups/" by matching the request string, but the
file was opened through realpath. On Windows "backups." and "BACKUP~1" are
the same directory as "backups" on disk while being different strings, so the
denylist could be stepped around. The files here are created literally
(a folder really called "backups.") so the tests prove the same thing on
every operating system: the decision is made on the folder name alone and
anything outside the allowlist is a 404, whether or not the file exists.
"""
import os

import pytest

from conftest import login_as, make_admin, make_student, student_user


def _put(app_module, *parts):
    path = os.path.join(app_module.BASE_DIR, "uploads", *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"data")
    return "/uploads/" + "/".join(parts)


@pytest.mark.parametrize("alias", ["backups.", "backups ", "BACKUP~1", "Backups."])
def test_backups_aliases_are_404_even_when_the_file_exists(client, app_module, alias):
    url = _put(app_module, alias, "auto_backup_2026_10_29_081502.db")
    login_as(client, make_admin())
    assert client.get(url).status_code == 404


def test_student_cannot_reach_other_qr_through_a_folder_alias(client, app_module):
    url = _put(app_module, "qrcodes.", "MEM-A2.png")
    login_as(client, student_user(make_student("A1")))
    assert client.get(url).status_code == 404


def test_folders_the_app_does_not_write_are_404(client, app_module):
    url = _put(app_module, "covers", "x.png")
    login_as(client, make_admin())
    assert client.get(url).status_code == 404


def test_files_directly_under_uploads_are_404(client, app_module):
    url = _put(app_module, "x.png")
    login_as(client, make_admin())
    assert client.get(url).status_code == 404


def test_nested_paths_inside_an_allowed_folder_are_404(client, app_module):
    url = _put(app_module, "barcodes", "sub", "x.png")
    login_as(client, make_admin())
    assert client.get(url).status_code == 404


def test_allowed_folders_still_serve(client, app_module):
    login_as(client, make_admin())
    for folder in ("barcodes", "qrcodes", "pdfs"):
        assert client.get(_put(app_module, folder, "ok.bin")).status_code == 200
