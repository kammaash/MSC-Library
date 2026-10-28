"""Sign in with Microsoft (students). MSAL is replaced by a fake that returns
fixed claims, so these tests exercise our own matching and refusal rules, not
Microsoft's token validation."""
import io

import pytest

from conftest import make_admin, make_student, login_as, student_user
from models import db, Member, User

CLAIMS = {"tid": "tenant-1", "oid": "oid-1", "preferred_username": "Asha@MSC.edu"}


class FakeEntraApp:
    """Stands in for msal.ConfidentialClientApplication."""

    def __init__(self):
        self.claims = None          # id_token_claims the "callback" returns
        self.result = None          # overrides the whole result when set
        self.raises = None          # exception to raise from the callback
        self.flow_calls = []
        self.token_calls = []

    def initiate_auth_code_flow(self, scopes, redirect_uri=None, **kwargs):
        self.flow_calls.append((scopes, redirect_uri, kwargs))
        return {"auth_uri": "https://login.example/auth", "state": "y"}

    def acquire_token_by_auth_code_flow(self, flow, args):
        self.token_calls.append((flow, args))
        if self.raises:
            raise self.raises
        if self.result is not None:
            return self.result
        return {"id_token_claims": self.claims}


@pytest.fixture
def entra(app_module, monkeypatch):
    fake = FakeEntraApp()
    monkeypatch.setattr(app_module, "ENTRA_ENABLED", True)
    monkeypatch.setattr(app_module, "ENTRA_TENANT_ID", "tenant-1")
    monkeypatch.setattr(app_module, "PUBLIC_BASE_URL", "https://library.example")
    monkeypatch.setattr(app_module, "_entra_app", lambda: fake)
    return fake


def _sign_in(client):
    """Run the full redirect round trip and return the callback response."""
    client.get("/auth/microsoft/login")
    return client.get("/auth/microsoft/callback?code=x&state=y")


def ctx(fn):
    """Run fn inside a fresh app context (the test client's requests use their
    own, so reads here always see committed data)."""
    import app
    with app.app.app_context():
        return fn()


def _whoami_role(client):
    # Signed out, login_required answers with a redirect/401 rather than JSON.
    return (client.get("/api/whoami").get_json(silent=True) or {}).get("role")


# ---------------------------------------------------------------- happy paths
def test_links_by_email_then_binds_oid(entra, client):
    m = make_student(email="asha@msc.edu")
    entra.claims = CLAIMS
    r = _sign_in(client)
    assert r.status_code == 302 and r.location.endswith("/")
    assert ctx(lambda: db.session.get(Member, m.id).entra_oid) == "oid-1"
    assert _whoami_role(client) == "student"


def test_bound_account_still_works_after_email_change_in_entra(entra, client):
    make_student(email="asha@msc.edu", entra_oid="oid-1")
    entra.claims = {**CLAIMS, "preferred_username": "asha.k@msc.edu"}
    r = _sign_in(client)
    assert r.status_code == 302 and "ms_error" not in r.location
    assert _whoami_role(client) == "student"


def test_email_claim_used_when_no_preferred_username(entra, client):
    make_student(email="asha@msc.edu")
    entra.claims = {"tid": "tenant-1", "oid": "oid-1", "email": " ASHA@msc.edu "}
    r = _sign_in(client)
    assert "ms_error" not in r.location and _whoami_role(client) == "student"


def test_login_start_redirects_to_microsoft_with_callback_uri(entra, client):
    r = client.get("/auth/microsoft/login")
    assert r.status_code == 302 and r.location == "https://login.example/auth"
    scopes, redirect_uri, _ = entra.flow_calls[0]
    assert scopes == ["User.Read"]
    assert redirect_uri == "https://library.example/auth/microsoft/callback"
    with client.session_transaction() as sess:
        assert sess["entra_flow"]["state"] == "y"


# -------------------------------------------------------------- refusal paths
def test_unknown_email_not_linked(entra, client):
    entra.claims = CLAIMS
    r = _sign_in(client)
    assert "ms_error=not_linked" in r.location
    assert ctx(lambda: User.query.filter_by(role="student").count()) == 0
    assert _whoami_role(client) != "student"


