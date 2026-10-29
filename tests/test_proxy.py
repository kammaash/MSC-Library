import os
import subprocess
import sys

import pytest
from flask import Flask, request


def _tiny_app():
    tiny = Flask(__name__)

    @tiny.route("/ip")
    def ip():
        return request.remote_addr or ""

    return tiny


def _get_ip(tiny, forwarded_for="203.0.113.7"):
    r = tiny.test_client().get("/ip", headers={"X-Forwarded-For": forwarded_for},
                               environ_base={"REMOTE_ADDR": "10.0.0.1"})
    return r.get_data(as_text=True)


def test_proxy_fix_not_applied_when_unset_or_0(app_module):
    for env in ({}, {"LIBRARY_TRUST_PROXY": ""}, {"LIBRARY_TRUST_PROXY": "0"}):
        assert app_module._apply_proxy_fix(_tiny_app(), env) is False


@pytest.mark.parametrize("value", ["true", "yes", "-1", "1.5", "three", "11", "99"])
def test_invalid_proxy_count_stops_the_app(app_module, value):
    # A typo must not quietly run with the wrong client address.
    with pytest.raises(RuntimeError, match="LIBRARY_TRUST_PROXY must be a whole number"):
        app_module._apply_proxy_fix(_tiny_app(), {"LIBRARY_TRUST_PROXY": value})


# What Render sends: whatever the client wrote itself, then the client's real
# address and two of Render's own proxies appended after it.
_RENDER_CHAIN = "6.6.6.6, 198.51.100.23, 172.70.1.1, 10.201.0.5"


def test_render_chain_with_three_hops_gives_the_client(app_module):
    tiny = _tiny_app()
    assert app_module._apply_proxy_fix(tiny, {"LIBRARY_TRUST_PROXY": "3"}) is True
    assert _get_ip(tiny, _RENDER_CHAIN) == "198.51.100.23"


def test_render_chain_with_one_hop_gives_a_proxy(app_module):
    # The old setting: every student would appear as Render's proxy.
    tiny = _tiny_app()
    app_module._apply_proxy_fix(tiny, {"LIBRARY_TRUST_PROXY": "1"})
    assert _get_ip(tiny, _RENDER_CHAIN) == "10.201.0.5"


def test_client_written_forwarded_for_cannot_choose_the_address(app_module):
    tiny = _tiny_app()
    app_module._apply_proxy_fix(tiny, {"LIBRARY_TRUST_PROXY": "3"})
    forged = "1.2.3.4, 5.6.7.8, " + _RENDER_CHAIN
    assert _get_ip(tiny, forged) == "198.51.100.23"


def test_too_few_hops_falls_back_to_the_direct_peer(app_module):
    # Fewer entries than trusted proxies: ProxyFix ignores the header rather
    # than guessing. The staging check in DEPLOY_TO_RENDER.md catches this.
    tiny = _tiny_app()
    app_module._apply_proxy_fix(tiny, {"LIBRARY_TRUST_PROXY": "3"})
    assert _get_ip(tiny, "198.51.100.23, 10.201.0.5") == "10.0.0.1"


def test_forwarded_for_ignored_when_not_trusted(app_module):
    tiny = _tiny_app()
    app_module._apply_proxy_fix(tiny, {})
    assert _get_ip(tiny) == "10.0.0.1"


def test_forwarded_for_used_when_proxy_trusted(app_module):
    tiny = _tiny_app()
    assert app_module._apply_proxy_fix(tiny, {"LIBRARY_TRUST_PROXY": "1"}) is True
    assert _get_ip(tiny) == "203.0.113.7"


# --------------------------------------------------------------------------
# Startup check: an HTTPS deployment imported by gunicorn needs the proxy
# trusted, or every visitor shares one address (audit item I5).
# --------------------------------------------------------------------------
def test_https_without_trusted_proxy_refuses_to_start(app_module):
    with pytest.raises(RuntimeError, match="LIBRARY_TRUST_PROXY"):
        app_module._check_proxy_settings({"LIBRARY_FORCE_HTTPS_COOKIES": "1"}, "app")


@pytest.mark.parametrize("hops", ["1", "3"])
def test_https_with_trusted_proxy_starts(app_module, hops):
    app_module._check_proxy_settings({"LIBRARY_FORCE_HTTPS_COOKIES": "1", "LIBRARY_TRUST_PROXY": hops}, "app")


def test_https_with_proxy_set_to_0_refuses_to_start(app_module):
    with pytest.raises(RuntimeError, match="LIBRARY_TRUST_PROXY=3"):
        app_module._check_proxy_settings({"LIBRARY_FORCE_HTTPS_COOKIES": "1", "LIBRARY_TRUST_PROXY": "0"}, "app")


def test_lan_install_needs_neither(app_module):
    app_module._check_proxy_settings({}, "app")


def test_vps_waitress_run_is_exempt(app_module):
    # python app.py behind Nginx: waitress trusts the local proxy itself.
    app_module._check_proxy_settings({"LIBRARY_FORCE_HTTPS_COOKIES": "1"}, "__main__")


def test_gunicorn_style_import_fails_without_trusted_proxy(tmp_path):
    """The check is wired at import time, which is how gunicorn loads app:app."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "LIBRARY_TRUST_PROXY") and not k.startswith(("R2_", "ENTRA_"))}
    env.update(LIBRARY_DATA_DIR=str(tmp_path), LIBRARY_FORCE_HTTPS_COOKIES="1")
    result = subprocess.run([sys.executable, "-c", "import app"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode != 0
    assert "LIBRARY_TRUST_PROXY is not." in result.stderr


# --------------------------------------------------------------------------
# End to end: the real /login behind Render's three proxies. Two phones that
# reach the app through the same Render edge server still get separate
# sign-in counters, and one of them hitting the limit does not lock out the
# other.
# --------------------------------------------------------------------------
def _render_request(client, client_ip, edge="172.70.1.1", **payload):
    chain = f"{client_ip}, {edge}, 10.201.0.5"
    return client.post("/login", json=payload, headers={"X-Forwarded-For": chain},
                       environ_overrides={"REMOTE_ADDR": "10.201.0.9"})


def test_sign_in_limits_follow_the_real_client_behind_render(app_module, client, monkeypatch):
    import security
    monkeypatch.setattr(app_module.app, "wsgi_app", app_module.app.wsgi_app)
    app_module._apply_proxy_fix(app_module.app, {"LIBRARY_TRUST_PROXY": "3"})

    wrong = {"role_mode": "staff", "username": "ghost", "password": "nope-nope-1"}
    for n in range(security.MAX_ATTEMPTS):
        assert _render_request(client, "198.51.100.23", **wrong).status_code == 401, n
    # The phone that kept guessing is now locked ...
    assert _render_request(client, "198.51.100.23", **wrong).status_code == 429
    # ... while another client through the very same edge server is not.
    assert _render_request(client, "203.0.113.50", **wrong).status_code == 401
