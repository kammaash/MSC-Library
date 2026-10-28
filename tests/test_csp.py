"""Content-Security-Policy header and no inline script (audit item I4)."""
import os
import re

import pytest

from conftest import login_as, make_admin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTTPS = {"X-Forwarded-Proto": "https"}


@pytest.fixture
def csp_on(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_SEND_CSP", True)
    monkeypatch.setattr(app_module, "_FORCE_HTTPS", True)
    return app_module


def _directives(header):
    return {part.split()[0]: part.split()[1:] for part in header.split("; ")}


def test_no_csp_on_a_lan_install(client):
    assert "Content-Security-Policy" not in client.get("/login").headers


def test_local_mode_policy(client, csp_on):
    policy = _directives(client.get("/login", headers=HTTPS).headers["Content-Security-Policy"])
    assert policy["script-src"] == ["'self'"]
    assert policy["default-src"] == ["'self'"]
    assert policy["object-src"] == ["'none'"]
    assert not any("r2.cloudflarestorage.com" in v for values in policy.values() for v in values)


def test_r2_mode_allows_the_bucket_host_for_files_only(client, csp_on, monkeypatch):
    class Meta:
        endpoint_url = "https://acct123.r2.cloudflarestorage.com"

    class Client:
        meta = Meta()

    class FakeStorage:
        client = Client()

    monkeypatch.setattr(csp_on, "storage", FakeStorage())
    policy = _directives(client.get("/login", headers=HTTPS).headers["Content-Security-Policy"])
    host = "https://acct123.r2.cloudflarestorage.com"
    for name in ("img-src", "media-src", "frame-src", "connect-src"):
        assert host in policy[name], name
    assert host not in policy["script-src"] and host not in policy["style-src"]


def test_r2_fixture_origin_is_derived_from_the_client(csp_on, r2):
    assert csp_on._file_link_origin().startswith("https://")


def test_policy_only_on_html(client, csp_on):
    login_as(client, make_admin())
    r = client.get("/api/whoami", headers=HTTPS)
    assert r.mimetype == "application/json"
    assert "Content-Security-Policy" not in r.headers


@pytest.mark.parametrize("path", ["templates/index.html", "templates/login.html", "templates/setup.html",
                                  "templates/change_password.html", "static/js/app.js", "static/js/login.js",
                                  "static/js/setup.js", "static/js/change_password.js"])
def test_no_inline_script_or_handlers(path):
    text = open(os.path.join(ROOT, path), encoding="utf-8").read()
    # Inline event handler attributes in markup or in HTML built by JS.
    assert not re.search(r"""<[^>]*\son[a-z]+\s*=\s*["']""", text), path
    if path.endswith(".html"):
        assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", text), path
