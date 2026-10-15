import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db as dbmod
from app.auth import passwords
from app.backup import service
from app.models import Resource, ResourceAcl, User, View, ViewAcl
from app.resources import service as rsvc
from tests.conftest import Api
from tests.helpers import new_oracle, new_view

PASS = "backup-passphrase-1"


def _fresh_host():
    """Empty database + new admin, as on a rebuilt server."""
    engine = dbmod.init_engine("sqlite://")
    dbmod.Base.metadata.create_all(engine)
    with dbmod.session_factory()() as db2:
        db2.add(User(login="admin", is_admin=True, password_hash=passwords.hash_password("correct-horse-battery")))
        db2.commit()
    from app.main import create_app
    return Api(TestClient(create_app()), "admin", "correct-horse-battery")


def _post_restore(api, blob, dry):
    return api.post("/api/admin/restore", data={"passphrase": PASS, "dry_run": str(dry).lower()},
                    files={"file": ("b", io.BytesIO(blob))})


def _backup(admin, **body):
    b = admin.post("/api/admin/backup", json={"passphrase": PASS, **body})
    assert b.status_code == 201, b.text
    return admin.get(f"/api/admin/backups/{b.json()['id']}/download").content


def test_inventory_round_trip_with_secrets(admin, make_user, fakes, settings):
    u = make_user("bella")
    a = new_oracle(admin, "A", tags=["5G", "CEIR"])["id"]
    b = new_oracle(admin, "B")["id"]
    admin.put(f"/api/resources/{a}/acl", json={"entries": [
        {"principal_type": "user", "principal_id": u.id, "level": "use"}]})
    vid = new_view(admin, [a, b], name="Daily errors", schedule_cron="*/10 * * * *", cache_ttl_s=300).json()["id"]
    admin.put(f"/api/views/{vid}/acl", json={"entries": [
        {"principal_type": "user", "principal_id": u.id, "level": "view"}]})
    blob = _backup(admin)

    admin2 = _fresh_host()
    rsvc.reset_vault()
    dry = _post_restore(admin2, blob, True).json()["report"]
    assert dry["resources"]["create"] == 2 and dry["views"]["create"] == 1 and dry["acl"]["create"] == 2
    with dbmod.session_factory()() as s:
        assert s.scalar(select(Resource)) is None  # dry run wrote nothing

    done = _post_restore(admin2, blob, False).json()["report"]
    assert done["resources"]["create"] == 2 and done["views"]["create"] == 1
    with dbmod.session_factory()() as s:
        res = {r.name: r for r in s.scalars(select(Resource))}
        assert res["A"].tags == ["5G", "CEIR"] and res["A"].config["service_name"] == "A"
        assert rsvc.open_secret(res["A"]) == {"password": "s3cr3t-pw"}  # credentials survived, re-sealed
        v = s.scalar(select(View))
        assert v.name == "Daily errors" and v.schedule_cron == "*/10 * * * *"
        assert set(v.source_resource_ids) == {str(res["A"].id), str(res["B"].id)}
        bella = s.scalar(select(User).where(User.login == "bella"))
        assert {(x.level) for x in s.scalars(select(ResourceAcl)) if x.principal_id == bella.id} == {"use"}
        assert {(x.level) for x in s.scalars(select(ViewAcl)) if x.principal_id == bella.id} == {"view"}
    # a restored view is runnable on the new host
    assert admin2.post(f"/api/views/{v.id}/run").status_code == 200

    again = _post_restore(admin2, blob, False).json()["report"]  # idempotent
    assert all(r["create"] == 0 for r in again.values())


def test_backup_without_secrets_restores_resources_without_credentials(admin, fakes, settings):
    new_oracle(admin, "A")
    blob = _backup(admin, include_secrets=False)
    assert b"s3cr3t-pw" not in service.decrypt(blob, PASS)
    admin2 = _fresh_host()
    rsvc.reset_vault()
    assert _post_restore(admin2, blob, False).status_code == 200
    with dbmod.session_factory()() as s:
        assert s.scalar(select(Resource)).secret_blob is None


def test_plain_export_never_contains_secret_values(admin, fakes, db):
    new_oracle(admin, "A")
    assert "s3cr3t-pw" not in str(service.build_export(db))