def test_other_tenant_rejected(entra, client):
    make_student(email="asha@msc.edu")
    entra.claims = {**CLAIMS, "tid": "other"}
    r = _sign_in(client)
    assert "ms_error=wrong_school" in r.location
    assert _whoami_role(client) != "student"


def test_email_bound_to_different_oid_is_not_rebound(entra, client):
    m = make_student(email="asha@msc.edu", entra_oid="someone-else")
    entra.claims = CLAIMS
    r = _sign_in(client)
    assert "ms_error=not_linked" in r.location
    assert ctx(lambda: db.session.get(Member, m.id).entra_oid) == "someone-else"
    assert _whoami_role(client) != "student"


def test_staff_card_cannot_sign_in_by_email(entra, client):
    m = make_student(email="asha@msc.edu", type="Staff")
    entra.claims = CLAIMS
    r = _sign_in(client)
    assert "ms_error=not_linked" in r.location
    assert ctx(lambda: db.session.get(Member, m.id).entra_oid) is None


def test_staff_card_cannot_sign_in_by_bound_oid(entra, client):
    make_student(email="asha@msc.edu", entra_oid="oid-1", type="Staff")
    entra.claims = CLAIMS
    r = _sign_in(client)
    assert "ms_error=not_linked" in r.location
    assert ctx(lambda: User.query.filter_by(role="student").count()) == 0


def test_oid_already_on_another_card_is_not_bound_again(entra, client):
    make_student(admission_no="A1", email="other@msc.edu", entra_oid="oid-1", type="Staff")
    m = make_student(admission_no="A2", email="asha@msc.edu")
    entra.claims = CLAIMS
    r = _sign_in(client)
    assert "ms_error=not_linked" in r.location
    assert ctx(lambda: db.session.get(Member, m.id).entra_oid) is None


def test_two_cards_with_same_email_is_ambiguous(entra, client):
    make_student(admission_no="A1", email="asha@msc.edu")
    make_student(admission_no="A2", email="asha@msc.edu")
    entra.claims = CLAIMS
    r = _sign_in(client)
    assert "ms_error=not_linked" in r.location
    assert ctx(lambda: Member.query.filter(Member.entra_oid.isnot(None)).count()) == 0


@pytest.mark.parametrize("claims", [
    {"tid": "tenant-1", "preferred_username": "asha@msc.edu"},   # no oid
    {"oid": "oid-1", "preferred_username": "asha@msc.edu"},      # no tid
    {},
    None,
])
def test_missing_identity_claims_fail(entra, client, claims):
    make_student(email="asha@msc.edu")
    entra.claims = claims
    r = _sign_in(client)
    assert "ms_error=failed" in r.location
    assert _whoami_role(client) != "student"


def test_no_email_claim_and_no_bound_oid_is_not_linked(entra, client):
    make_student(email="asha@msc.edu")
    entra.claims = {"tid": "tenant-1", "oid": "oid-1"}
    r = _sign_in(client)
    assert "ms_error=not_linked" in r.location


def test_callback_without_stored_flow_fails(entra, client):
    make_student(email="asha@msc.edu")
    entra.claims = CLAIMS
    r = client.get("/auth/microsoft/callback?code=x&state=y")   # never started
    assert "ms_error=failed" in r.location
    assert entra.token_calls == []
    assert _whoami_role(client) != "student"


def test_callback_cannot_be_replayed(entra, client):
    make_student(email="asha@msc.edu")
    entra.claims = CLAIMS
    client.get("/auth/microsoft/login")
    assert "ms_error" not in client.get("/auth/microsoft/callback?code=x&state=y").location
    client.get("/logout")
    r = client.get("/auth/microsoft/callback?code=x&state=y")
    assert "ms_error=failed" in r.location


def test_msal_error_result_fails(entra, client):
    make_student(email="asha@msc.edu")
    entra.result = {"error": "invalid_grant", "error_description": "secret detail"}
    r = _sign_in(client)
    assert "ms_error=failed" in r.location and "secret" not in r.location
    assert _whoami_role(client) != "student"


