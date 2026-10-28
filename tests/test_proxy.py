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


def _get_ip(tiny):
    r = tiny.test_client().get("/ip", headers={"X-Forwarded-For": "203.0.113.7"},
                               environ_base={"REMOTE_ADDR": "10.0.0.1"})
    return r.get_data(as_text=True)


def test_proxy_fix_not_applied_unless_flag_is_exactly_1(app_module):
    for env in ({}, {"LIBRARY_TRUST_PROXY": "0"}, {"LIBRARY_TRUST_PROXY": "true"}):
        assert app_module._apply_proxy_fix(_tiny_app(), env) is False


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


def test_https_with_trusted_proxy_starts(app_module):
    app_module._check_proxy_settings({"LIBRARY_FORCE_HTTPS_COOKIES": "1", "LIBRARY_TRUST_PROXY": "1"}, "app")


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
    assert "LIBRARY_TRUST_PROXY is not 1" in result.stderr
