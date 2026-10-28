"""
Sign-in throttling and the librarian-issued PIN set-up code.

Covers the three counters in security.py (per IP+username, per account,
per IP across usernames) on /login and /api/pin-reset-request, and that a
student card without a PIN cannot be claimed with only the admission
number and date of birth.
"""
import pytest
from werkzeug.security import generate_password_hash

import security
from conftest import login_as, make_admin, make_student

DOB = "01052014"  # make_student's default dob, as typed at the login page


def _student_login(client, ip="10.0.0.1", admission_no="A100", dob=DOB, **extra):
    payload = {"role_mode": "student", "username": admission_no, "password": dob, **extra}
    return client.post("/login", json=payload, environ_overrides={"REMOTE_ADDR": ip})


def _member(app_module, member_id):
    from models import Member
    with app_module.app.app_context():
        return app_module.db.session.get(Member, member_id)


def _new_client(client):
    """Another browser: its own session, with a CSRF token."""
    other = client.application.test_client()
    with other.session_transaction() as sess:
        sess["_csrf_token"] = "t"
    other.environ_base["HTTP_X_CSRF_TOKEN"] = "t"
    return other


def _shift_clock(monkeypatch, seconds):
    """Moves security.py's clock only (patching time.time itself would
    also break Flask's signed session cookie)."""
    import time
    import types
    monkeypatch.setattr(security, "time", types.SimpleNamespace(time=lambda: time.time() + seconds))


def _make_user(role, username):
    from conftest import _persist
    from models import User
    return _persist(User(username=username, name=username, role=role,
                         password_hash=generate_password_hash("pass-word-123")))


# --------------------------------------------------------------------------
# Counters
# --------------------------------------------------------------------------
def test_many_usernames_from_one_ip_hit_the_address_cap(client):
    for n in range(security.IP_MAX_FAILURES):
        r = _student_login(client, admission_no=f"X{n}", dob="01012000")
        assert r.status_code == 401, n
    r = _student_login(client, admission_no="X-next", dob="01012000")
    assert r.status_code == 429
    # Another address is unaffected.
    assert _student_login(client, ip="10.0.0.2", admission_no="X-next", dob="01012000").status_code == 401


def test_address_cap_also_covers_pin_reset_requests(client):
    for n in range(security.IP_MAX_FAILURES):
        r = client.post("/api/pin-reset-request", json={"admission_no": f"X{n}", "dob": "01012000"},
                        environ_overrides={"REMOTE_ADDR": "10.0.0.1"})
        assert r.status_code == 401, n
    assert _student_login(client, admission_no="X-next", dob="01012000").status_code == 429


def test_many_ips_against_one_student_lock_the_card(client):
    make_student()
    for n in range(security.ACCOUNT_MAX_FAILURES):
        r = _student_login(client, ip=f"10.1.0.{n}", dob="02022014")
        assert r.status_code == 401, n
    # Even the right DOB from a fresh address is now refused.
    r = _student_login(client, ip="10.9.9.9")
    assert r.status_code == 429
    assert "librarian" in r.get_json()["error"]


def test_student_card_lock_waits_for_a_librarian(client, monkeypatch):
    member = make_student(pin_setup_code="K7MP2Q")
    for n in range(security.ACCOUNT_MAX_FAILURES):
        _student_login(client, ip=f"10.1.0.{n}", dob="02022014")
    _shift_clock(monkeypatch, 3 * 24 * 3600)
    assert _student_login(client, ip="10.9.9.9").status_code == 429

    staff = _new_client(client)
    login_as(staff, make_admin())
    rows = staff.get("/api/members").get_json()
    assert rows[0]["signin_locked"] is True
    assert staff.post(f"/api/members/{member.id}/unlock").status_code == 200
    assert staff.get("/api/members").get_json()[0]["signin_locked"] is False
    assert _student_login(client, ip="10.9.9.9").get_json().get("need_setup_code") is True


def test_pin_reset_request_failures_count_against_the_card(client):
    make_student()
    for n in range(security.ACCOUNT_MAX_FAILURES):
        r = client.post("/api/pin-reset-request", json={"admission_no": "A100", "dob": "02022014"},
                        environ_overrides={"REMOTE_ADDR": f"10.2.0.{n}"})
        assert r.status_code == 401
    assert _student_login(client, ip="10.9.9.9").status_code == 429


