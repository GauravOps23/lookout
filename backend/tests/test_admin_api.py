import uuid


def test_non_admin_forbidden_everywhere(make_user):
    u = make_user("frank")
    for path in ("/api/admin/users", "/api/admin/groups", "/api/admin/audit", "/api/admin/backups"):
        r = u.get(path)
        assert r.status_code == 403, path


def test_group_permission_grants_access(admin, make_user):
    u = make_user("gina")
    assert u.get("/api/admin/audit").status_code == 403
    g = admin.post("/api/admin/groups", json={"name": "auditors"}).json()
    assert admin.put(f"/api/admin/groups/{g['id']}/members", json={"user_ids": [u.id]}).status_code == 200
    assert admin.put(f"/api/admin/groups/{g['id']}/permissions",
                     json={"permissions": [{"permission": "admin.audit"}]}).status_code == 200
    assert u.get("/api/admin/audit").status_code == 200
    assert u.get("/api/admin/users").status_code == 403  # only what was granted


def test_direct_permission_and_validation(admin, make_user):
    u = make_user("hank")
    bad = admin.put(f"/api/admin/users/{u.id}/permissions", json={"permissions": [{"permission": "nope"}]})
    assert bad.status_code == 422
    scoped = admin.put(f"/api/admin/users/{u.id}/permissions",
                       json={"permissions": [{"permission": "llm.use", "scope_id": str(uuid.uuid4())}]})
    assert scoped.status_code == 422  # llm.use takes no scope
    ok = admin.put(f"/api/admin/users/{u.id}/permissions", json={"permissions": [{"permission": "admin.users"}]})
    assert ok.status_code == 200
    assert u.get("/api/admin/users").status_code == 200


def test_deactivate_kills_sessions_and_blocks_login(admin, make_user, client):
    u = make_user("ivy")
    assert u.get("/api/auth/me").status_code == 200
    assert admin.patch(f"/api/admin/users/{u.id}", json={"is_active": False}).status_code == 200
    assert u.get("/api/auth/me").status_code == 401
    r = client.post("/api/auth/login", json={"login": "ivy", "password": u.password})
    assert r.status_code == 401


def test_admin_cannot_demote_self(admin):
    me = admin.get("/api/auth/me").json()["user"]["id"]
    assert admin.patch(f"/api/admin/users/{me}", json={"is_admin": False}).status_code == 409


def test_duplicate_login_conflict(admin):
    assert admin.post("/api/admin/users", json={"login": "jack"}).status_code == 201
    assert admin.post("/api/admin/users", json={"login": "JACK"}).status_code == 409


def test_reset_password_forces_change(admin, make_user, client):
    u = make_user("kim")
    temp = admin.post(f"/api/admin/users/{u.id}/reset-password").json()["temp_password"]
    assert u.get("/api/auth/me").status_code == 401  # old sessions gone
    assert client.post("/api/auth/login", json={"login": "kim", "password": u.password}).status_code == 401
    r = client.post("/api/auth/login", json={"login": "kim", "password": temp})
    assert r.json()["user"]["must_change_pw"] is True


def test_audit_trail_has_no_secrets_and_exports_csv(admin):
    admin.post("/api/admin/users", json={"login": "leo1"})
    rows = admin.get("/api/admin/audit", params={"action": "user.create"}).json()
    assert rows and "temp_password" not in str(rows) and "password_hash" not in str(rows)
    r = admin.get("/api/admin/audit/export.csv")
    assert r.status_code == 200 and r.text.startswith("at,actor_id,action")


def test_audit_scrub():
    from app.audit.service import scrub

    assert scrub({"password": "p", "nested": {"api_key": "k", "ok": 1}}) == {
        "password": "***", "nested": {"api_key": "***", "ok": 1}}
