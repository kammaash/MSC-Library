import os

from conftest import login_as, make_admin


def test_app_uses_isolated_data_dir(app_module, tmp_path_factory):
    assert "MSC-Library/database.db" not in app_module.DB_PATH
    assert os.environ["LIBRARY_DATA_DIR"] in app_module.DB_PATH


def test_login_as_admin_reaches_whoami(client):
    login_as(client, make_admin())
    r = client.get("/api/whoami")
    assert r.status_code == 200 and r.get_json()["role"] == "admin"
