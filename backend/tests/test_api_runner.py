import json

from app.views.api_response.plugin import RAW_LIMIT
from tests.helpers import fakes  # noqa: F401


def new_api(admin, name="crm", kind="rest", methods=("GET", "POST")):
    return admin.post("/api/resources", json={"type": "api", "name": name, "config": {
        "kind": kind, "base_url": "https://crm.example/api", "allowed_methods": list(methods), "auth": "bearer"},
        "secret": {"token": "tok"}}).json()["id"]


def new_req(admin, rid, name="req", **cfg):
    return admin.post("/api/views", json={"type": "api_response", "name": name, "source_resource_ids": [rid],
                                          "config": {"output": "raw", **cfg}})


def send(admin, vid, params=None):
    return admin.post(f"/api/views/{vid}/run?fresh=true", json={"params": params or {}})


def test_raw_get_returns_status_and_pretty_json(admin, fakes):
    rid = new_api(admin)
    vid = new_req(admin, rid, path="/users/{{id}}", variables=[{"name": "id", "pattern": "^[0-9]+$"}]).json()["id"]
    fakes.http.body = b'{"id":7,"name":"Ann","tags":["a","b"]}'
    res = send(admin, vid, {"id": "7"}).json()
    assert [c["name"] for c in res["columns"]] == ["status", "body"]
    status, body = res["rows"][0]
    assert status == 200 and json.loads(body) == {"id": 7, "name": "Ann", "tags": ["a", "b"]} and "\n  " in body
    assert fakes.http.calls[0]["url"] == "https://crm.example/api/users/7" and fakes.http.calls[0]["method"] == "GET"


def test_raw_post_sends_the_json_body_template(admin, fakes):
    rid = new_api(admin)
    vid = new_req(admin, rid, method="POST", path="/orders", body_template={"sku": "{{sku}}", "qty": "{{qty}}", "note": "x"},
                  variables=[{"name": "sku"}, {"name": "qty", "type": "number"}]).json()["id"]
    fakes.http.status, fakes.http.body = 201, b'{"ok":true}'
    res = send(admin, vid, {"sku": "A-1", "qty": 3}).json()
    assert res["rows"][0][0] == 201
    call = fakes.http.calls[0]
    assert call["method"] == "POST" and call["headers"]["Content-Type"] == "application/json"
    assert json.loads(call["body"]) == {"sku": "A-1", "qty": 3, "note": "x"}  # typed value, not the string "3"


def test_raw_shows_error_responses_but_table_mode_still_fails(admin, fakes):
    rid = new_api(admin)
    raw = new_req(admin, rid, name="raw", path="/x").json()["id"]
    table = admin.post("/api/views", json={"type": "api_response", "name": "tbl", "source_resource_ids": [rid],
                                           "config": {"path": "/x"}}).json()["id"]
    fakes.http.status, fakes.http.body = 500, b'{"error":"boom"}'
    res = send(admin, raw)
    assert res.status_code == 200 and res.json()["rows"][0][0] == 500 and "boom" in res.json()["rows"][0][1]
    assert send(admin, table).status_code == 502  # a table view cannot map an error response


def test_raw_soap_is_not_parsed_so_nothing_expands(admin, fakes):
    rid = new_api(admin, "soapy", "soap", ("POST",))
    vid = new_req(admin, rid, soap={"operation": "GetUser", "envelope_template": "<e><id>{{id}}</id></e>"},
                  variables=[{"name": "id"}]).json()["id"]
    bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><l>&b;</l>'
    fakes.http.body = bomb
    res = send(admin, vid, {"id": "</id><x/>"})
    assert res.status_code == 200 and res.json()["rows"][0][1] == bomb.decode()  # as received, entities untouched
    sent = fakes.http.calls[0]
    assert sent["method"] == "POST" and "<x/>" not in sent["body"].decode() and "&lt;/id&gt;" in sent["body"].decode()


