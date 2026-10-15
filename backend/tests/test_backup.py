import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db as dbmod
from app.auth import passwords
from app.backup import service
from app.errors import AppError
from app.models import Group, User, UserGroup
from tests.conftest import Api

PASS = "backup-passphrase-1"


def test_encrypt_round_trip_and_wrong_passphrase():
    blob = service.encrypt(b"data", PASS)
    assert service.decrypt(blob, PASS) == b"data"
    with pytest.raises(AppError):
        service.decrypt(blob, "wrong-passphrase-1")
    with pytest.raises(AppError):
        service.decrypt(b"garbage" * 10, PASS)


def test_export_excludes_password_hashes(admin, db):
    export = service.build_export(db)
    assert "password" not in str(export).lower()


def test_backup_restore_into_empty_db(admin, make_user, settings):
    u = make_user("mona")
    g = admin.post("/api/admin/groups", json={"name": "ops", "description": "operators"}).json()
    admin.put(f"/api/admin/groups/{g['id']}/members", json={"user_ids": [u.id]})
    admin.put(f"/api/admin/groups/{g['id']}/permissions", json={"permissions": [{"permission": "llm.use"}]})

    b = admin.post("/api/admin/backup", json={"passphrase": PASS})
    assert b.status_code == 201
    blob = admin.get(f"/api/admin/backups/{b.json()['id']}/download").content

    # Fresh database + fresh admin
    engine = dbmod.init_engine("sqlite://")
    dbmod.Base.metadata.create_all(engine)
    with dbmod.session_factory()() as db2:
        db2.add(User(login="admin", is_admin=True, password_hash=passwords.hash_password("correct-horse-battery")))
        db2.commit()
    from app.main import create_app

    api2 = Api(TestClient(create_app()), "admin", "correct-horse-battery")

    def post(dry):
        return api2.post("/api/admin/restore", data={"passphrase": PASS, "dry_run": str(dry).lower()},
                         files={"file": ("b.tar.enc", io.BytesIO(blob))})

    dry = post(True)
    assert dry.status_code == 200 and dry.json()["report"]["users"]["create"] == 1
    with dbmod.session_factory()() as db2:  # dry run changed nothing
        assert db2.scalar(select(User).where(User.login == "mona")) is None

    done = post(False).json()["report"]
    assert done["groups"]["create"] == 1 and done["memberships"]["create"] == 1 and done["permissions"]["create"] == 1
    with dbmod.session_factory()() as db2:
        mona = db2.scalar(select(User).where(User.login == "mona"))
        assert mona.must_change_pw and db2.scalar(select(UserGroup).where(UserGroup.user_id == mona.id))
        assert db2.scalar(select(Group).where(Group.name == "ops")).description == "operators"

    again = post(False).json()["report"]  # idempotent
    assert all(v["create"] == 0 for v in again.values())


def test_restore_wrong_passphrase_and_non_admin(admin, make_user):
    blob = service.encrypt(b"x", PASS)
    r = admin.post("/api/admin/restore", data={"passphrase": "nope-nope-nope", "dry_run": "true"},
                   files={"file": ("b", io.BytesIO(blob))})
    assert r.status_code == 422
    u = make_user("nina")
    r = u.post("/api/admin/restore", data={"passphrase": PASS}, files={"file": ("b", io.BytesIO(blob))})
    assert r.status_code == 403


def test_newer_schema_refused(db):
    import json
    import tarfile

    export = json.dumps({}).encode()
    import hashlib

    manifest = json.dumps({"schema_version": 99, "sha256": {"export.json": hashlib.sha256(export).hexdigest()}}).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for n, d in (("export.json", export), ("manifest.json", manifest)):
            i = tarfile.TarInfo(n)
            i.size = len(d)
            t.addfile(i, io.BytesIO(d))
    with pytest.raises(AppError) as e:
        service.read_archive(service.encrypt(buf.getvalue(), PASS), PASS)
    assert e.value.code == "CONFLICT"
