import json
import threading
import time

from sqlalchemy import select

from app.models import Resource, ViewRun
from tests.helpers import grant, new_oracle, new_view

CAN_BUILD = ("inventory.oracle.create", "view.db_table.create")


def test_types_and_template(admin):
    types = {t["key"]: t for t in admin.get("/api/view-types").json()}
    assert set(types) == {"ssh_health", "db_table", "api_response"}
    assert types["db_table"]["config_schema"]["properties"]["sql"]
    assert admin.get("/api/view-templates").json()[0]["name"] == "Daily errors"


def test_create_checks_permission_sources_and_sql(admin, make_user, fakes):
    u = make_user("vera")
    rid = new_oracle(admin)["id"]
    assert new_view(u, [rid]).status_code == 403  # no capability
    grant(admin, u, *CAN_BUILD)
    assert new_view(u, [rid]).status_code == 404  # source is invisible without an ACL entry
    admin.put(f"/api/resources/{rid}/acl", json={"entries": [
        {"principal_type": "user", "principal_id": u.id, "level": "use"}]})
    assert new_view(u, [rid]).status_code == 201
    bad = new_view(u, [rid], config={"mode": "sql", "sql": "DELETE FROM t"})
    assert bad.status_code == 422
    assert new_view(u, [rid], config={"mode": "sql", "sql": "SELECT 1 FROM dual", "bogus": 1}).status_code == 422


def test_source_type_mismatch(admin, fakes):
    ssh = admin.post("/api/resources", json={"type": "ssh", "name": "S", "config": {
        "host": "h", "username": "u", "auth": "password"}, "secret": {"password": "p"}}).json()
    assert new_view(admin, [ssh["id"]]).status_code == 422


def test_run_returns_resultset_then_cache_then_fresh(admin, fakes):
    rid = new_oracle(admin)["id"]
    vid = new_view(admin, [rid], cache_ttl_s=300).json()["id"]
    r1 = admin.post(f"/api/views/{vid}/run").json()
    assert [c["name"] for c in r1["columns"]] == ["SYSTEM", "REQ_USER", "ERROR", "COUNT"]
    assert r1["meta"]["cached"] is False and r1["meta"]["row_count"] == 1
    r2 = admin.post(f"/api/views/{vid}/run").json()
    assert r2["meta"]["cached"] is True and len(fakes.oracle.calls) == 1
    admin.post(f"/api/views/{vid}/run", params={"fresh": 1})
    assert len(fakes.oracle.calls) == 2
    # engine wraps the SQL with a row cap and only passes binds that the SQL uses
    name, sql, binds = fakes.oracle.calls[0]
    assert sql.startswith("SELECT * FROM (") and "ROWNUM <= :max_rows" in sql and "max_rows" in binds


def test_row_cap_sets_truncated(admin, fakes):
    rid = new_oracle(admin)["id"]
    fakes.oracle.rows["SVC1"] = [["S", "u", f"e{i}", i] for i in range(50)]
    vid = new_view(admin, [rid], config={"mode": "sql", "sql": "SELECT 1 FROM dual", "row_limit": 10}).json()["id"]
    r = admin.post(f"/api/views/{vid}/run").json()
    assert len(r["rows"]) == 10 and r["meta"]["truncated"] is True


def test_fan_out_merges_and_reports_partial_failure(admin, fakes):
    a, b, c = (new_oracle(admin, n)["id"] for n in ("A", "B", "C"))
    fakes.oracle.rows = {"A": [["S", "u", "e1", 2]], "B": [["S", "u", "e2", 5]]}
    fakes.oracle.fail["C"] = RuntimeError("ORA-12541: TNS:no listener at db-c.internal:1521")
    vid = new_view(admin, [a, b, c], cache_ttl_s=0).json()["id"]
    r = admin.post(f"/api/views/{vid}/run").json()
    assert r["columns"][0]["name"] == "SOURCE"
    assert sorted(x[0] for x in r["rows"]) == ["A", "B"]
    by = {s["name"]: s for s in r["meta"]["sources"]}
    assert by["C"]["status"] == "error" and by["C"]["error"] == "Oracle error ORA-12541"  # failure visible, sanitised
    assert "db-c" not in json.dumps(r)


def test_all_sources_failing_is_an_error_and_recorded(admin, db, fakes):
    rid = new_oracle(admin)["id"]
    fakes.oracle.fail["SVC1"] = TimeoutError()
    vid = new_view(admin, [rid]).json()["id"]
    r = admin.post(f"/api/views/{vid}/run")
    assert r.status_code == 502 and r.json()["error"]["code"] == "UPSTREAM_ERROR"
    run = db.scalar(select(ViewRun))
    assert run.status == "error" and run.error == "TIMEOUT"


def test_losing_source_access_stops_the_view_on_next_run(admin, make_user, fakes):
    u = make_user("walt")
    grant(admin, u, *CAN_BUILD)
    rid = new_oracle(admin)["id"]
    admin.put(f"/api/resources/{rid}/acl", json={"entries": [
        {"principal_type": "user", "principal_id": u.id, "level": "use"}]})
    vid = new_view(u, [rid], cache_ttl_s=0).json()["id"]
    assert u.post(f"/api/views/{vid}/run").status_code == 200
    admin.put(f"/api/resources/{rid}/acl", json={"entries": []})  # revoke the source
    r = u.post(f"/api/views/{vid}/run")
    assert r.status_code == 404  # source now invisible to the runner