def test_tampered_secrets_member_detected(admin, fakes, db):
    import tarfile
    new_oracle(admin, "A")
    plain = service.decrypt(service.build_archive(db, PASS), PASS)
    src, out = tarfile.open(fileobj=io.BytesIO(plain)), io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            if m.name == "secrets.json":
                data = data.replace(b"s3cr3t-pw", b"attacker!!")
            info = tarfile.TarInfo(m.name)
            info.size = len(data)
            dst.addfile(info, io.BytesIO(data))
    from app.errors import AppError
    with pytest.raises(AppError) as e:
        service.read_archive(service.encrypt(out.getvalue(), PASS), PASS)
    assert "checksum" in e.value.message


def test_duplicate_names_conflict(admin, fakes):
    rid = new_oracle(admin, "A")["id"]
    dup = admin.post("/api/resources", json={"type": "oracle", "name": "A", "config": {
        "host": "h", "service_name": "x", "username": "u"}, "secret": {"password": "p"}})
    assert dup.status_code == 409
    other = new_oracle(admin, "B")["id"]
    assert admin.patch(f"/api/resources/{other}", json={"name": "A"}).status_code == 409
    assert admin.patch(f"/api/resources/{rid}", json={"name": "A"}).status_code == 200  # own name is fine
    v = new_view(admin, [rid], name="V").json()["id"]
    assert new_view(admin, [rid], name="V").status_code == 409
    w = new_view(admin, [rid], name="W").json()["id"]
    assert admin.patch(f"/api/views/{w}", json={"name": "V"}).status_code == 409
    assert v


def test_ssrf_allowlist_setting(admin, make_user, db):
    assert admin.get("/api/admin/settings/ssrf-allowlist").json() == {"entries": []}
    assert admin.put("/api/admin/settings/ssrf-allowlist", json={"entries": ["127.0.0.0/8", "mock.internal"]}).status_code == 200
    assert rsvc.ssrf_allowlist(db) == ["127.0.0.0/8", "mock.internal"]
    assert admin.put("/api/admin/settings/ssrf-allowlist", json={"entries": ["not a host!"]}).status_code == 422
    u = make_user("nosettings")
    assert u.get("/api/admin/settings/ssrf-allowlist").status_code == 403


def test_dashboards_and_links_round_trip(admin, make_user, fakes, settings):
    from app.models import Dashboard, DashboardAcl, DashboardWidget, LinkTile
    u = make_user("dana")
    rid = new_oracle(admin, "A")["id"]
    vid = new_view(admin, [rid], name="V").json()["id"]
    did = admin.post("/api/dashboards", json={"name": "Servers", "widgets": [
        {"view_id": vid, "title": "errors", "grid": {"x": 0, "y": 0, "w": 6, "h": 4}}]}).json()["id"]
    admin.put(f"/api/dashboards/{did}/acl", json={"entries": [
        {"principal_type": "user", "principal_id": u.id, "level": "view"}]})
    token = admin.post(f"/api/dashboards/{did}/share-link").json()["token"]
    admin.post("/api/links", json={"name": "Grafana", "url": "https://g.example", "credential_mode": "shared",
                                   "credential": {"username": "ops", "password": "link-pw-7"}})
    blob = _backup(admin)
    assert token.encode() not in service.decrypt(blob, PASS)  # share tokens never leave the database

    admin2 = _fresh_host()
    rsvc.reset_vault()
    done = _post_restore(admin2, blob, False).json()["report"]
    assert done["dashboards"]["create"] == 1 and done["links"]["create"] == 1
    with dbmod.session_factory()() as s:
        d = s.scalar(select(Dashboard))
        assert d.name == "Servers" and d.share_token_hash is None
        w = s.scalar(select(DashboardWidget))
        assert w.title == "errors" and w.grid == {"x": 0, "y": 0, "w": 6, "h": 4}
        assert s.scalar(select(DashboardAcl)).level == "view"
    tile = admin2.get("/api/links").json()[0]
    assert tile["name"] == "Grafana" and tile["has_credential"]
    assert admin2.post(f"/api/links/{tile['id']}/reveal").json() == {"username": "ops", "password": "link-pw-7"}
    again = _post_restore(admin2, blob, False).json()["report"]
    assert all(r["create"] == 0 for r in again.values())
