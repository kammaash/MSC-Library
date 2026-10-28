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
