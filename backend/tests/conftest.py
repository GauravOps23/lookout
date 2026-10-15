import os

os.environ.update({
    "DATABASE_URL": "sqlite://",
    "MASTER_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    "COOKIE_SECURE": "false",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db as dbmod  # noqa: E402
from app.auth import passwords  # noqa: E402
from app.auth.router import reset_rate_limiter  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import User  # noqa: E402

ADMIN_PW = "correct-horse-battery"


@pytest.fixture()
def settings(tmp_path):
    s = get_settings()
    s.backup_dir = str(tmp_path / "backups")
    return s


@pytest.fixture()
def db(settings, tmp_path):
    # File-backed so threads (fan-out, scheduler, concurrent requests) each get their own connection.
    engine = dbmod.init_engine(f"sqlite:///{tmp_path / 'test.db'}")
    dbmod.Base.metadata.create_all(engine)
    reset_rate_limiter()
    with dbmod.session_factory()() as session:
        session.add(User(login="admin", display_name="Admin", is_admin=True,
                         password_hash=passwords.hash_password(ADMIN_PW)))
        session.commit()
        yield session


@pytest.fixture()
def client(db):
    from app.main import create_app

    return TestClient(create_app())


class Api:
    """Signed-in test client that sends the CSRF header automatically."""

    def __init__(self, client: TestClient, login: str, password: str):
        self.c = client
        r = client.post("/api/auth/login", json={"login": login, "password": password})
        assert r.status_code == 200, r.text
        self.csrf = r.json()["csrf_token"]

    def _h(self):
        return {"X-CSRF-Token": self.csrf}

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, **kw):
        return self.c.post(url, headers=self._h(), **kw)

    def put(self, url, **kw):
        return self.c.put(url, headers=self._h(), **kw)

    def patch(self, url, **kw):
        return self.c.patch(url, headers=self._h(), **kw)

    def delete(self, url, **kw):
        return self.c.delete(url, headers=self._h(), **kw)


@pytest.fixture()
def admin(client):
    return Api(client, "admin", ADMIN_PW)


@pytest.fixture()
def make_user(admin, client):
    """Create a user via the API, complete the forced password change, return a signed-in Api."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    def _make(login: str, **extra):
        r = admin.post("/api/admin/users", json={"login": login, **extra})
        assert r.status_code == 201, r.text
        temp, uid = r.json()["temp_password"], r.json()["id"]
        other = TestClient(create_app())
        api = Api(other, login, temp)
        new_pw = f"new-password-{login}"
        assert api.post("/api/auth/change-password",
                        json={"current_password": temp, "new_password": new_pw}).status_code == 204
        api.id, api.password = uid, new_pw
        return api

    return _make

from tests.helpers import fakes  # noqa: E402,F401  (fixture)