def test_msal_exception_fails(entra, client):
    make_student(email="asha@msc.edu")
    entra.raises = ValueError("state mismatch")
    r = _sign_in(client)
    assert "ms_error=failed" in r.location
    assert _whoami_role(client) != "student"


def test_sign_in_discards_previous_session_contents(entra, client):
    make_student(email="asha@msc.edu")
    entra.claims = CLAIMS
    client.get("/auth/microsoft/login")
    with client.session_transaction() as sess:
        sess["planted"] = "attacker-chosen"
    client.get("/auth/microsoft/callback?code=x&state=y")
    with client.session_transaction() as sess:
        assert "planted" not in sess and "entra_flow" not in sess
        assert sess["srv"]
    assert _whoami_role(client) == "student"


def test_routes_404_when_not_configured(client):
    assert client.get("/auth/microsoft/login").status_code == 404
    assert client.get("/auth/microsoft/callback?code=x&state=y").status_code == 404


def test_routes_reachable_before_login_when_setup_complete(entra, client):
    # No session beyond the CSRF token: neither hook may bounce these.
    r = client.get("/auth/microsoft/login")
    assert r.location == "https://login.example/auth"


# ------------------------------------------------------------ login page text
def test_login_page_shows_button_only_when_enabled(entra, client, app_module, monkeypatch):
    assert b"/auth/microsoft/login" in client.get("/login").data
    monkeypatch.setattr(app_module, "ENTRA_ENABLED", False)
    assert b"/auth/microsoft/login" not in client.get("/login").data


@pytest.mark.parametrize("code,text", [
    ("not_linked", "isn&#39;t linked to a library card yet"),
    ("wrong_school", "Please sign in with your school Microsoft account."),
    ("failed", "Microsoft sign-in didn&#39;t complete. Please try again."),
])
def test_login_page_shows_fixed_message(client, code, text):
    assert text in client.get(f"/login?ms_error={code}").get_data(as_text=True)


def test_login_page_never_echoes_unknown_error_code(client):
    body = client.get("/login?ms_error=<script>alert(1)</script>").get_data(as_text=True)
    assert "alert(1)" not in body


# ------------------------------------------------------------ startup config
def test_entra_config_unset_is_disabled(app_module):
    assert app_module._entra_config({}) == (False, "", "", "")
    assert app_module._entra_config({"ENTRA_TENANT_ID": "", "ENTRA_CLIENT_ID": " "}) == (False, "", "", "")


def test_entra_config_partial_names_missing_variables(app_module):
    with pytest.raises(RuntimeError) as exc:
        app_module._entra_config({"ENTRA_TENANT_ID": "t", "PUBLIC_BASE_URL": "https://x"})
    assert "ENTRA_CLIENT_ID" in str(exc.value) and "ENTRA_CLIENT_SECRET" in str(exc.value)
    assert "ENTRA_TENANT_ID" not in str(exc.value).split("missing")[1].split(".")[0]


def test_entra_config_requires_public_base_url(app_module):
    env = {"ENTRA_TENANT_ID": "t", "ENTRA_CLIENT_ID": "c", "ENTRA_CLIENT_SECRET": "s"}
    with pytest.raises(RuntimeError) as exc:
        app_module._entra_config(env)
    assert "PUBLIC_BASE_URL" in str(exc.value)
    assert app_module._entra_config({**env, "PUBLIC_BASE_URL": "https://x"}) == (True, "t", "c", "s")


# -------------------------------------------------------- members API: email
def test_duplicate_member_email_rejected(client):
    login_as(client, make_admin())
    make_student(email="a@msc.edu")
    r = client.post("/api/members", json={"name": "B", "email": " A@MSC.edu "})
    assert r.status_code == 409
    assert r.get_json()["error"] == "That email is already on another library card."


def test_add_member_normalises_email_and_blank_is_none(client):
    login_as(client, make_admin())
    r = client.post("/api/members", json={"name": "B", "email": "  Bee@MSC.edu "})
    assert r.status_code == 201 and r.get_json()["email"] == "bee@msc.edu"
    assert r.get_json()["ms_linked"] is False
    r = client.post("/api/members", json={"name": "C", "email": "   "})
    assert r.status_code == 201 and r.get_json()["email"] is None


