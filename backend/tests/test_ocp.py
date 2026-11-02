import pytest

from app.resources.connectors.base import ConnectorError, Handle
from app.resources.connectors.ocp import OcpConnector
from tests.helpers import fakes, grant  # noqa: F401

CFG = {"api_url": "https://api.cluster.example:6443"}


def make(api, name="PROD-OCP", **kw):
    return api.post("/api/resources", json={"type": "ocp", "name": name, "tags": ["prod"], "config": CFG,
                                            "secret": {"token": "sha256~tok"}, **kw})


def test_create_test_and_hide_token(admin, fakes):
    r = make(admin)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "ocp" and body["secret"] == {"set": True} and "tok" not in r.text
    assert admin.post(f"/api/resources/{body['id']}/test").json()["ok"] is True
    fakes.ocp.fail = ConnectorError("The cluster rejected the token")
    t = admin.post(f"/api/resources/{body['id']}/test").json()
    assert t["ok"] is False and admin.get(f"/api/resources/{body['id']}").json()["status"] == "down"
    assert admin.get("/api/home/summary").json()["by_type"] == {"ocp": {"down": 1}}


def test_validation_and_edit_keeps_token(admin, fakes):
    assert admin.post("/api/resources", json={"type": "ocp", "name": "x", "config": {"api_url": "ftp://h"},
                                              "secret": {"token": "t"}}).status_code == 422
    assert admin.post("/api/resources", json={"type": "ocp", "name": "x", "config": CFG, "secret": {}}).status_code == 422
    rid = make(admin).json()["id"]
    r = admin.patch(f"/api/resources/{rid}", json={"name": "RENAMED", "config": {**CFG, "verify_tls": False}})
    assert r.status_code == 200 and r.json()["name"] == "RENAMED" and r.json()["secret"] == {"set": True}


def test_create_needs_the_ocp_permission(admin, make_user, fakes):
    u = make_user("olga")
    assert make(u).status_code == 403
    grant(admin, u, "inventory.ocp.create")
    assert make(u).status_code == 201


class _Http:
    def __init__(self, status=200, body=b'{"gitVersion": "v1.29"}'):
        self.status, self.body, self.calls = status, body, []

    def request(self, h, method, url, headers, body):
        self.calls.append((method, url, h.secret, h.config["auth"]))
        return self.status, self.body


def handle():
    return Handle(id="1", name="o", type="ocp", config={**CFG, "timeout_s": 5, "verify_tls": True}, secret={"token": "tkn"})


def test_connector_sends_a_bearer_get_and_maps_errors():
    c = OcpConnector()
    c.http = _Http()
    assert c.get_json(handle(), "/api/v1/pods", {"limit": 5}) == {"gitVersion": "v1.29"}
    method, url, secret, auth = c.http.calls[0]
    assert (method, url, auth) == ("GET", "https://api.cluster.example:6443/api/v1/pods?limit=5", "bearer")
    assert secret == {"token": "tkn"}
    for status, msg in ((401, "rejected the token"), (403, "rejected the token"), (500, "HTTP 500")):
        c.http = _Http(status)
        with pytest.raises(ConnectorError, match=msg):
            c.ping(handle())
    c.http = _Http(200, b"<html>")
    with pytest.raises(ConnectorError, match="JSON"):
        c.ping(handle())
