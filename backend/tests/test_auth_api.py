from sqlalchemy import select

from app.auth import passwords
from app.models import AuditLog, SessionRow, User
from tests.conftest import ADMIN_PW, Api


def test_login_sets_hardened_cookie(client):
    r = client.post("/api/auth/login", json={"login": "Admin", "password": ADMIN_PW})
    assert r.status_code == 200 and r.json()["csrf_token"]
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_session_token_stored_hashed(client, db):
    token = client.post("/api/auth/login", json={"login": "admin", "password": ADMIN_PW}).cookies["sid"]
    assert all(s.token_hash != token for s in db.scalars(select(SessionRow)))


def test_bad_login_is_generic(client):
    a = client.post("/api/auth/login", json={"login": "admin", "password": "wrong-password-1"})
    b = client.post("/api/auth/login", json={"login": "nobody", "password": "wrong-password-1"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_unauthenticated_and_csrf(client, admin):
    anon = client.__class__(client.app)
    assert anon.get("/api/admin/users").status_code == 401
    r = admin.c.post("/api/admin/users", json={"login": "x1"})  # no CSRF header
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


def test_lockout_after_five_failures(client, db):
    for _ in range(5):
        client.post("/api/auth/login", json={"login": "admin", "password": "wrong-password-1"})
    r = client.post("/api/auth/login", json={"login": "admin", "password": ADMIN_PW})
    assert r.status_code == 401  # correct password still rejected while locked
    actions = {a.action for a in db.scalars(select(AuditLog))}
    assert {"login.fail", "login.lockout", "login.locked"} <= actions


def test_login_rate_limit(client):
    codes = [client.post("/api/auth/login", json={"login": "n", "password": "x"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


def test_forced_password_change(admin, make_user, client):
    r = admin.post("/api/admin/users", json={"login": "dave"})
    temp = r.json()["temp_password"]
    from fastapi.testclient import TestClient

    from app.main import create_app

    api = Api(TestClient(create_app()), "dave", temp)
    blocked = api.get("/api/admin/users")
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"
    assert api.get("/api/auth/me").status_code == 200
    weak = api.post("/api/auth/change-password", json={"current_password": temp, "new_password": "short"})
    assert weak.status_code == 422
    ok = api.post("/api/auth/change-password", json={"current_password": temp, "new_password": "a-long-new-password"})
    assert ok.status_code == 204
    assert api.get("/api/admin/users").status_code == 403  # now allowed in, but not an admin: FORBIDDEN
    assert api.get("/api/admin/users").json()["error"]["code"] == "FORBIDDEN"


def test_password_policy():
    assert passwords.validate_strength("short")
    assert passwords.validate_strength("aaaaaaaaaaaaaaaa")
    assert passwords.validate_strength("password1234")
    assert passwords.validate_strength("a-decent-passphrase") is None


def test_logout_invalidates_session(admin):
    assert admin.post("/api/auth/logout").status_code == 204
    assert admin.get("/api/auth/me").status_code == 401


def test_unknown_fields_rejected(admin):
    r = admin.post("/api/admin/users", json={"login": "eve1", "bogus": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION"


def test_health(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