def test_view_level_sees_cached_result_but_cannot_run(admin, make_user, fakes):
    v, rid = make_user("vic"), new_oracle(admin)["id"]
    vid = new_view(admin, [rid]).json()["id"]
    admin.put(f"/api/views/{vid}/acl", json={"entries": [
        {"principal_type": "user", "principal_id": v.id, "level": "view"}]})
    assert v.get(f"/api/views/{vid}/result").status_code == 404  # nothing cached yet
    admin.post(f"/api/views/{vid}/run")
    assert v.get(f"/api/views/{vid}/result").status_code == 200
    assert v.post(f"/api/views/{vid}/run").status_code == 403
    assert v.get(f"/api/views/{vid}").json()["config"] is None  # SQL hidden at view level
    n = len(fakes.oracle.calls)
    v.get(f"/api/views/{vid}/result")
    assert len(fakes.oracle.calls) == n  # result endpoint never calls a connector


def test_variables_flow_into_binds_and_are_validated(admin, fakes):
    rid = new_oracle(admin)["id"]
    cfg = {"mode": "sql", "sql": "SELECT * FROM t WHERE cust = {{cust}}",
           "params": [{"name": "cust", "pattern": "^[0-9]{10}$", "description": "msisdn"}]}
    vid = new_view(admin, [rid], config=cfg).json()["id"]
    miss = admin.post(f"/api/views/{vid}/run", json={"params": {}})
    assert miss.status_code == 422 and miss.json()["error"]["details"]["missing"] == ["cust"]
    inj = admin.post(f"/api/views/{vid}/run", json={"params": {"cust": "1 OR 1=1"}})
    assert inj.status_code == 422 and not fakes.oracle.calls
    assert admin.post(f"/api/views/{vid}/run", json={"params": {"cust": "9876543210"}}).status_code == 200
    assert fakes.oracle.calls[0][2]["cust"] == "9876543210"
    assert "cust = :cust" in fakes.oracle.calls[0][1]
    assert admin.post(f"/api/views/{vid}/run", json={"params": {"nope": 1}}).status_code == 422


def test_cache_is_per_parameter_values(admin, fakes):
    rid = new_oracle(admin)["id"]
    cfg = {"mode": "sql", "sql": "SELECT * FROM t WHERE c = {{c}}", "params": [{"name": "c"}]}
    vid = new_view(admin, [rid], config=cfg, cache_ttl_s=300).json()["id"]
    for val in ("a", "b", "a"):
        admin.post(f"/api/views/{vid}/run", json={"params": {"c": val}})
    assert len(fakes.oracle.calls) == 2


def test_schedule_validation(admin, fakes):
    rid = new_oracle(admin)["id"]
    assert new_view(admin, [rid], schedule_cron="not cron").status_code == 422
    assert new_view(admin, [rid], schedule_cron="*/10 * * * *").status_code == 201
    cfg = {"mode": "sql", "sql": "SELECT * FROM t WHERE c = {{c}}", "params": [{"name": "c"}]}
    r = new_view(admin, [rid], name="needs-var", config=cfg, schedule_cron="*/5 * * * *")
    assert r.status_code == 422 and r.json()["error"]["details"]["missing_defaults"] == ["c"]
    cfg["params"][0]["default"] = "x"
    assert new_view(admin, [rid], name="has-default", config=cfg, schedule_cron="*/5 * * * *").status_code == 201


def test_concurrent_identical_runs_coalesce(admin, fakes):
    rid = new_oracle(admin)["id"]
    vid = new_view(admin, [rid], cache_ttl_s=0).json()["id"]
    orig = fakes.oracle.query

    def slow(h, sql, binds, max_rows):
        time.sleep(0.3)
        return orig(h, sql, binds, max_rows)

    fakes.oracle.query = slow
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.conftest import ADMIN_PW, Api
    apis = [Api(TestClient(create_app()), "admin", ADMIN_PW) for _ in range(3)]
    codes = []
    ts = [threading.Thread(target=lambda a=a: codes.append(a.post(f"/api/views/{vid}/run").status_code)) for a in apis]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert codes == [200, 200, 200] and len(fakes.oracle.calls) < 3