ENVELOPE_12 = """<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Header/><soap:Body>
<ins:DisplayRequest xmlns:ins="http://soa.comptel.com/2011/02/instantlink"><ins:RequestParameters>
<ins:Parameter name="ACT_CODE1" value="GetAccumulatorInfo"/><ins:Parameter name="MSISDN1" value="{{msisdn}}"/>
<ins:Parameter name="CIRCLE_ID" value="{{circle}}"/></ins:RequestParameters></ins:DisplayRequest></soap:Body></soap:Envelope>"""


def test_soap_12_from_a_typed_header_and_values_inside_attributes(admin, fakes):
    rid = new_api(admin, "instantlink", "soap", ("POST",))
    vid = new_req(admin, rid, path="/ws/service", soap={"envelope_template": ENVELOPE_12},
                  headers={"Content-Type": "application/soap+xml;charset=utf-8"}, variables=[
        {"name": "msisdn", "pattern": "^91[0-9]{10}$"}, {"name": "circle", "default": "UPW", "required": False, "pattern": "^[A-Z]{2,4}$"}]).json()["id"]
    fakes.http.body = b"<ok/>"
    assert send(admin, vid, {"msisdn": "919000000001"}).status_code == 200
    call = fakes.http.calls[0]
    assert call["url"] == "https://crm.example/api/ws/service" and call["method"] == "POST"
    assert call["headers"] == {"Content-Type": "application/soap+xml;charset=utf-8"}  # exactly what was typed, no SOAPAction
    sent = call["body"].decode()
    assert 'name="MSISDN1" value="919000000001"' in sent and 'name="CIRCLE_ID" value="UPW"' in sent  # default used
    assert send(admin, vid, {"msisdn": '9194"/><x/>'}).status_code == 422  # the pattern stops attribute injection
    assert send(admin, vid, {"msisdn": "919000000001", "circle": "up w"}).status_code == 422
    assert len(fakes.http.calls) == 1


def test_typed_headers_beat_the_defaults_and_defaults_fill_the_gaps(admin, fakes):
    rid = new_api(admin, "s", "soap", ("POST",))
    make = lambda n, soap, headers=None: new_req(admin, rid, name=n, soap={"envelope_template": "<e/>", **soap}, headers=headers or {}).json()["id"]
    plain, action = make("a", {}), make("b", {"operation": "GetUser"})
    lower = make("c", {}, {"content-type": "application/soap+xml", "SOAPAction": '"x"'})
    typed12 = make("d", {"operation": "GetUser"}, {"Content-Type": "application/soap+xml"})
    for v in (plain, action, lower, typed12):
        send(admin, v)
    h_plain, h_action, h_lower, h_12 = (c["headers"] for c in fakes.http.calls)
    assert h_plain == {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'}             # SOAP 1.1 defaults
    assert h_action == {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"GetUser"'}
    assert h_lower == {"content-type": "application/soap+xml", "SOAPAction": '"x"'}              # no duplicate Content-Type
    assert h_12 == {"Content-Type": "application/soap+xml", "SOAPAction": '"GetUser"'}           # an action still goes out if asked for
    assert new_req(admin, rid, name="e", soap={"version": "1.2", "envelope_template": "<e/>"}).status_code == 422  # no such setting


def test_rest_body_content_type_can_be_overridden(admin, fakes):
    rid = new_api(admin)
    vid = new_req(admin, rid, method="POST", path="/p", body_template={"a": 1}, headers={"content-type": "application/vnd.api+json"}).json()["id"]
    send(admin, vid)
    assert fakes.http.calls[0]["headers"] == {"content-type": "application/vnd.api+json"}


def test_raw_body_is_capped_and_flagged(admin, fakes):
    rid = new_api(admin)
    vid = new_req(admin, rid, path="/big").json()["id"]
    fakes.http.body = b"x" * (RAW_LIMIT + 500)
    res = send(admin, vid).json()
    assert len(res["rows"][0][1]) == RAW_LIMIT and res["meta"]["truncated"] is True


def test_output_mode_is_validated(admin, fakes):
    rid = new_api(admin)
    assert new_req(admin, rid, name="a", output="html").status_code == 422