def test_update_member_duplicate_email_excludes_self(client):
    login_as(client, make_admin())
    a = make_student(admission_no="A1", email="a@msc.edu")
    b = make_student(admission_no="A2", email="b@msc.edu")
    assert client.put(f"/api/members/{a.id}", json={"email": "A@msc.edu"}).status_code == 200
    r = client.put(f"/api/members/{b.id}", json={"email": "A@msc.edu"})
    assert r.status_code == 409
    assert ctx(lambda: db.session.get(Member, b.id).email) == "b@msc.edu"


def test_changing_email_clears_link_but_same_email_keeps_it(client):
    login_as(client, make_admin())
    m = make_student(email="a@msc.edu", entra_oid="oid-1")
    r = client.put(f"/api/members/{m.id}", json={"name": "X", "email": " A@msc.edu"})
    assert r.get_json()["ms_linked"] is True
    assert ctx(lambda: db.session.get(Member, m.id).entra_oid) == "oid-1"
    r = client.put(f"/api/members/{m.id}", json={"email": "new@msc.edu"})
    assert r.get_json()["ms_linked"] is False and r.get_json()["email"] == "new@msc.edu"
    assert ctx(lambda: db.session.get(Member, m.id).entra_oid) is None


def test_update_without_email_field_leaves_email_and_link(client):
    login_as(client, make_admin())
    m = make_student(email="a@msc.edu", entra_oid="oid-1")
    client.put(f"/api/members/{m.id}", json={"name": "Renamed"})
    assert ctx(lambda: (lambda r: (r.email, r.entra_oid))(db.session.get(Member, m.id))) == ("a@msc.edu", "oid-1")


def test_entra_oid_cannot_be_set_through_the_api(client):
    login_as(client, make_admin())
    m = make_student(email="a@msc.edu")
    client.put(f"/api/members/{m.id}", json={"entra_oid": "forged"})
    assert ctx(lambda: db.session.get(Member, m.id).entra_oid) is None
    r = client.post("/api/members", json={"name": "B", "entra_oid": "forged"})
    assert r.get_json()["ms_linked"] is False


def _xlsx(rows):
    import pandas as pd
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    buf.seek(0)
    return buf


def test_import_reads_email_and_skips_duplicate_email_rows(client):
    login_as(client, make_admin())
    make_student(admission_no="A1", email="taken@msc.edu")
    data = _xlsx([
        {"Name": "New One", "AdmissionNo": "B1", "School Email": "New@MSC.edu"},
        {"Name": "Clash", "AdmissionNo": "B2", "School Email": "taken@msc.edu"},
        {"Name": "Clash Again", "AdmissionNo": "B3", "School Email": "NEW@msc.edu"},
    ])
    r = client.post("/api/import/members", data={"file": (data, "m.xlsx")},
                    content_type="multipart/form-data")
    j = r.get_json()
    assert r.status_code == 200 and j["created"] == 1 and j["skipped_duplicate_email"] == 2
    assert ctx(lambda: Member.query.filter_by(admission_no="B1").one().email) == "new@msc.edu"
    assert ctx(lambda: Member.query.filter_by(admission_no="B2").first()) is None


# ------------------------------------------------------- PIN login regression
def test_pin_login_unchanged(client):
    from werkzeug.security import generate_password_hash
    m = make_student(admission_no="A100", dob="2014-05-01", pin_hash=generate_password_hash("1234"))
    body = {"role_mode": "student", "username": "A100", "password": "01052014"}

    r = client.post("/login", json=body)
    assert r.status_code == 200 and r.get_json()["need_pin"] is True
    assert _whoami_role(client) != "student"

    r = client.post("/login", json={**body, "pin": "9999"})
    assert r.status_code == 401

    r = client.post("/login", json={**body, "pin": "1234"})
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "user": {"name": "Test Student", "role": "student"}}
    assert _whoami_role(client) == "student"
    assert ctx(lambda: User.query.filter_by(username=f"student-{m.member_id}", role="student").count()) == 1
