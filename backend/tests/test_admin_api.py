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


def test_group_can_be_renamed_and_edited(admin, make_user):
    a = admin.post("/api/admin/groups", json={"name": "ops"}).json()
    admin.post("/api/admin/groups", json={"name": "dba"})
    r = admin.patch(f"/api/admin/groups/{a['id']}", json={"name": "operators", "description": "on call"})
    assert r.status_code == 200 and r.json()["name"] == "operators" and r.json()["description"] == "on call"
    assert admin.patch(f"/api/admin/groups/{a['id']}", json={"name": "dba"}).status_code == 409  # name taken
    assert admin.patch(f"/api/admin/groups/{a['id']}", json={"description": "x"}).json()["name"] == "operators"
    assert make_user("ivy").patch(f"/api/admin/groups/{a['id']}", json={"name": "z"}).status_code == 403


def test_delete_user_and_guards(admin, make_user):
    from tests.helpers import new_oracle

    me = admin.get("/api/auth/me").json()["user"]["id"]
    assert admin.delete(f"/api/admin/users/{me}").status_code == 409  # not yourself

    g = admin.post("/api/admin/groups", json={"name": "temps"}).json()
    u = make_user("zed")
    admin.put(f"/api/admin/groups/{g['id']}/members", json={"user_ids": [u.id]})
    assert admin.delete(f"/api/admin/users/{u.id}").status_code == 204
    assert u.id not in [x["id"] for x in admin.get("/api/admin/users").json()]
    assert admin.get("/api/admin/groups").json()[0]["members"] == []
    assert "user.delete" in [a["action"] for a in admin.get("/api/home/activity").json()]
    assert admin.delete(f"/api/admin/users/{u.id}").status_code == 404

    # a user who owns something cannot be deleted until it is gone
    ops = make_user("ops")
    admin.put(f"/api/admin/users/{ops.id}/permissions", json={"permissions": [
        {"permission": "admin.users"}, {"permission": "inventory.oracle.create"}]})
    res = new_oracle(ops, "OPS1")
    r = admin.delete(f"/api/admin/users/{ops.id}")
    assert r.status_code == 409 and "1 connections" in r.json()["error"]["message"]
    assert admin.delete(f"/api/resources/{res['id']}").status_code == 204
    # ops (not an admin) cannot delete the only active administrator
    assert ops.delete(f"/api/admin/users/{me}").status_code == 409
    assert admin.delete(f"/api/admin/users/{ops.id}").status_code == 204


def test_the_last_administrator_cannot_be_demoted_or_deactivated(admin, make_user):
    me = admin.get("/api/auth/me").json()["user"]["id"]
    ops = make_user("ops2")
    admin.put(f"/api/admin/users/{ops.id}/permissions", json={"permissions": [{"permission": "admin.users"}]})
    for change in ({"is_admin": False}, {"is_active": False}):  # ops is not an admin, so only the guard stops this
        r = ops.patch(f"/api/admin/users/{me}", json=change)
        assert r.status_code == 409 and "at least one active administrator" in r.json()["error"]["message"]
    boss = make_user("boss2", is_admin=True)                       # a second administrator lifts the restriction
    assert ops.patch(f"/api/admin/users/{boss.id}", json={"is_admin": False}).status_code == 200
    assert ops.patch(f"/api/admin/users/{boss.id}", json={"is_admin": True}).status_code == 200
    assert ops.patch(f"/api/admin/users/{me}", json={"is_admin": False}).status_code == 200


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