def test_staff_account_lock_is_timed(client, monkeypatch):
    make_admin()
    for n in range(security.ACCOUNT_MAX_FAILURES):
        r = client.post("/login", json={"username": "admin", "password": "wrong"},
                        environ_overrides={"REMOTE_ADDR": f"10.3.0.{n}"})
        assert r.status_code == 401
    good = {"username": "admin", "password": "admin-pass-123"}
    assert client.post("/login", json=good, environ_overrides={"REMOTE_ADDR": "10.9.9.9"}).status_code == 429
    _shift_clock(monkeypatch, security.STAFF_ACCOUNT_LOCK_SECONDS + 1)
    assert client.post("/login", json=good, environ_overrides={"REMOTE_ADDR": "10.9.9.9"}).status_code == 200


def test_invented_role_mode_shares_the_staff_counters(client):
    make_admin()
    for n in range(security.ACCOUNT_MAX_FAILURES):
        client.post("/login", json={"role_mode": f"x{n}", "username": "admin", "password": "wrong"},
                    environ_overrides={"REMOTE_ADDR": f"10.4.0.{n}"})
    r = client.post("/login", json={"username": "admin", "password": "admin-pass-123"},
                    environ_overrides={"REMOTE_ADDR": "10.9.9.9"})
    assert r.status_code == 429


def test_success_does_not_reset_the_address_counter(client, app_module):
    make_admin()
    for n in range(security.IP_MAX_FAILURES - 1):
        _student_login(client, admission_no=f"X{n}", dob="01012000")
    # A separate client: signing in clears the session, CSRF token included.
    r = _new_client(client).post("/login", json={"username": "admin", "password": "admin-pass-123"},
                                 environ_overrides={"REMOTE_ADDR": "10.0.0.1"})
    assert r.status_code == 200
    _student_login(client, admission_no="X-last", dob="01012000")
    assert _student_login(client, admission_no="X-after", dob="01012000").status_code == 429


def test_unknown_accounts_leave_no_account_rows(client, app_module):
    _student_login(client, admission_no="nobody", dob="01012000")
    from sqlalchemy import text
    with app_module.app.app_context():
        keys = [k for (k,) in app_module.db.session.execute(text("SELECT rate_key FROM login_rate_limits"))]
    assert not [k for k in keys if k.startswith("account|")]


def test_wrong_pin_reports_attempts_left(client):
    make_student(pin_hash=generate_password_hash("1234"))
    r = _student_login(client, pin="9999")
    assert r.status_code == 401
    body = r.get_json()
    assert body["need_pin"] is True
    assert body["attempts_remaining"] == security.MAX_ATTEMPTS - 1
    assert _student_login(client, pin="1234").status_code == 200


# --------------------------------------------------------------------------
# First-PIN set-up code
# --------------------------------------------------------------------------
def test_card_without_code_cannot_create_a_pin(client, app_module):
    member = make_student()
    r = _student_login(client)
    assert r.status_code == 403
    body = r.get_json()
    assert "need_setup_code" not in body and "must_set_pin" not in body
    # Sending a new PIN anyway changes nothing.
    r = _student_login(client, setup_code="", new_pin="1234", confirm_pin="1234")
    assert r.status_code == 403
    assert _member(app_module, member.id).pin_hash is None


def test_wrong_code_is_a_counted_failure(client, app_module):
    member = make_student(pin_setup_code="K7MP2Q")
    assert _student_login(client).get_json()["need_setup_code"] is True
    r = _student_login(client, setup_code="AAAAAA", new_pin="1234", confirm_pin="1234")
    assert r.status_code == 401
    assert r.get_json()["need_setup_code"] is True
    assert r.get_json()["attempts_remaining"] == security.MAX_ATTEMPTS - 1
    assert _member(app_module, member.id).pin_hash is None


def test_non_ascii_code_is_rejected_not_a_500(client):
    make_student(pin_setup_code="K7MP2Q")
    r = _student_login(client, setup_code="కోడ్", new_pin="1234", confirm_pin="1234")
    assert r.status_code == 401


