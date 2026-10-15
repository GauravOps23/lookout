import pytest
from sqlalchemy import select

from app.errors import AppError
from app.models import Resource
from app.resources import service
from tests.helpers import ORACLE_CFG, grant, new_oracle, new_view


def test_create_requires_capability_and_hides_secret(admin, make_user, db, fakes):
    u = make_user("rita")
    r = u.post("/api/resources", json={"type": "oracle", "name": "A", "config": ORACLE_CFG,
                                       "secret": {"password": "pw"}})
    assert r.status_code == 403
    grant(admin, u, "inventory.oracle.create")
    made = new_oracle(u, "A")
    assert made["secret"] == {"set": True}
    assert "s3cr3t-pw" not in str(u.get(f"/api/resources/{made['id']}").json())
    row = db.scalar(select(Resource).where(Resource.name == "A"))
    assert b"s3cr3t-pw" not in row.secret_blob  # encrypted at rest


def test_secret_bound_to_row(admin, db, fakes):
    new_oracle(admin, "A")
    new_oracle(admin, "B")
    ra, rb = (db.scalar(select(Resource).where(Resource.name == n)) for n in "AB")
    rb.secret_blob = ra.secret_blob  # a blob copied onto another row must not decrypt
    with pytest.raises(AppError):
        service.open_secret(rb)


def test_validation_and_unknown_fields(admin, fakes):
    assert admin.post("/api/resources", json={"type": "oracle", "name": "X",
                                              "config": {"host": "h"}}).status_code == 422
    extra = admin.post("/api/resources", json={"type": "oracle", "name": "X",
                                               "config": {**ORACLE_CFG, "evil": 1}, "secret": {"password": "p"}})
    assert extra.status_code == 422
    ftp = admin.post("/api/resources", json={"type": "api", "name": "X", "config": {"base_url": "ftp://x"}})
    assert ftp.status_code == 422
    meth = admin.post("/api/resources", json={"type": "api", "name": "X", "config": {
        "base_url": "https://x.example", "allowed_methods": ["DELETE"]}})
    assert meth.status_code == 422  # only GET and POST exist


def test_acl_levels_and_visibility(admin, make_user, fakes):
    owner, other = make_user("olive"), make_user("otto")
    grant(admin, owner, "inventory.oracle.create")
    rid = new_oracle(owner, "A")["id"]
    assert other.get("/api/resources").json() == []  # list never 403s, just omits
    assert other.get(f"/api/resources/{rid}").status_code == 404

    def put(level):
        return owner.put(f"/api/resources/{rid}/acl", json={"entries": [
            {"principal_type": "user", "principal_id": other.id, "level": level}]})

    assert put("view").status_code == 200
    seen = other.get(f"/api/resources/{rid}").json()
    assert seen["level"] == "view" and seen["config"] is None  # view = metadata only
    assert other.post(f"/api/resources/{rid}/test").status_code == 403
    assert other.patch(f"/api/resources/{rid}", json={"name": "Z"}).status_code == 403
    put("use")
    assert other.post(f"/api/resources/{rid}/test").status_code == 200
    assert other.get(f"/api/resources/{rid}").json()["config"]["host"] == "db1.example"
    put("edit")
    assert other.patch(f"/api/resources/{rid}", json={"name": "Z"}).status_code == 200
    assert other.delete(f"/api/resources/{rid}").status_code == 403  # edit < manage
    assert other.put(f"/api/resources/{rid}/acl", json={"entries": []}).status_code == 403


def test_group_acl(admin, make_user, fakes):
    u = make_user("gwen")
    g = admin.post("/api/admin/groups", json={"name": "dba"}).json()
    admin.put(f"/api/admin/groups/{g['id']}/members", json={"user_ids": [u.id]})
    rid = new_oracle(admin)["id"]
    admin.put(f"/api/resources/{rid}/acl", json={"entries": [
        {"principal_type": "group", "principal_id": g["id"], "level": "use"}]})
    assert u.get(f"/api/resources/{rid}").json()["level"] == "use"


def test_test_endpoint_stores_status_and_sanitises(admin, fakes):
    rid = new_oracle(admin)["id"]
    ok = admin.post(f"/api/resources/{rid}/test").json()
    assert ok["ok"] and ok["status"] == "up"
    fakes.oracle.fail["SVC1"] = RuntimeError("ORA-01017: invalid username/password; logon denied for db1.example")
    bad = admin.post(f"/api/resources/{rid}/test").json()
    assert not bad["ok"] and bad["error"] == "Oracle error ORA-01017" and "db1" not in str(bad)
    assert admin.get(f"/api/resources/{rid}").json()["status"] == "down"


def test_delete_blocked_by_dependent_views(admin, fakes):
    rid = new_oracle(admin)["id"]
    v = new_view(admin, [rid]).json()
    r = admin.delete(f"/api/resources/{rid}")
    assert r.status_code == 409 and r.json()["error"]["details"]["views"][0]["id"] == v["id"]
    admin.delete(f"/api/views/{v['id']}")
    assert admin.delete(f"/api/resources/{rid}").status_code == 204


def test_transfer_and_filters(admin, make_user, fakes):
    u = make_user("tina")
    new_oracle(admin, "AAA", tags=["x"])
    new_oracle(admin, "BBB", tags=["y"])
    rid = new_oracle(admin, "CCC", tags=["x"])["id"]
    assert [r["name"] for r in admin.get("/api/resources", params={"tag": "x"}).json()] == ["AAA", "CCC"]
    assert [r["name"] for r in admin.get("/api/resources", params={"q": "bb"}).json()] == ["BBB"]
    assert admin.post(f"/api/resources/{rid}/transfer", json={"user_id": u.id}).status_code == 200
    assert u.get(f"/api/resources/{rid}").json()["level"] == "manage"


def test_patch_secret_keep_and_replace(admin, db, fakes):
    rid = new_oracle(admin)["id"]
    admin.patch(f"/api/resources/{rid}", json={"name": "Renamed"})
    row = db.scalar(select(Resource).where(Resource.name == "Renamed"))
    assert service.open_secret(row) == {"password": "s3cr3t-pw"}  # omitted secret is kept
    admin.patch(f"/api/resources/{rid}", json={"secret": {"password": "new-pw"}})
    db.refresh(row)
    assert service.open_secret(row) == {"password": "new-pw"}
