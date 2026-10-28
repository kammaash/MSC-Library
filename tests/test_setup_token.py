"""LIBRARY_SETUP_TOKEN on /setup for internet-facing deployments (audit item I1)."""
import pytest

import security

FORM = {"school_name": "Test School", "admin_name": "IT Person", "admin_username": "itadmin",
        "admin_password": "first-pass-123", "confirm_password": "first-pass-123"}


@pytest.fixture
def fresh(app_module, monkeypatch):
    """A database where setup has not been completed yet."""
    from sqlalchemy import text
    with app_module.app.app_context():
        app_module.db.session.execute(text("DELETE FROM app_security WHERE key = 'setup_complete'"))
        app_module.db.session.commit()
    security.clear_caches()
    monkeypatch.setattr(app_module, "_SETUP_COMPLETE_CACHE", False)
    return app_module


@pytest.fixture
def https(fresh, monkeypatch):
    monkeypatch.setattr(fresh, "_FORCE_HTTPS", True)
    monkeypatch.setenv("LIBRARY_SETUP_TOKEN", "s3cret-token")
    return fresh


# As Render's proxy sends it; the HTTPS flag otherwise redirects the request.
HTTPS = {"X-Forwarded-Proto": "https"}


def _post(client, **extra):
    return client.post("/setup", data={**FORM, **extra}, headers=HTTPS)


def _get(client, query=""):
    return client.get("/setup" + query, headers=HTTPS)


def test_lan_install_needs_no_token(client, fresh):
    assert client.get("/setup").status_code == 200
    r = client.post("/setup", data=FORM)
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_https_get_without_token_is_400(client, https):
    assert _get(client).status_code == 400
    assert _get(client, "?token=wrong").status_code == 400


def test_https_get_with_token_renders_it_into_the_form(client, https):
    r = _get(client, "?token=s3cret-token")
    assert r.status_code == 200
    assert b'value="s3cret-token"' in r.data


def test_https_post_needs_the_token(client, https):
    from models import User
    assert _post(client).status_code == 400
    assert _post(client, setup_token="wrong").status_code == 400
    with https.app.app_context():
        assert User.query.count() == 0
    r = _post(client, setup_token="s3cret-token")
    assert r.status_code == 200
    assert security.setup_is_complete(https.DB_PATH)


def test_https_without_configured_token_refuses_setup(client, https, monkeypatch):
    monkeypatch.delenv("LIBRARY_SETUP_TOKEN")
    r = _get(client, "?token=")
    assert r.status_code == 400 and b"LIBRARY_SETUP_TOKEN" in r.data
    assert _post(client, setup_token="").status_code == 400


def test_wrong_tokens_are_rate_limited(client, https):
    for _ in range(security.MAX_ATTEMPTS):
        assert _post(client, setup_token="guess").status_code == 400
    # Locked now, even with the right token.
    assert _post(client, setup_token="s3cret-token").status_code == 429


def test_non_ascii_token_is_a_400_not_a_500(client, https):
    assert _get(client, "?token=%E0%B0%95").status_code == 400
