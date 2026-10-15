"""Fake connectors and small API helpers shared by the M2 tests."""
import pytest

ORACLE_CFG = {"host": "db1.example", "port": 1521, "service_name": "SVC1", "username": "RO"}


class FakeOracle:
    def __init__(self):
        self.rows, self.fail, self.calls = {}, {}, []

    def ping(self, h):
        if h.name in self.fail:
            raise self.fail[h.name]

    def query(self, h, sql, binds, max_rows):
        self.calls.append((h.name, sql, dict(binds)))
        if h.name in self.fail:
            raise self.fail[h.name]
        rows = self.rows.get(h.name, [["SYS", "u1", "boom", 3]])
        cols = [{"name": n, "type": t} for n, t in
                (("SYSTEM", "string"), ("REQ_USER", "string"), ("ERROR", "string"), ("COUNT", "number"))]
        return cols, [list(r) for r in rows][:max_rows]


class FakeSsh:
    def __init__(self):
        self.out, self.fail, self.calls = {}, None, []

    def ping(self, h):
        if self.fail:
            raise self.fail

    def exec(self, h, command, timeout_s=30):
        self.calls.append(command)
        if self.fail:
            raise self.fail
        h.observed["host_key_fingerprint"] = "SHA256:abc"
        return self.out.get(command.split()[0], "")


class FakeHttp:
    def __init__(self):
        self.status, self.body, self.calls = 200, b"{}", []

    def ping(self, h):
        pass

    def request(self, h, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return self.status, self.body


@pytest.fixture()
def fakes(db):
    from app.resources import service

    service.reset_vault()
    saved = dict(service.CONNECTORS)
    service.CONNECTORS.update(oracle=FakeOracle(), ssh=FakeSsh(), api=FakeHttp())
    yield type("F", (), {"oracle": service.CONNECTORS["oracle"], "ssh": service.CONNECTORS["ssh"],
                         "http": service.CONNECTORS["api"]})
    service.CONNECTORS.update(saved)


def grant(admin, user, *perms):
    r = admin.put(f"/api/admin/users/{user.id}/permissions",
                  json={"permissions": [{"permission": p} for p in perms]})
    assert r.status_code == 200, r.text


def new_oracle(api, name="SVC1", tags=("T",)):
    r = api.post("/api/resources", json={"type": "oracle", "name": name, "tags": list(tags),
                                         "config": {**ORACLE_CFG, "service_name": name},
                                         "secret": {"password": "s3cr3t-pw"}})
    assert r.status_code == 201, r.text
    return r.json()


def new_view(api, sources, name="Daily", config=None, **kw):
    body = {"type": "db_table", "name": name, "source_resource_ids": sources,
            "config": config or {"mode": "sql", "sql": "SELECT 1 AS x FROM dual"}, **kw}
    return api.post("/api/views", json=body)