def test_ssh_custom_commands_need_permission_and_tofu_pin(admin, make_user, db, fakes):
    u = make_user("sam")
    grant(admin, u, "inventory.ssh.create", "view.ssh_health.create")
    rid = u.post("/api/resources", json={"type": "ssh", "name": "H1", "config": {
        "host": "h", "username": "u", "auth": "password"}, "secret": {"password": "p"}}).json()["id"]
    body = {"type": "ssh_health", "name": "h", "source_resource_ids": [rid],
            "config": {"checks": [{"id": "cpu_load", "warn": 1, "crit": 5}], "custom": [{"name": "x", "command": "id"}]}}
    assert u.post("/api/views", json=body).status_code == 403
    body["config"]["custom"] = []
    vid = u.post("/api/views", json=body).json()["id"]
    fakes.ssh.out["cat"] = "0.5 0.4 0.3 1/100 9"
    r = u.post(f"/api/views/{vid}/run").json()
    assert r["rows"][0][:3] == ["cpu_load", "load1", 0.5]
    row = db.scalar(select(Resource).where(Resource.name == "H1"))
    assert row.config["host_key_fingerprint"] == "SHA256:abc"  # pinned on first connect
    bad = u.post("/api/views", json={**body, "name": "h2", "config": {"checks": [{"id": "disk", "args": {"mount": "/;id"}}]}})
    assert bad.status_code == 422


def test_api_response_view_end_to_end(admin, fakes):
    res = admin.post("/api/resources", json={"type": "api", "name": "crm", "config": {
        "kind": "rest", "base_url": "https://crm.example/api", "allowed_methods": ["GET", "POST"],
        "auth": "bearer"}, "secret": {"token": "tok-123"}}).json()
    cfg = {"method": "GET", "path": "/customers/{{id}}/orders", "query_template": "status={{st}}",
           "variables": [{"name": "id", "pattern": "^[0-9]+$"}, {"name": "st", "type": "enum", "enum": ["open", "closed"]}],
           "mapping": {"root": "$.items[*]", "fields": [{"name": "ref", "path": "$.ref"},
                                                         {"name": "amt", "path": "$.amount", "type": "number"}]}}
    r = admin.post("/api/views", json={"type": "api_response", "name": "orders", "config": cfg,
                                       "source_resource_ids": [res["id"]]})
    assert r.status_code == 201, r.text
    fakes.http.body = json.dumps({"items": [{"ref": "A1", "amount": "12.5"}, {"ref": "B2"}]}).encode()
    out = admin.post(f"/api/views/{r.json()['id']}/run", json={"params": {"id": "42", "st": "open"}}).json()
    assert out["rows"] == [["A1", 12.5], ["B2", None]]
    call = fakes.http.calls[0]
    assert call["url"] == "https://crm.example/api/customers/42/orders?status=open" and call["method"] == "GET"
    assert admin.post(f"/api/views/{r.json()['id']}/run", json={"params": {"id": "4/../2", "st": "open"}}).status_code == 422


def test_api_view_config_rules(admin, fakes):
    res = admin.post("/api/resources", json={"type": "api", "name": "x", "config": {
        "base_url": "https://x.example"}}).json()
    base = {"type": "api_response", "name": "n", "source_resource_ids": [res["id"]]}
    undeclared = admin.post("/api/views", json={**base, "config": {"path": "/{{nope}}"}})
    assert undeclared.status_code == 422
    hdr = admin.post("/api/views", json={**base, "config": {"headers": {"X-A": "{{h}}"}, "variables": [{"name": "h"}]}})
    assert hdr.status_code == 422  # header variable without a pattern
    ok = admin.post("/api/views", json={**base, "config": {"headers": {"X-A": "{{h}}"},
                                                            "variables": [{"name": "h", "pattern": "^[a-z]+$"}]}})
    assert ok.status_code == 201
    nopath = admin.post("/api/views", json={**base, "name": "m", "config": {"path": "evil.com/x"}})
    assert nopath.status_code == 422


def test_soap_escapes_variables(admin, fakes):
    res = admin.post("/api/resources", json={"type": "api", "name": "soapy", "config": {
        "kind": "soap", "base_url": "https://s.example/ws", "allowed_methods": ["POST"]}}).json()
    cfg = {"soap": {"operation": "GetUser", "envelope_template": "<e><id>{{id}}</id></e>"},
           "variables": [{"name": "id"}],
           "mapping": {"root": ".//user", "fields": [{"name": "name", "path": "name"}]}}
    vid = admin.post("/api/views", json={"type": "api_response", "name": "s", "config": cfg,
                                         "source_resource_ids": [res["id"]]}).json()["id"]
    fakes.http.body = b'<s:Envelope xmlns:s="x"><s:Body><user><name>Ann</name></user></s:Body></s:Envelope>'
    out = admin.post(f"/api/views/{vid}/run", json={"params": {"id": "</id><x/>"}}).json()
    assert out["rows"] == [["Ann"]]
    sent = fakes.http.calls[0]["body"].decode()
    assert "<x/>" not in sent and "&lt;/id&gt;" in sent and fakes.http.calls[0]["method"] == "POST"


def test_soap_rejects_entity_bombs(admin, fakes):
    res = admin.post("/api/resources", json={"type": "api", "name": "soapy", "config": {
        "kind": "soap", "base_url": "https://s.example/ws", "allowed_methods": ["POST"]}}).json()
    cfg = {"soap": {"operation": "Op", "envelope_template": "<e/>"}}
    vid = admin.post("/api/views", json={"type": "api_response", "name": "s", "config": cfg,
                                         "source_resource_ids": [res["id"]]}).json()["id"]
    fakes.http.body = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><l>&b;</l>'
    assert admin.post(f"/api/views/{vid}/run").status_code == 502  # parse refused, nothing expanded