def test_right_code_creates_the_pin_and_is_used_up(client, app_module):
    member = make_student(pin_setup_code="K7MP2Q")
    r = _student_login(client, setup_code="k7mp-2q", new_pin="4321", confirm_pin="4321")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    stored = _member(app_module, member.id)
    assert stored.pin_hash and stored.pin_setup_code is None
    # From now on it is DOB + PIN.
    fresh = _new_client(client)
    assert _student_login(fresh).get_json()["need_pin"] is True


def test_bad_new_pin_keeps_the_code(client, app_module):
    member = make_student(pin_setup_code="K7MP2Q")
    r = _student_login(client, setup_code="K7MP2Q", new_pin="12", confirm_pin="12")
    assert r.status_code == 400 and r.get_json()["need_setup_code"] is True
    assert _member(app_module, member.id).pin_setup_code == "K7MP2Q"


@pytest.fixture
def staff(client):
    login_as(client, make_admin())
    return client


def test_reset_pin_issues_a_code(staff, app_module):
    member = make_student(pin_hash=generate_password_hash("1234"))
    r = staff.post(f"/api/members/{member.id}/reset-pin")
    code = r.get_json()["pin_setup_code"]
    assert len(code) == 6
    stored = _member(app_module, member.id)
    assert stored.pin_hash is None and stored.pin_setup_code == code


def test_approved_reset_request_issues_a_code(staff, app_module):
    from conftest import _persist
    from models import PinResetRequest
    member = make_student(pin_hash=generate_password_hash("1234"))
    req = _persist(PinResetRequest(member_id=member.member_id, status="Pending", requested_at="2026-10-28"))
    r = staff.post(f"/api/pin-reset-requests/{req.id}/approve")
    assert r.status_code == 200
    assert _member(app_module, member.id).pin_setup_code == r.get_json()["pin_setup_code"]


def test_issue_code_refuses_a_card_with_a_pin(staff):
    member = make_student(pin_hash=generate_password_hash("1234"))
    assert staff.post(f"/api/members/{member.id}/pin-code").status_code == 400


def test_issue_code_replaces_the_old_one(staff, app_module):
    member = make_student(pin_setup_code="K7MP2Q")
    code = staff.post(f"/api/members/{member.id}/pin-code").get_json()["pin_setup_code"]
    assert code != "K7MP2Q" and _member(app_module, member.id).pin_setup_code == code


def test_bulk_codes_only_for_cards_without_pin_or_code(staff, app_module):
    no_pin = make_student("A1", class_dept="7-A")
    other_class = make_student("A2", class_dept="8-B")
    has_pin = make_student("A3", class_dept="7-A", pin_hash=generate_password_hash("1234"))
    has_code = make_student("A4", class_dept="7-A", pin_setup_code="K7MP2Q")

    r = staff.post("/api/members/pin-codes", json={"class_dept": "7-A"})
    assert r.get_json()["issued"] == 1
    assert _member(app_module, no_pin.id).pin_setup_code
    assert _member(app_module, other_class.id).pin_setup_code is None
    assert _member(app_module, has_pin.id).pin_setup_code is None
    assert _member(app_module, has_code.id).pin_setup_code == "K7MP2Q"

    assert staff.post("/api/members/pin-codes", json={}).get_json()["issued"] == 1


def test_codes_are_listed_for_librarians_not_viewers(client):
    make_student(pin_setup_code="K7MP2Q")
    login_as(client, _make_user("librarian", "lib"))
    assert client.get("/api/members").get_json()[0]["pin_setup_code"] == "K7MP2Q"
    login_as(client, _make_user("viewer", "view"))
    row = client.get("/api/members").get_json()[0]
    assert "pin_setup_code" not in row and row["pin_code_issued"] is True


def test_code_routes_are_staff_only(client):
    member = make_student()
    login_as(client, _make_user("viewer", "view"))
    for path in (f"/api/members/{member.id}/pin-code", f"/api/members/{member.id}/unlock",
                 "/api/members/pin-codes"):
        assert client.post(path).status_code == 403
